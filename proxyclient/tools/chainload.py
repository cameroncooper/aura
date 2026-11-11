#!/usr/bin/env python3
# SPDX-License-Identifier: MIT AND GPL-2.0-only
import sys, pathlib
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

import argparse, struct, time

LOAD_HEADER = struct.Struct("<8sIIQQ")
LOAD_HEADER_OFFSET = 0x808
RUN_BASE_OFFSET = LOAD_HEADER_OFFSET + 0x18
RAW_ENTRY = 0x800
PAGE_SIZE = 0x4000

parser = argparse.ArgumentParser(description='Mach-O loader for aura')
parser.add_argument('-q', '--quiet', action="store_true", help="Disable framebuffer")
parser.add_argument('-n', '--no-sepfw', action="store_true", help="Do not preserve SEPFW")
parser.add_argument('-c', '--call', action="store_true", help="Use call mode")
parser.add_argument('-r', '--raw', action="store_true", help="Image is raw")
parser.add_argument('-E', '--entry-point', action="store", type=int, help="Entry point for the raw image", default=RAW_ENTRY)
parser.add_argument('-x', '--xnu', action="store_true", help="Set up for chainloading XNU")
parser.add_argument('--vector-only', action="store_true", help="Return after vector reply without probing the next image")
parser.add_argument('--capture', type=pathlib.Path, help="Write raw post-vector console bytes to this file")
parser.add_argument('--capture-seconds', type=float, default=0, help="Seconds of raw console capture after vector")
parser.add_argument('--relocate', action='store_true', help='Allocate a new raw-image span while retaining the live stage-1 heap')
parser.add_argument('--t6050-run-base', action='store_true',
                    help='Stage raw Aura above J716c CTXR_D, then execute at the live Stage 1 base')
parser.add_argument('payload', type=pathlib.Path)
parser.add_argument('boot_args', default=[], nargs="*")
args = parser.parse_args()
if args.vector_only and args.call:
    parser.error('--vector-only cannot be combined with --call')
if args.capture and not args.vector_only:
    parser.error('--capture requires --vector-only')
if args.t6050_run_base:
    if not (args.raw and args.relocate and args.vector_only) or args.xnu or args.no_sepfw:
        parser.error('--t6050-run-base requires --raw --relocate --vector-only and preserved SEPFW')
    if args.entry_point != RAW_ENTRY:
        parser.error('--t6050-run-base requires the default raw Aura entry point')
    image = bytearray(args.payload.read_bytes())
    if len(image) < LOAD_HEADER_OFFSET + LOAD_HEADER.size:
        parser.error('Truncated AURALOAD header')
    magic, version, flags, footprint, run_base = LOAD_HEADER.unpack_from(image, LOAD_HEADER_OFFSET)
    if ((magic, version, flags, run_base) != (b'AURALOAD', 1, 1, 0) or
            not PAGE_SIZE <= footprint <= len(image) or footprint % PAGE_SIZE):
        parser.error('Invalid or prepatched AURALOAD run_base header')

from aura.setup import *
from aura.tgtypes import BootArgs_r1, BootArgs_r2, BootArgs_r3
from aura.macho import MachO
from aura import asm
from aura.bootmem import BOOT_BLOBS, find_boot_blobs, copy_boot_blobs
from construct import Array, Int64ul


def make_bootargs(top):
    tba = u.ba.copy()
    tba.top_of_kernel_data = top
    if args.boot_args:
        boot_args = " ".join(args.boot_args)
        tba.video.display = int("-v" not in boot_args.split())
        print(f"Setting boot arguments to {boot_args!r}")
        tba.cmdline = boot_args
    return tba


def vector(entry, *stage_args):
    print(f"Vectoring to 0x{entry:x}; no next-stage proxy request will be sent")
    p.request(p.P_VECTOR, entry, *stage_args)
    if args.capture:
        deadline = time.monotonic() + max(0, args.capture_seconds)
        with args.capture.open('wb') as output:
            while time.monotonic() < deadline:
                data = iface.dev.read(4096)
                if data:
                    output.write(data)
    sys.exit(0)


