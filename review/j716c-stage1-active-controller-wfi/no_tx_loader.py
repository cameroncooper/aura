#!/usr/bin/env python3
"""Execute pinned loader once, seal on P_VECTOR, then observe using O_RDONLY only.

Importing this module has no target side effects. Only explicit --execute enters it.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import struct
import sys
import time
import traceback

from trial import OBSERVE_SECONDS, PassiveReader

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


class WireSeal:
    """One final vector packet is allowed; partial/error writes still seal forever."""
    def __init__(self, emit):
        self.emit = emit
        self.sealed = False

    def write(self, writer, data, *, vector=False):
        if self.sealed:
            self.emit({'kind': 'blocked_tx', 'count': len(data)})
            raise RuntimeError('Post-vector serial transmission blocked')
        if vector:
            if len(data) != 64 or struct.unpack_from('<IQ', data) != (0x01aa55ff, 0xb):
                raise RuntimeError('Unexpected final P_VECTOR packet')
            self.sealed = True
            self.emit({'kind': 'vector_intent', 'count': len(data)})
            try:
                count = writer(data)
            except BaseException:
                self.emit({'kind': 'vector_error'})
                raise
            self.emit({'kind': 'vector_complete', 'count': count})
            return count
        return writer(data)


def tty_identity(path):
    """No USB strings/serial identifiers, only USB0 identity and physical port."""
    tty = Path(path).resolve(strict=True)
    interface = (Path('/sys/class/tty') / tty.name / 'device').resolve(strict=True)
    if (interface / 'bInterfaceNumber').read_text().strip() != '00':
        raise RuntimeError('Not primary USB0 interface')
    device = interface.parent
    if ((device / 'idVendor').read_text().strip(), (device / 'idProduct').read_text().strip()) != ('1209', '316d'):
        raise RuntimeError('Not Aura CDC VID/PID')
    return {'path': str(tty), 'physical_port': device.name,
            'generation': (device / 'busnum').read_text().strip() + ':' + (device / 'devnum').read_text().strip()}


def same_port_tty(physical_port):
    found = []
    for tty in Path('/sys/class/tty').glob('ttyACM*'):
        try:
            identity = tty_identity(Path('/dev') / tty.name)
            if identity['physical_port'] == physical_port:
                found.append(identity)
        except (OSError, RuntimeError):
            continue
    if len(found) > 1:
        raise RuntimeError('Ambiguous primary USB interface on physical port')
    return found[0] if found else None


def observe(run, emit, vector_time, initial, reader):
    """No proxy object or serial method is used in this loop."""
    generation = initial['generation']
    present = initial
    emit({'kind': 'present', **initial})
    previous_other_carrier = None
    with (run / 'console.bin').open('ab', buffering=0) as raw:
        while time.monotonic() < vector_time + OBSERVE_SECONDS and not (run / 'stop-observer').exists():
            try:
                current = same_port_tty(initial['physical_port'])
            except (OSError, RuntimeError) as error:
                emit({'kind': 'capture_gap', 'reason': type(error).__name__})
                current = None
            if current is None:
                try:
                    usb = Path('/sys/bus/usb/devices') / initial['physical_port']
                    carrier = ((usb / 'idVendor').read_text().strip(), (usb / 'idProduct').read_text().strip())
                except OSError:
                    carrier = None
                if carrier is not None and carrier != previous_other_carrier:
                    emit({'kind': 'unmatched_carrier', 'vid': carrier[0], 'pid': carrier[1],
                          'note': 'Passive sysfs only; not opened or treated as SoC reset'})
                previous_other_carrier = carrier
            else:
                previous_other_carrier = None
            if present is not None and (current is None or current['generation'] != generation):
                emit({'kind': 'absent', 'generation': generation})
                if reader:
                    reader.close()
                    reader = None
                present = None
            if current is not None and present is None:
                generation = current['generation']
                present = current
                emit({'kind': 'present', **current})
            if current is not None and reader is None:
                try:
                    reader = PassiveReader(current['path'])
                    emit({'kind': 'passive_open', 'generation': generation, 'access': 'O_RDONLY'})
                except OSError as error:
                    emit({'kind': 'capture_gap', 'reason': 'open_errno_' + str(error.errno)})
                    time.sleep(0.2)
            if reader:
                try:
                    data = reader.read(65536)
                    if data:
                        raw.write(data)
                        emit({'kind': 'console', 'generation': generation, 'text': data.decode('latin1')})
                    else:
                        time.sleep(0.02)
                except OSError as error:
                    emit({'kind': 'capture_gap', 'reason': 'read_errno_' + str(error.errno)})
                    reader.close()
                    reader = None
                    time.sleep(0.1)
            else:
                time.sleep(0.1)
        if reader:
            reader.close()
    emit({'kind': 'observation_closed'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--physical-port', required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve(strict=True)
    if run.parent != ROOT / 'build/j716c-stage1-active-runs':
        parser.error('Unexpected private run directory')
    event_file = (run / 'wire-events.jsonl').open('a', buffering=1)
    def emit(event):
        event = dict(event, time=time.monotonic())
        event_file.write(json.dumps(event, sort_keys=True) + '\n')
        event_file.flush()
        os.fsync(event_file.fileno())
    # Verify immutable files immediately before importing any proxy code.
    from controller import check_inputs
    check_inputs()
    sys.path.insert(0, str(ROOT / 'src/aura/proxyclient'))
    import serial
    from aura.proxy import AuraProxy, UartInterface
    seal = WireSeal(emit)
    pending_vector = False
    passive = None
    initial = None
    vector_time = None
    serial_device = None
    original_write = serial.Serial.write
    original_cmd = UartInterface.cmd
    original_request = AuraProxy.request

    def guarded_write(device, data):
        nonlocal vector_time
        if pending_vector and not seal.sealed:
            vector_time = time.monotonic()
        return seal.write(lambda block: original_write(device, block), data, vector=pending_vector)

    def guarded_cmd(iface, cmd, payload=b''):
        nonlocal pending_vector, passive, initial, serial_device
        vector = cmd == UartInterface.REQ_PROXY and len(payload) >= 8 and struct.unpack_from('<Q', payload)[0] == AuraProxy.P_VECTOR
        if seal.sealed:
            emit({'kind': 'blocked_tx', 'origin': 'proxy_cmd'})
            raise RuntimeError('Post-vector proxy command blocked')
        if vector:
            initial = tty_identity(iface.devpath)
            if initial['physical_port'] != args.physical_port:
                raise RuntimeError('Proxy moved to a different physical USB port')
            # Open before vector: never flush input or race the candidate startup text.
            passive = PassiveReader(initial['path'])
            serial_device = iface.dev
            emit({'kind': 'passive_open', 'generation': initial['generation'], 'access': 'O_RDONLY'})
        pending_vector = vector
        try:
            return original_cmd(iface, cmd, payload)
        finally:
            pending_vector = False

    def guarded_request(proxy, opcode, *values, **options):
        if seal.sealed:
            emit({'kind': 'blocked_tx', 'origin': 'proxy_request'})
            raise RuntimeError('Post-vector proxy request blocked')
        if opcode == proxy.P_VECTOR:
            if any(not isinstance(value, int) for value in values):
                raise RuntimeError('P_VECTOR requires integer arguments; no deferred frees')
            options['no_reply'] = True
        return original_request(proxy, opcode, *values, **options)

    serial.Serial.write = guarded_write
    UartInterface.cmd = guarded_cmd
    AuraProxy.request = guarded_request
    wrapper = ROOT / 'scripts/chainload-j716c-gpu.py'
    sys.argv = [str(wrapper), str(ROOT / 'build/j716c-persistent-stage1-evidence/ram-probe/aura.bin'),
                '--aura-source', str(ROOT / 'build/j716c-dcp-aura-source')]
    try:
        runpy.run_path(str(wrapper), run_name='__main__')
    except SystemExit as error:
        emit({'kind': 'loader_exit', 'code': error.code})
    except BaseException as error:
        emit({'kind': 'loader_error', 'type': type(error).__name__, 'entry_possible': seal.sealed})
        traceback.print_exc()
    finally:
        if seal.sealed:
            # HUPCL was cleared before the vector. Closing here cannot send payload data.
            if serial_device:
                serial_device.close()
            try:
                observe(run, emit, vector_time, initial, passive)
            finally:
                if passive and passive.fd is not None:
                    passive.close()
        else:
            if passive:
                passive.close()
            emit({'kind': 'no_entry'})
        event_file.close()
    return 0 if seal.sealed else 2


if __name__ == '__main__':
    raise SystemExit(main())
