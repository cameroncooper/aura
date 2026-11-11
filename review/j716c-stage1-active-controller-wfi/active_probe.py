#!/usr/bin/env python3
"""Bounded active fallback helper. Import has no hardware side effects."""
import argparse
import json
import os
from pathlib import Path
import struct
import sys
import time
import traceback

from active_trial import ACTIVE_PHASE_SECONDS, ARM_OBSERVE_SECONDS, fallback_gate, samples_valid
from trial import PassiveReader

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
COUNT,ALARM,CONTROL=0x28836c010,0x28836c014,0x28836c01c


class ActiveWire:
    """Fail-closed packet allowlist, one exact API arm, irreversible final seal."""
    def __init__(self,emit,deadline,clock=time.monotonic):
        self.emit=emit;self.deadline=deadline;self.clock=clock;self.sealed=False
        self.regions=[];self.identity_verified=False;self.config_verified=False
        self.fallback_authorized=False;self.arm_seen=False;self.arm_verified=False;self.packets=0

    def seal(self):
        if not self.sealed:
            self.sealed=True;self.emit({'kind':'active_sealed'})

    def write(self,writer,data):
        if self.sealed or self.clock()>self.deadline:
            self.emit({'kind':'blocked_active_tx'});raise RuntimeError('Active TX sealed or expired')
        def reject():
            self.emit({'kind':'rejected_active_tx'});raise RuntimeError('Packet outside active allowlist')
        if len(data)!=64 or self.packets>=128:reject()
        cmd=struct.unpack_from('<I',data)[0];values=struct.unpack_from('<7Q',data,4)
        info={'kind':'active_tx','command':cmd}
        is_arm=cmd==0x01aa55ff and values==tuple([0x15,30,0,0,0,0,0])
        if not self.arm_seen and not (is_arm and self.fallback_authorized):reject()
        if self.arm_seen and not self.arm_verified:reject()
        if cmd==0x02aa55ff:
            addr,size=values[:2]
            if any(values[2:]) or not 0<size<=16384 or not any(start<=addr<addr+size<=end for start,end in self.regions):reject()
            info.update(read_size=size)
        elif cmd==0x01aa55ff:
            op,*args=values;info.update(opcode=op)
            if op in (0x3,0x4,0x13,0x905,0x908):
                if any(args):reject()
            elif op==0x105:
                if not self.identity_verified or args[0] not in (COUNT,ALARM,CONTROL) or any(args[1:]):reject()
            elif op==0x15:
                if not self.fallback_authorized or self.arm_seen or args!=[30,0,0,0,0,0]:reject()
                self.arm_seen=True;self.emit({'kind':'active_arm_intent'})
            else:reject()
        else:reject()
        self.packets+=1;self.emit(info)
        try:count=writer(data)
        except BaseException:
            self.seal();raise
        self.emit({'kind':'active_tx_complete','count':count})
        if count!=64:
            self.seal();raise RuntimeError('Partial active request; no retry')
        return count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args()
    run=args.run_dir.resolve(strict=True)
    if run.parent!=ROOT/'build/j716c-stage1-active-runs':parser.error('Unexpected run directory')
    from controller import check_inputs,read_events,evidence_from
    from no_tx_loader import tty_identity,same_port_tty,observe
    manifest=check_inputs()
    authorization=json.loads((run/'active-authorization.json').read_text())
    gate=fallback_gate(evidence_from(read_events(run)),time.monotonic())
    if not gate['eligible'] or gate['generation']!=authorization['generation']:
        raise RuntimeError('Post-lookup active authorization is missing or stale')
    identity=same_port_tty(authorization['physical_port'])
    if identity is None or identity['generation']!=authorization['generation']:
        raise RuntimeError('Candidate USB0 generation changed')
    if not any(e['kind']=='observation_closed' for e in read_events(run)):
        raise RuntimeError('Original passive observer is not closed')
    events=[]
    journal=(run/'wire-events.jsonl').open('a',buffering=1)
    def emit(event):
        item=dict(event,time=time.monotonic());events.append(item)
        journal.write(json.dumps(item,sort_keys=True)+'\n');journal.flush();os.fsync(journal.fileno())
    emit({'kind':'active_start','generation':identity['generation'],
          'first_frame':'P_WDT_ARM(30), no preceding NOP/setup/identity packet'})
    start=events[-1]['time']
    wire=ActiveWire(emit,deadline=start+ACTIVE_PHASE_SECONDS)
    wire.fallback_authorized=True
    sys.path.insert(0,str(ROOT/'src/aura/proxyclient'))
    import serial
    from aura.proxy import AuraProxy,UartInterface,Serial,IODEV
    from aura.tgtypes import BootArgs_r3
    from aura.adt import load_adt
    original_write=serial.Serial.write;original_read=serial.Serial.read
    device=None;passive=None;complete=False
    def guarded_write(dev,data):
        current=tty_identity(dev.port)
        if (current['physical_port'],current['generation'])!=(identity['physical_port'],identity['generation']):
            raise RuntimeError('USB generation changed during active phase')
        return wire.write(lambda block:original_write(dev,block),data)
    def guarded_read(dev,size=1):
        if wire.sealed or time.monotonic()>wire.deadline:
            raise TimeoutError('Active read deadline/seal reached')
        return original_read(dev,size)
    serial.Serial.write=guarded_write;serial.Serial.read=guarded_read
    try:
        # Aura Serial prevents input flushing; construction performs no data write.
        device=Serial(identity['path'],115200,timeout=1,write_timeout=1,exclusive=True)
        # Clear HUPCL and automatic echo/flow before the first packet, without a proxy request.
        from trial import raw_attributes
        import termios
        termios.tcsetattr(device.fileno(),termios.TCSANOW,raw_attributes(termios.tcgetattr(device.fileno())))
        iface=UartInterface(device);device.timeout=1
        def console(data):
            if data:
                with (run/'console.bin').open('ab',buffering=0) as raw:raw.write(data)
                emit({'kind':'console','generation':identity['generation'],'text':bytes(data).decode('latin1'),'phase':'active'})
        iface.unkhandler=console
        proxy=AuraProxy(iface)
        # FIRST valid full frame: source clears boot flag and invokes this leaf
        # synchronously. No NOP/bootstrap/setup/identity request precedes it.
        retval=proxy.wdt_arm(30)
        if retval!=0:raise RuntimeError('Explicit P_WDT_ARM(30) refused')
        wire.arm_verified=True
        emit({'kind':'active_arm_verified','retval':retval})
        if (proxy.get_chipid(),proxy.iodev_whoami(),proxy.cdc_status())!=(0x6050,IODEV.USB0,0x502):
            raise RuntimeError('Wrong live chip/proxy identity')
        base=proxy.get_base();ba_addr=proxy.get_bootargs()
        if (base,ba_addr)!=(authorization['run_base'],authorization['bootargs']):
            raise RuntimeError('Wrong candidate base or BootArgs')
        profile=manifest['active_profile']
        raw=(ROOT/profile['fixture']).read_bytes();offset=profile['config_offset'];block=raw[offset:offset+260]
        if not base%0x4000==0 or not offset+260<=profile['footprint']:
            raise RuntimeError('Invalid pinned config geometry')
        wire.regions=[(base+offset,base+offset+260),(ba_addr,ba_addr+BootArgs_r3.sizeof())]
        if iface.readmem(base+offset,260)!=block:raise RuntimeError('Live filled config differs from fixture')
        wire.config_verified=True;emit({'kind':'active_config_verified','bytes':260,'offset':offset})
        ba=BootArgs_r3.parse(iface.readmem(ba_addr,BootArgs_r3.sizeof()))
        adt_base=ba.devtree-ba.virt_base+ba.phys_base
        if (ba.revision!=3 or not 0<ba.devtree_size<=1<<20 or
            not 0<ba.phys_base<=adt_base<adt_base+ba.devtree_size<=base<ba.phys_base+ba.mem_size<1<<64):
            raise RuntimeError('BootArgs/ADT RAM geometry refused')
        wire.regions.append((adt_base,adt_base+ba.devtree_size))
        adt=load_adt(iface.readmem(adt_base,ba.devtree_size));chosen=adt['/chosen'];wdt=adt['/arm-io/wdt']
        if ((adt.model,adt.target_type,chosen.chip_id,chosen.board_id)!=('Mac17,6','J716c',0x6050,0xe)
            or wdt.wdt_version!=3 or wdt.get_reg(0)!=(0x28836c000,0x4000)
            or wdt.get_reg(2)!=(0x288330008,4)):
            raise RuntimeError('Live board/WD geometry refused')
        wire.identity_verified=True
        emit({'kind':'active_identity_verified','model':'Mac17,6','target':'J716c','chip':0x6050,'board':0xe,'USB0':True,'CDC':0x502,'ADT_bytes':ba.devtree_size})
        # No ADT, firmware or private unit properties are retained after parsing.
        del adt,wdt,chosen
        for index in range(3):
            if index:time.sleep(3)
            count=proxy.read32(COUNT);when=time.monotonic()
            alarm=proxy.read32(ALARM);control=proxy.read32(CONTROL)
            emit({'kind':'active_snapshot','index':index,'count':count,'alarm':alarm,'control':control,'count_receipt_time':when})
            if alarm!=720_000_000 or control!=4 or not count<720_000_000:
                raise RuntimeError('Watchdog readback refused')
        if not samples_valid(events):raise RuntimeError('Watchdog progress outside bounded24MHz expectations')
        wire.seal()
        emit({'kind':'active_probe_complete','requests':wire.packets})
        complete=True
    except BaseException as error:
        # No rescue NOP/arm/reboot/MMIO request: seal and observe, even on a prefix.
        emit({'kind':'active_error','type':type(error).__name__})
        traceback.print_exc()
    finally:
        wire.seal()
        try:
            current=same_port_tty(identity['physical_port'])
            if current and current['generation']==identity['generation']:
                passive=PassiveReader(current['path'])
            if device:device.close()
            evidence=evidence_from(read_events(run));evidence['active_events']=read_events(run)
            from active_trial import recovery_deadline
            # Reuse the proven passive loop, but give it the explicit active bound.
            observe(run,emit,recovery_deadline(evidence)-195.0,current or identity,passive)
        finally:
            if device and device.is_open:device.close()
            if passive and passive.fd is not None:passive.close()
            journal.close()
    return 0 if complete else 2


if __name__=='__main__':raise SystemExit(main())