def stage_t6050(image, footprint):
    chosen = u.adt["/chosen"]
    if (p.get_chipid(), chosen.chip_id, chosen.board_id, u.adt.model, u.adt.target_type) != (
            0x6050, 0x6050, 0xe, "Mac17,6", "J716c"):
        raise ValueError("T6050 run_base staging requires Mac17,6 / J716c")
    if u.mrs("CurrentEL") != 8 or u.ba.revision != 3:
        raise ValueError("T6050 run_base staging requires EL2 and revision 3 BootArgs")
    # The next image replaces Stage 1 at its original reset address. A live secondary
    # could still execute the original image, so leave cold CPUs and their RVBARs alone.
    if any(p.smp_is_alive(cpu.cpu_id) for cpu in u.adt["cpus"] if cpu.state != "running"):
        raise ValueError("T6050 run_base staging requires all secondaries to be parked in reset")

    ba = u.ba
    ram_end = ba.phys_base + ba.mem_size
    base = u.base
    lower = u.mrs("CTXR_D_LWR_EL1")
    upper = u.mrs("CTXR_D_UPR_EL1")
    armed_end = (upper + 0x1000) & -PAGE_SIZE
    # Match ctxr_exec_find_window() in src/ctxr_exec.c.
    window_start = align(base + 0x1000000, 0x200000)
    window_end = window_start + 0x8000000
    if ((lower | upper) & 0xfff or base % PAGE_SIZE or
            not 0 < ba.phys_base <= lower <= base < base + footprint <= window_start or
            not window_end <= armed_end <= ba.top_of_kernel_data < ram_end < (1 << 64)):
        raise ValueError("Inherited T6050 CTXR_D range cannot hold Aura and the Linux window")

    adt_addr = ba.devtree - ba.virt_base + ba.phys_base
    if not (ba.phys_base <= adt_addr < adt_addr + ba.devtree_size <= base):
        raise ValueError("Inherited ADT is outside the protected area below Stage 1")
    mm = chosen["memory-map"]
    blobs, bootargs_off = find_boot_blobs(mm._properties, BOOT_BLOBS, 0)
    if not any(name == "SEPFW" for name, _, _, _ in blobs):
        raise ValueError("Inherited ADT has no SEPFW to preserve")
    for name, src, size, _ in blobs:
        if not ba.phys_base <= src < src + size <= ba.top_of_kernel_data:
            raise ValueError(f"Inherited {name} is outside the firmware RAM span")

    image.extend(b"\0" * 4)
    image_size = align(len(image))
    fw_size = bootargs_off + PAGE_SIZE
    packed_size = image_size + fw_size
    live_heap = p.heapblock_alloc(0)
    limit = min(u.heap_base, ram_end)
    if not ba.top_of_kernel_data <= live_heap <= limit:
        raise ValueError("Invalid Stage 1 heap bounds")
    if align(live_heap) + packed_size + PAGE_SIZE > limit:
        raise MemoryError("Stage 1 heap has insufficient room for the staged raw payload")

    staged = p.memalign(PAGE_SIZE, packed_size)
    staged_end = staged + packed_size
    if staged % PAGE_SIZE or not live_heap <= staged < staged_end <= limit:
        raise ValueError("Allocated T6050 staging span is outside the checked heap bounds")
    fw_base = staged + image_size
    bootargs_addr = fw_base + bootargs_off
    tba = BootArgs_r3.build(make_bootargs(staged_end))

    for name, _, size, off in blobs:
        mm._properties[name] = (fw_base + off, size)
        mm._types[name] = (Array(2, Int64ul), False)
    mm.BootArgs = (bootargs_addr, PAGE_SIZE)
    mm._types["BootArgs"] = (Array(2, Int64ul), False)
    adt = u.adt.build()
    if not 0 < len(adt) <= ba.devtree_size:
        raise ValueError("Updated ADT no longer fits in its inherited span")

    struct.pack_into("<Q", image, RUN_BASE_OFFSET, base)
    print(f"T6050 staging: raw [0x{staged:x}, 0x{staged + len(image):x}), "
          f"BootArgs 0x{bootargs_addr:x}, run_base 0x{base:x}")
    print(f"T6050 Linux window: [0x{window_start:x}, 0x{window_end:x})")
    u.compressed_writemem(staged, image, True)
    copy_boot_blobs(p, [(name, src, size, fw_base + off) for name, src, size, off in blobs])
    iface.writemem(bootargs_addr, tba)
    p.dc_cvac(staged, packed_size)
    p.ic_ivau(staged, footprint)
    header = image[LOAD_HEADER_OFFSET:LOAD_HEADER_OFFSET + LOAD_HEADER.size]
    if iface.readmem(staged + LOAD_HEADER_OFFSET, len(header)) != header:
        raise ValueError("Staged AURALOAD header did not verify")
    if iface.readmem(bootargs_addr, len(tba)) != tba:
        raise ValueError("Staged BootArgs did not verify")
    iface.writemem(adt_addr, adt)
    p.dc_cvac(adt_addr, len(adt))
    if args.quiet:
        p.iodev_set_usage(IODEV.FB, 0)
    vector(staged + RAW_ENTRY, bootargs_addr)


