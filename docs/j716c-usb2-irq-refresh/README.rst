J716c USB handoff and upstream IRQ refresh
=========================================

This source snapshot retains the qualified J716c USB0 retirement-contract2,
GPU/USB4 handoff and guarded RAM chainload work, and imports two upstream USB
fixes: a136855 selects CONTROL_STATUS3 for HS three-stage control transfers;
bdc0e09 decodes all four DWC command-status bits. It backports170e23f to the
four M5 Pro/Max MIDR entries present in this baseline, writing SIQ route0 on
every initialized Sotra core while preserving the one-byte CPU feature ABI.
New Ultra CPU definitions are not imported.

Independent CurrentEL2-guarded reads on all eighteen J716c CPUs establish the
change: USB-fixed control has route0 on CPU0 and route3 on CPUs1-17; the M5
backport has route0 on every CPU. Actual AIC2 hardware IRQ delivery changes
from CPU0 alone to eight observed CPUs in the first pair. The subsequent
Linux reporting/data trial observes twelve CPUs. Acceptance configuration
is distinguished from actual delivery; every CPU's delivery is not claimed.

Both USB-fixed and IRQ-fixed variants boot the matched Linux RAM fixture and
complete direct configuration and host ACM activation controls. The real10B
notification still stalls; full host/all18 CPU traces, cached coherent-RAM
rings, physical watchdog recovery and held-TTY/rule cleanup pass. These Aura
changes do not repair the remaining ACM notification failure. The separate
Linux fourteen-line T6050 effective-affinity patch and guarded serial data
validation are published at cameroncooper/linux, j716c/aic-effective-affinity.
Diagnostic connect-notification suppression still permits4096+65536 payload
bytes each way; it is not standard notification support.

The original control rebuild is byte-identical to raw07594a9f. The upstream
USB changes initially crossed the existing0x120000 DCP vital-file bound.
Both compared variants then compile only usb.o/usb_dwc3_hs.o/usb_dwc3_ss.o at
-Oz. The bound remains unchanged and the IRQ image vital end is0x11b878.
The Rust archive and original four build-role definitions remain exact.

The tested raw SHA256 is bbc4a007acb43e0afecd01847d0cf034d00bc9b660a804dfe60a1895749cdfe3,
4063232 bytes. Source base is bd9f3e8273dc79ae9095c19c2c4f32218446ad4e plus
local qualified handoff work; the838 copied source files are byte-identical
to the tested snapshot. Adjacent source/build pins and curated bounded
results record the comparisons. Runtime binaries predate this publication
commit; a new-version-tag binary rebuild is not claimed.

Build with the pinned Rust1.96.1/aarch64-unknown-none-softfloat target and
clang/lld22, USE_CLANG=1 ARCH=aarch64-linux-gnu RELEASE=1 CHAINLOADING=1
ESP_STAGE2=1 DEBUGUSB_PROXY=1. The tested host chainloader is unchanged.
Whole latest Aura, newer NVRAM failed-boot handling and DART lifetime/API
changes are not imported. Trials preserve the independent physical watchdog
and both held TTY FDs through reset. No SSD/ESP/install/enrollment/NVRAM or
boot-policy write is part of qualification. Private firmware/ADT/key/identity
material, raw addresses and captures are excluded. Other boards, full
notification support, hotplug, suspend and sustained reliability are unqualified.
