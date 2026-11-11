#!/usr/bin/env python3
"""Prepared single-shot controller. Default checks files ONLY; --execute is explicit.

Execute only after parent review, as the normal host user owning the autoboot user
service. This is a RAM trial, never enrollment or installation. No target actions
are taken on import or without --execute.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from trial import OBSERVE_SECONDS
from active_trial import classify, fallback_gate, recovery_deadline

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SERVICE = 'j716c-autoboot.service'
PYTHON = ROOT / 'build/aura-python/bin/python'
SSH_HELPER = ROOT / 'scripts/ssh-j716c.sh'
ABORT = False


def check_inputs(recovery_only=False):
    manifest = json.loads((HERE / 'inputs.json').read_text())
    for item in manifest['files']:
        if recovery_only and not item['recovery']:
            continue
        path = Path(item['path'])
        if not path.is_absolute():
            path = ROOT / path
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if 'mode' in item and path.stat().st_mode & 0o777 != item['mode']:
            raise RuntimeError('Immutable input mode mismatch: ' + item['path'])
        if actual != item['sha256'] or path.stat().st_size != item['size']:
            raise RuntimeError('Immutable input mismatch: ' + item['path'])
    for item in manifest['links']:
        path = Path(item['path'])
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_symlink() or os.readlink(path) != item['target']:
            raise RuntimeError('Immutable symlink mismatch: ' + item['path'])
    return manifest


def command(args, timeout=15, check=True):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Local command failed: ' + args[0])
    return result


def service_properties():
    names = ['FragmentPath', 'DropInPaths', 'ExecStart', 'ExecStartPre', 'ExecStartPost',
             'ExecStop', 'ExecStopPost', 'WorkingDirectory', 'TriggeredBy', 'Restart']
    result = command(['systemctl', '--user', 'show', SERVICE] + ['--property=' + key for key in names])
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    expected_fragment = ROOT / 'config/j716c-autoboot.service'
    if Path(props.get('FragmentPath', '')).resolve() != expected_fragment:
        raise RuntimeError('Unexpected autoboot unit fragment')
    for key in ['DropInPaths', 'ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost', 'TriggeredBy']:
        if props.get(key):
            raise RuntimeError('Unexpected effective unit property: ' + key)
    expected_argv = str(PYTHON) + ' -u ' + str(ROOT / 'scripts/auto-boot-j716c.py')
    start = props.get('ExecStart', '')
    match = re.fullmatch(r'\{ path=([^;]+) ; argv\[\]=(.*?) ; ignore_errors=[^}]+\}', start)
    if not match or match[1].strip() != str(PYTHON) or match[2].strip() != expected_argv:
        raise RuntimeError('Unexpected effective autoboot ExecStart')
    if props.get('WorkingDirectory') != str(ROOT) or props.get('Restart') != 'on-failure':
        raise RuntimeError('Unexpected autoboot working directory or restart policy')
    return props


def service_active():
    return command(['systemctl', '--user', 'is-active', '--quiet', SERVICE], check=False).returncode == 0


def ssh_baseline(log, expected_release):
    remote = (r'''set -eu; test "$(uname -r)" = "''' + expected_release + r'''";
tr "\000" "\n" </proc/device-tree/compatible | grep -qx apple,j716c;
test "$(cat /sys/devices/system/cpu/online)" = 0-17;
test ! -e /proc/device-tree/chosen/j716c,ram-test;
printf 'J716C_BASELINE %s\n' "$(cat /proc/sys/kernel/random/boot_id)"''')
    try:
        result = command([str(SSH_HELPER), remote], timeout=9, check=False)
    except subprocess.TimeoutExpired:
        log.write('SSH_TIMEOUT\n');log.flush()
        return None
    log.write(result.stdout + result.stderr);log.flush()
    matches = re.findall(r'^J716C_BASELINE ([0-9a-f-]{36})$', result.stdout, re.M)
    return matches[-1] if result.returncode == 0 and len(matches) == 1 else None


def read_events(run):
    path = run / 'wire-events.jsonl'
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # A concurrent append may end in a partial line; reread at next poll.
            continue
    return events


def evidence_from(events, interference=False):
    intents = [event for event in events if event['kind'] == 'vector_intent']
    complete = [event for event in events if event['kind'] == 'vector_complete']
    return {'vector_intent': intents[0]['time'] if intents else None,
            'vector_complete': complete[0]['time'] if complete else None,
            'vector_bytes': complete[0].get('count', 0) if complete else None,
            'console_chunks': [event for event in events if event['kind'] == 'console'],
            'usb_events': [event for event in events if event['kind'] in ('present', 'absent')],
            'capture_gaps': [event for event in events if event['kind'] == 'capture_gap'],
            'post_vector_tx_attempts': sum(event['kind'] == 'blocked_tx' for event in events),
            'interference': interference, 'active_events': events}


def no_tty_consumers(identity):
    result = command(['fuser', identity['path']], check=False)
    if result.returncode != 1 or result.stdout.strip():
        raise RuntimeError('TTY has an existing or uninspectable consumer')
    modem = command(['systemctl', 'is-active', '--quiet', 'ModemManager.service'], check=False)
    if modem.returncode == 0:
        props = command(['udevadm', 'info', '--query=property', '--name', identity['path']]).stdout.splitlines()
        if 'ID_MM_DEVICE_IGNORE=1' not in props:
            raise RuntimeError('Active ModemManager is not explicitly ignoring this device')


def signal_abort(signum, frame):
    global ABORT
    ABORT = True


def execute(args, manifest):
    from no_tx_loader import tty_identity
    os.umask(0o077)
    runs = ROOT / 'build/j716c-stage1-active-runs'
    runs.mkdir(mode=0o700, exist_ok=True)
    run = runs / args.run_id
    run.mkdir(mode=0o700)  # Never reuse a trial directory.
    timeline = (run / 'controller-events.jsonl').open('a', buffering=1)
    timeline_failed = False
    def event(kind, **fields):
        nonlocal timeline_failed
        try:
            timeline.write(json.dumps({'time': time.monotonic(), 'kind': kind, **fields}, sort_keys=True) + '\n')
            timeline.flush();os.fsync(timeline.fileno())
        except OSError:
            # A diagnostic log failure must never bypass the recovery trap.
            timeline_failed = True
    event('prepared', utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
          inputs_sha256=hashlib.sha256((HERE / 'inputs.json').read_bytes()).hexdigest())
    ssh_log = (run / 'ssh-private.log').open('a', buffering=1)
    loader_log = (run / 'loader-private.log').open('a', buffering=1)
    stopped = False
    child = None
    initial_boot = None
    recovered_boot = None
    interference = False
    failure = None
    boundary = None
    lease_origin = None
    active_attempted = False
    expected_release = (ROOT / 'build/j716c-autoboot/kernel.release').read_text().strip()
    if expected_release != manifest['baseline_release']:
        raise RuntimeError('Unexpected baseline release')

    def signal_child(signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    def handoff_active(evidence):
        nonlocal child,active_attempted
        from no_tx_loader import same_port_tty
        active_attempted=True
        gate=fallback_gate(evidence,time.monotonic())
        if not gate['eligible']:raise RuntimeError('Active fallback gate changed')
        (run/'stop-observer').touch(mode=0o600)
        child.wait(timeout=4)
        if child.returncode!=0:raise RuntimeError('Passive loader did not close cleanly')
        if not any(e['kind']=='observation_closed' for e in read_events(run)):
            raise RuntimeError('Passive descriptor closure not confirmed')
        current=same_port_tty(identity['physical_port'])
        if current is None or current['generation']!=gate['generation']:
            raise RuntimeError('Post-lookup USB generation changed')
        no_tty_consumers(current)
        if service_active():raise RuntimeError('Autoboot restarted before active handoff')
        check_inputs()
        text=(run/'loader-private.log').read_text(errors='replace')
        geometry=re.findall(r'T6050 staging: raw \[0x[0-9a-f]+, 0x[0-9a-f]+\), BootArgs 0x([0-9a-f]+), run_base 0x([0-9a-f]+)',text)
        if len(geometry)!=1:raise RuntimeError('Pinned loader geometry missing/ambiguous')
        authorization={'physical_port':current['physical_port'],'generation':gate['generation'],
                       'bootargs':int(geometry[0][0],16),'run_base':int(geometry[0][1],16),
                       'fallback_time':gate['fallback_time'],'single_attempt':True}
        (run/'active-authorization.json').write_text(json.dumps(authorization,sort_keys=True)+'\n')
        (run/'stop-observer').unlink()
        event('active_handoff_authorized',generation=current['generation'])
        child=subprocess.Popen([str(PYTHON),'-u',str(HERE/'active_probe.py'),'--execute','--run-dir',str(run)],
            cwd=ROOT,env=env,stdout=loader_log,stderr=subprocess.STDOUT,start_new_session=True)

    def await_boundary(allow_active=True):
        """The recovery trap shares this path. ABORT never shortens an entered lease."""
        nonlocal interference, boundary, lease_origin
        next_service_check = 0
        while True:
            evidence = evidence_from(read_events(run), interference)
            if evidence['vector_intent'] is not None:
                lease_origin = evidence['vector_intent']
                verdict = classify(evidence)
                if allow_active and not active_attempted and not ABORT and not timeline_failed and fallback_gate(evidence,time.monotonic())['eligible']:
                    handoff_active(evidence)
                    continue
                if verdict['reset_observed']:
                    boundary = 'observed_factory_reset'
                    event('recovery_boundary', reason=boundary)
                    return
                if time.monotonic() >= recovery_deadline(evidence):
                    boundary = 'bounded_observation_timeout'
                    event('recovery_boundary', reason=boundary)
                    return
            elif child is None or child.poll() is not None:
                boundary = 'no_vector_sent'
                return
            elif ABORT or time.monotonic() - started > 100:
                # Stop first, then reread the fsynced intent before terminating.
                signal_child(signal.SIGSTOP)
                evidence = evidence_from(read_events(run), interference)
                if evidence['vector_intent'] is None:
                    signal_child(signal.SIGKILL)
                    child.wait(timeout=5)
                    boundary = 'stopped_before_vector'
                    return
                signal_child(signal.SIGCONT)
                continue
            if time.monotonic() >= next_service_check:
                try:
                    if service_active():
                        interference = True
                        event('interference', detail='autoboot unexpectedly active; qualification invalid')
                        command(['systemctl', '--user', 'stop', SERVICE])
                except BaseException as error:
                    interference = True
                    event('service_monitor_error', type=type(error).__name__)
                next_service_check = time.monotonic() + 1
            time.sleep(0.1)

    try:
        service_properties()
        initial_boot = ssh_baseline(ssh_log, expected_release)
        if not initial_boot:
            raise RuntimeError('Pinned baseline SSH authentication/identity did not pass')
        event('initial_baseline_verified', boot_id=initial_boot)
        # Stop before entry and before the explicitly logged initial software reboot.
        stopped = True  # Even a timed-out stop request requires restoration.
        command(['systemctl', '--user', 'stop', SERVICE])
        if service_active():
            raise RuntimeError('Autoboot remains active')
        event('autoboot_stopped')
        if ABORT:
            raise RuntimeError('Interrupted before initial reboot')
        result = command([str(SSH_HELPER), '(sleep 2; reboot -f) </dev/null >/dev/null 2>&1 &'], timeout=9, check=False)
        ssh_log.write(result.stdout + result.stderr);ssh_log.flush()
        if result.returncode:
            raise RuntimeError('Initial baseline reboot request did not complete')
        event('pre_entry_software_reboot_requested')
        port = Path((ROOT / 'build/j716c-autoboot-private/port').read_text().strip())
        deadline = time.monotonic() + 55
        identity = None
        while time.monotonic() < deadline and not ABORT:
            try:
                identity = tty_identity(port)
                break
            except (OSError, RuntimeError):
                time.sleep(0.2)
        if identity is None:
            raise RuntimeError('Expected primary USB proxy did not enumerate')
        no_tty_consumers(identity)
        if service_active():
            raise RuntimeError('Autoboot restarted before entry')
        check_inputs()  # Close the preparation-to-entry hash gap.
        if ABORT or timeline_failed:
            raise RuntimeError('Interrupted or controller journal failed before loader')
        event('primary_USB0_selected', physical_port=identity['physical_port'], generation=identity['generation'])
        env = os.environ | {'USE_CLANG': '1', 'ARCH': 'aarch64-linux-gnu',
                            'TOOLCHAIN': '/usr/lib/llvm/22/bin/', 'LLDDIR': '/usr/lib/llvm/22/bin/',
                            'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('PYTHONPATH', None)
        started = time.monotonic()
        child = subprocess.Popen([str(PYTHON), '-u', str(HERE / 'no_tx_loader.py'), '--execute',
                                  '--run-dir', str(run), '--physical-port', identity['physical_port']],
                                 cwd=ROOT, env=env, stdout=loader_log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        event('single_loader_started', pid=child.pid)
        await_boundary()
    except BaseException as error:
        failure = type(error).__name__ + ': ' + str(error)
        event('controller_error', error=failure)
    finally:
        if stopped:
            try:
                # An exception must not restart the transmitting controller early.
                if boundary is None:
                    await_boundary(allow_active=False)
            except BaseException as error:
                event('recovery_observation_error', type=type(error).__name__)
                # Freeze an unobserved in-flight loader, then inspect its durable intent.
                if child and child.poll() is None:
                    signal_child(signal.SIGSTOP)
                evidence = evidence_from(read_events(run), True)
                interference = True
                if evidence['vector_intent'] is not None:
                    lease_origin = evidence['vector_intent']
                # If journal inspection itself failed, assume the latest possible entry.
                conservative = (lease_origin if lease_origin is not None else time.monotonic()) + OBSERVE_SECONDS
                while time.monotonic() < conservative:
                    time.sleep(max(0, min(0.5, conservative - time.monotonic())))
                boundary = 'bounded_timeout_after_observer_failure'
            if child and child.poll() is None:
                (run / 'stop-observer').touch(mode=0o600)
                try:
                    signal_child(signal.SIGCONT)
                    child.wait(timeout=4)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    if child.poll() is None:
                        signal_child(signal.SIGKILL)
                        child.wait(timeout=5)
            event('passive_observation_finished', boundary=boundary)
            possible_active=any(e['kind']=='active_arm_intent' for e in read_events(run))
            if possible_active and boundary!='observed_factory_reset':
                failure=failure or 'Manual recovery required: active packet possible but factory reset not observed'
                event('manual_recovery_required',detail='Known autoboot remains stopped; no reset of a stuck proxy is assumed')
            else:
                try:
                    check_inputs(recovery_only=True)
                    service_properties()
                    command(['systemctl', '--user', 'start', SERVICE])
                    event('known_autoboot_restarted')
                    baseline_deadline = time.monotonic() + 240
                    while time.monotonic() < baseline_deadline:
                        boot = ssh_baseline(ssh_log, expected_release)
                        if boot and (lease_origin is None or boot != initial_boot):
                            recovered_boot = boot
                            event('stock_baseline_recovered', boot_id=boot)
                            break
                        time.sleep(2)
                    if not recovered_boot:
                        event('baseline_recovery_failed', detail='No authenticated matching baseline within 240 s')
                except BaseException as error:
                    failure = failure or ('Recovery failed: ' + type(error).__name__ + ': ' + str(error))
                    event('recovery_failed', error=type(error).__name__)
        events = read_events(run)
        evidence = evidence_from(events, interference)
        verdict = classify(evidence)
        prearm_logged = 'Verified J716c WD1 armed for 160 seconds' in (run / 'loader-private.log').read_text(errors='replace')
        verdict.update({'trial_executed': True, 'single_attempt_only': True, 'host_prearm_verified': prearm_logged,
                        'baseline_recovered': recovered_boot is not None, 'initial_boot_id': initial_boot,
                        'recovered_boot_id': recovered_boot, 'recovery_boundary': boundary,
                        'interrupted': ABORT, 'controller_failure': failure, 'active_attempted': active_attempted,
                        'manual_recovery_required': bool(stopped and recovered_boot is None and any(e['kind']=='active_arm_intent' for e in events) and boundary!='observed_factory_reset'), 'controller_journal_failed': timeline_failed,
                        'full_scope_pass': bool(verdict['intended_scope_pass'] and prearm_logged
                                                and recovered_boot and not ABORT and not failure and not timeline_failed)})
        # Complete private evidence is retained; this result contains no credentials/addresses.
        (run / 'evidence-private.json').write_text(json.dumps(evidence, indent=2) + '\n')
        (run / 'result.json').write_text(json.dumps(verdict, indent=2, sort_keys=True) + '\n')
        ssh_log.close();loader_log.close();timeline.close()
    print(json.dumps({'run': str(run.relative_to(ROOT)), 'full_scope_pass': verdict['full_scope_pass'],
                      'baseline_recovered': verdict['baseline_recovered'], 'recovery_boundary': boundary}))
    return 0 if verdict['full_scope_pass'] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Parent-reviewed hardware trial; never automatic')
    parser.add_argument('--run-id', help='New private evidence directory name')
    args = parser.parse_args()
    manifest = check_inputs()
    if not args.execute:
        print(json.dumps({'prepared_only': True, 'immutable_inputs_verified': len(manifest['files']),
                          'fixture_sha256': manifest['fixture_sha256'], 'hardware_actions': 0}))
        return 0
    if not args.run_id or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}', args.run_id):
        parser.error('--execute requires a fresh, simple --run-id')
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal_abort)
    return execute(args, manifest)


if __name__ == '__main__':
    raise SystemExit(main())
