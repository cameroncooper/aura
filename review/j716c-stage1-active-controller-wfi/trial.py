#!/usr/bin/env python3
"""Passive tty primitives and conservative evidence classification."""
import fcntl
import os
import re
import select
import termios

WD_MARK = 'Stage 1: J716c disk watchdog verified (160 s)'
FACTORY_BANNER = 'aura 1.0.0b10-gbd9f3e82-dirty\n'
FACTORY_WD = 'WDT: could not arm the boot watchdog'
FACTORY_USB = 'J716c: USB2 proxy setup scheduled'
OBSERVE_SECONDS = 195.0


def raw_attributes(attrs):
    out = list(attrs)
    out[6] = list(attrs[6])
    out[0] = 0  # In particular, no kernel XON/XOFF or input translations.
    out[1] = 0
    out[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.HUPCL)
    out[2] &= ~getattr(termios, 'CRTSCTS', 0)
    out[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
    out[3] = 0  # No echo, signal characters or canonical processing.
    out[4] = out[5] = termios.B115200
    out[6][termios.VMIN] = 1
    out[6][termios.VTIME] = 0
    return out


class PassiveReader:
    """Kernel-enforced read-only FD; no serial library or transmit method."""
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            if fcntl.fcntl(self.fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                raise RuntimeError('Passive tty is not read-only')
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            termios.tcsetattr(self.fd, termios.TCSANOW, raw_attributes(termios.tcgetattr(self.fd)))
            attrs = termios.tcgetattr(self.fd)
            if attrs[0] & (termios.IXON | termios.IXOFF | getattr(termios, 'IXANY', 0)) or attrs[3] & termios.ECHO:
                raise RuntimeError('TTY automatic transmit/echo remains enabled')
        except BaseException:
            self.close()
            raise

    def read(self, count):
        if not select.select([self.fd], [], [], 0.2)[0]:
            return b''
        try:
            return os.read(self.fd, count)
        except BlockingIOError:
            return b''

    def close(self):
        if getattr(self, 'fd', None) is not None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCNXCL)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


def classify(evidence):
    chunks = evidence.get('console_chunks', [])
    text = ''.join(x['text'] for x in chunks)
    ends, count = [], 0
    for chunk in chunks:
        count += len(chunk['text'])
        ends.append((count, chunk['time']))

    def receipt(end):
        return next((when for size, when in ends if size >= end), None)

    wd_pos = text.find(WD_MARK)
    wd_time = receipt(wd_pos + len(WD_MARK)) if wd_pos >= 0 else None
    factory_start = factory_end = None
    if wd_pos >= 0:
        banners = list(re.finditer(r'\baura [^\n]+\n', text[wd_pos + len(WD_MARK):]))
        origin = wd_pos + len(WD_MARK)
        for i, banner in enumerate(banners):
            start = origin + banner.start()
            if text[start:origin + banner.end()].rstrip('\r\n') != FACTORY_BANNER.rstrip('\r\n'):
                continue
            stop = origin + banners[i + 1].start() if i + 1 < len(banners) else len(text)
            wd = text.find(FACTORY_WD, start, stop)
            usb = text.find(FACTORY_USB, wd, stop) if wd >= 0 else -1
            if usb >= 0:
                factory_start, factory_end = start, usb + len(FACTORY_USB)
                break
    factory_time = receipt(factory_end) if factory_end is not None else None
    candidate = text[wd_pos:factory_start] if wd_pos >= 0 else ''
    before_factory = text[:factory_start] if factory_start is not None else text
    software_reset = bool(re.search(r'\bpanic\b|Assertion failed|Unhandled exception|\bException(?: taken|:)|\brebooting\b', before_factory, re.I))
    host_seen = 'host request received' in candidate or 'WDT: host attached' in candidate
    refused = 'disk watchdog REFUSED' in text or 'final watchdog refused' in candidate
    pnf = candidate.find('Chainload failed: PartitionNotFound')
    nvme_error = bool(re.search(r'NVME init failed|nvme:.*(?:setup failed|preserving.*failure|timeout|Error)', candidate, re.I))
    if pnf >= 0:
        storage = 'partition_not_found_after_nvme_failure' if nvme_error else 'partition_not_found'
    elif nvme_error or 'reboot required' in candidate:
        storage = 'nvme_failed_before_partition'
    elif 'Chainload failed:' in candidate:
        storage = 'gpt_or_file_error'
    else:
        storage = 'lookup_not_established'
    fallback_pos = candidate.find('Running proxy...', max(0, pnf))
    fallback_time = receipt(wd_pos + fallback_pos + len('Running proxy...')) if fallback_pos >= 0 and wd_pos >= 0 else None
    cdc_ready = bool(re.search(r'CDC USB2 controller ready; awaiting host enumeration, status=0x502\b', candidate))
    reset_event = None
    if factory_time is not None:
        events = evidence.get('usb_events', [])
        for i, event in enumerate(events):
            if event['kind'] != 'absent' or not event['time'] <= factory_time <= event['time'] + 40:
                continue
            for following in events[i + 1:]:
                if (following['kind'] == 'present' and following.get('generation') != event.get('generation')
                        and event['time'] <= following['time'] <= factory_time <= following['time'] + 30):
                    reset_event = event
                    break
            if reset_event:
                break
    vec = evidence.get('vector_intent')
    reset_time = reset_event['time'] if reset_event else None
    marker_timely = wd_time is not None and vec is not None and 0 <= wd_time - vec <= 25
    timing = bool(marker_timely and reset_time is not None and vec + 154 <= reset_time <= wd_time + 166)
    stable_from = fallback_time if fallback_time is not None else wd_time
    gaps = [g for g in evidence.get('capture_gaps', []) if stable_from is not None
            and stable_from <= g['time'] < (reset_time - 1 if reset_time is not None else float('inf'))]
    pristine_tx = evidence.get('vector_bytes') == 64 and not evidence.get('post_vector_tx_attempts')
    natural = bool(reset_event and timing and pristine_tx and not software_reset and not host_seen
                   and not refused and not evidence.get('interference') and not gaps)
    scope = natural and storage == 'partition_not_found' and fallback_pos > pnf >= 0 and cdc_ready
    return {'candidate_WD_verified': wd_pos >= 0, 'WD_marker_time': wd_time,
            'marker_within_25s_of_vector': marker_timely,
            'storage_outcome': storage, 'partition_not_found_seen': pnf >= 0,
            'passive_USB_fallback_seen': bool(pnf >= 0 and fallback_pos > pnf and cdc_ready),
            'fallback_time': fallback_time, 'CDC_ready_message_seen': cdc_ready,
            'reset_observed': reset_event is not None, 'reset_disconnect_time': reset_time,
            'factory_fingerprint_time': factory_time, 'lease_timing_consistent': timing,
            'software_reset_evidence': software_reset, 'host_attached_after_entry': host_seen,
            'guard_refusal_seen': refused, 'capture_gap_ambiguity': bool(gaps),
            'post_vector_wire_pristine': pristine_tx,
            'natural_expiry_consistent': natural, 'intended_scope_pass': bool(scope),
            'cause_limit': 'Timing/source/no-transmit inference; no hardware reset-cause register was read',
            'USB_limit': 'Passive enumeration/console only; CDC control-line effects are possible and proxy protocol was not queried'}