if args.t6050_run_base:
    stage_t6050(image, footprint)

new_base = u.base

if args.raw:
    image = args.payload.read_bytes()
    image += b"\x00\x00\x00\x00"
    entry = new_base + args.entry_point
else:
    macho = MachO(args.payload.read_bytes())
    image = macho.prepare_image()
    image += b"\x00\x00\x00\x00"
    entry = macho.entry
    entry -= macho.vmin
    entry += new_base

if args.quiet:
    p.iodev_set_usage(IODEV.FB, 0)

if args.no_sepfw:
    blob_names = tuple(name for name in BOOT_BLOBS if name != "SEPFW")
else:
    blob_names = BOOT_BLOBS

image_only_size = align(len(image))
# The boot blobs and the boot args form the firmware block, packed after the image or parked.
blobs, bootargs_off = find_boot_blobs(u.adt["chosen"]["memory-map"]._properties, blob_names, 0)
bootargs_size = 0x4000
fw_size = bootargs_off + bootargs_size
if args.relocate:
    if not args.raw or args.xnu:
        raise ValueError("Relocation requires a raw aura image")
    new_base = p.memalign(0x4000, image_only_size + fw_size)
    if not new_base:
        raise MemoryError("No space for relocated image")
    entry = new_base + args.entry_point
packed_end = new_base + image_only_size + fw_size

if not (u.ba.phys_base <= new_base < u.ba.phys_base + u.ba.mem_size):
    raise ValueError("Image base is outside inherited RAM")
ram_end = u.ba.phys_base + u.ba.mem_size
if ram_end > (1 << 64) or packed_end > ram_end or u.ba.top_of_kernel_data >= ram_end:
    raise ValueError("Packed chainload span is outside inherited RAM")

ro_start = ro_end = 0
if p.get_chipid() == 0x8140:
    ro_start = u.mrs("CTRR_C_LWR_EL2")
    upper = u.mrs("CTRR_C_UPR_EL2")
    ro_end = upper + 0x1000
    if ((ro_start | upper) & 0xfff or ro_end - ro_start != 0xc000 or
            not (u.ba.phys_base <= ro_start < ro_end <= ram_end) or
            not (u.ba.phys_base <= u.ba.top_of_kernel_data < ram_end)):
        raise ValueError("Invalid T8140 firmware RO range")

live_heap = p.heapblock_alloc(0)
inherited_top = u.ba.top_of_kernel_data
image_end = new_base + image_only_size
if ro_end and image_end > ro_start and new_base < ro_end:
    raise ValueError("Image itself overlaps firmware RO memory")
if not args.relocate and live_heap > inherited_top and image_end > inherited_top and new_base < live_heap:
    raise ValueError("Image overlaps live Stage 1 heap")

park_fw = not args.relocate and ((ro_end and packed_end > ro_start and image_end < ro_end) or
           (live_heap > inherited_top and packed_end > inherited_top and image_end < live_heap))
fw_dest = new_base + image_only_size
if park_fw:
    print("Packed span would overlap protected/live memory; parking firmware block")
    fw_dest = p.memalign(0x4000, fw_size)
    if not fw_dest or fw_dest + fw_size > ram_end:
        raise ValueError("No safe location for firmware block")
    if fw_dest < live_heap or (ro_end and fw_dest < ro_end and fw_dest + fw_size > ro_start):
        raise ValueError("Parked firmware block overlaps protected/live memory")

copy_size = image_only_size if park_fw else image_only_size + fw_size
print(f"Copy span: 0x{copy_size:x} bytes; firmware block: 0x{fw_dest:x}")
image_addr = p.memalign(0x4000, copy_size + 0x4000)
if not image_addr:
    raise MemoryError("No aligned staging block")
fw_stage = fw_dest if park_fw else image_addr + image_only_size

copy_boot_blobs(p, [(name, src, size, fw_stage + off) for name, src, size, off in blobs])

print(f"Loading kernel image (0x{len(image):x} bytes)...")
u.compressed_writemem(image_addr, image, True)
p.dc_cvac(image_addr, len(image))

