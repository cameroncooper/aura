# J716c USB0 dual-DART handoff

The restricted USB2 RAM profile mapped bank1/SID1. Normal Linux EEM traffic
also reaches bank0/SID0, whose invalid root produced an unhandled shared
fault IRQ. Native T6050 DTS names both banks for DWC0. The companion kernel
now maps both after this new, explicit retirement transaction.

Use existing `j716c,usb0-handoff=<1>` and exact u32 chosen flags
`j716c,usb0-dual-retire=<1>` plus `j716c,usb0-bank0-map=<0|1>`.
Flags absent preserve contract2. Static input keeps bank0 disabled/native and
single-bank1 IOMMU wiring. All flags/resources/firmware/power/IRQ metadata and
late DT capacity are validated before MMIO; changed late trees still refuse.

The existing stopped-device/SID1 retirement supplies a one-shot full-wrapper
reset receipt. With the controller clamped and held in reset, the new bank0
path requires the exact observed layout, no protection/enabled streams/masks,
no faults and drained counters. It changes only SID0 TCR/root and performs a
bounded SID0 flush. Every foreign raw TCR/root and control word must remain
unchanged. No raw roots are logged. Failure never publishes ownership.

Contract3 is published only after both retirements succeed. Map0 is a
retirement-only control. Map1 prepares the exact bank0 compatible/status,
allocates its phandle if absent and installs ordered bank0/SID0 plus
bank1/SID1 IOMMU references. All late-tree allocation occurs before MMIO.
New run-once functions occupy permanent executable Aura `.init`, which
remains mapped through finish before MMU shutdown. Linker scripts and DCP
vital end0x11b878/limit0x120000 remain unchanged; raw size4063232bytes.

Five RAM boots with the companion99b3 kernel cover old contract2 and
counterbalanced map0/map1/map0/map1. Map0 stalls twice despite quiet faults;
map1 passes4456448bytes/128frames each direction twice,4978/4938 normal IN
requests and19ringwraps per run, pools10 and normalTRB811 with no LST workaround.
Both owned roots match and remain valid; no faults/capture loss/emptyIN/USB
completion errors occur. All five physically recover with held native120s
watchdog leases and scoped cleanup. No persistent disk/ESP/NVRAM or boot-policy
write was used; the original recovery baseline stays unchanged.

Raw SHA256: `7b68e97701d1b63098a961dc41aef560e037af3f7fe9704e1c337a451d5188ae`.
All838 prepublication source files match tested source. Compiler22.1.8,
zero warnings; the build uses:

```sh
AURA_VERSION_TAG=1.0.0b10-gbd9f3e82-dirty make -j4 USE_CLANG=1 RELEASE=1 CHAINLOADING=1 ESP_STAGE2=1 DEBUGUSB_PROXY=1 build/aura.bin
```

A publication-tag rebuild is not claimed unless the same build inputs are
used. Complete kernel/configuration/runtime pins and observations are on
`cameroncooper/linux`, branch `j716c/usb2-dual-dart`, under
`Documentation/usb/j716c-usb2-dual-dart*`. This is firmware-specific guarded
RAM bring-up, not native USB4/host/hotplug/suspend or installer qualification.
It retains the prior selected USB fixes/all-core routing/clock42. The broader
upstream reserve-and-boot-on-failed-retirement policy is not imported. Real
ACM notification remains a separate failing investigation.