print("Adjusting addresses in ADT...")
for name, src, size, off in blobs:
    mm = u.adt["chosen"]["memory-map"]
    mm._properties[name] = (fw_dest + off, size)
    mm._types[name] = (Array(2, Int64ul), False)
u.adt["chosen"]["memory-map"].BootArgs = (fw_dest + bootargs_off, bootargs_size)

if args.xnu:
    def remove_oslog(node):
        names = node.segment_names.split(";")
        try:
            idx = names.index("__OS_LOG")
        except ValueError:
            return
        print(f"Removing __OS_LOG from {node.name}")
        names = names[:idx] + names[idx + 1:]
        node.segment_names = ";".join(names)
        node.segment_ranges = node.segment_ranges[:idx * 32] + node.segment_ranges[32 + idx * 32: ]

    for node in u.adt["/arm-io"]:
        if hasattr(node, "segment_names"):
            remove_oslog(node)

        for nub in node:
            if hasattr(nub, "segment_names"):
                remove_oslog(nub)

rvbar = entry & ~0xfff
if rvbar != u.base:
    print("Setting secondary CPU RVBARs...")

    for cpu in u.adt["cpus"]:
        if cpu.state == "running":
            continue
        if u.adt["/chosen"].chip_id == 0x8142:
            # Host writes to a cold T8142 secondary's RVBAR can wedge CPU0.
            # Leave the cold cores and their reset vectors to the next stage.
            print(f"  {cpu.name}: preserving T8142 secondary RVBAR")
            continue
        addr, size = cpu.cpu_impl_reg
        if p.read64(addr) & 1:
            print(f"  {cpu.name}: RVBAR locked, leaving it unchanged")
            continue
        print(f"  {cpu.name}: [0x{addr:x}] = 0x{rvbar:x}")
        p.write64(addr, rvbar)

u.push_adt()

print("Setting up bootargs...")
tba = make_bootargs(max(image_end, fw_dest + fw_size, live_heap, inherited_top, ro_end))

if args.xnu:
    # Fix virt_base, since we often install aura with it set to 0 which xnu does not like
    tba.virt_base = 0xfffffe0010000000 + (tba.phys_base & (32 * 1024 * 1024 - 1))
    tba.devtree = u.ba.devtree - u.ba.virt_base + tba.virt_base

if tba.revision <= 1:
    iface.writemem(fw_stage + bootargs_off, BootArgs_r1.build(tba))
elif tba.revision == 2:
    iface.writemem(fw_stage + bootargs_off, BootArgs_r2.build(tba))
elif tba.revision == 3:
    iface.writemem(fw_stage + bootargs_off, BootArgs_r3.build(tba))

p.dc_cvac(fw_stage, fw_size)

print(f"Copying stub...")

stub = asm.ARMAsm(f"""
        mov x7, x2
        mov x8, x3
1:
        ldp x4, x5, [x1], #16
        stp x4, x5, [x2]
        add x2, x2, #16
        subs x3, x3, #16
        b.ne 1b

        mov x2, x7
        mov x3, x8
2:
        dc cvac, x2
        add x2, x2, #64
        subs x3, x3, #64
        b.ne 2b
        dsb sy

        mov x2, x7
        mov x3, x8
3:
        ic ivau, x2
        add x2, x2, #64
        subs x3, x3, #64
        b.ne 3b
        dsb sy
        isb
        ldr x1, ={entry}
        br x1
""", image_addr + copy_size)

iface.writemem(stub.addr, stub.data)
p.dc_cvac(stub.addr, stub.len)
p.ic_ivau(stub.addr, stub.len)

print(f"Entry point: 0x{entry:x}")

if args.xnu and p.display_is_external():
    if p.display_start_dcp() >= 0:
        p.display_shutdown(0)

if args.call:
    print(f"Shutting down MMU...")
    try:
        p.mmu_shutdown()
    except ProxyCommandError:
        pass
    print(f"Jumping to stub at 0x{stub.addr:x}")
    p.call(stub.addr, fw_dest + bootargs_off, image_addr, new_base, copy_size, reboot=True)
elif args.vector_only:
    vector(stub.addr, fw_dest + bootargs_off, image_addr, new_base, copy_size)
else:
    print(f"Reloading into stub at 0x{stub.addr:x}")
    p.reload(stub.addr, fw_dest + bootargs_off, image_addr, new_base, copy_size)

iface.nop()
print("Proxy is alive again")
