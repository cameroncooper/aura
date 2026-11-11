# J716c recovery review sources

This topic adds only existing public helper/fixture source to the exact
configured-recovery production tree. No private input catalogs, unit ADT,
BootArgs, configuration blocks, real PARTUUID, keys, captures, firmware,
initramfs, build products or runtime result JSON is included.

The seven files in j716c-stage1-active-controller-wfi are byte-preserved
exports from the root-reviewed workspace controller of that name. They
retain their original source author/provenance through this reconstruction;
the upstream base history is preserved and the new reconstruction commits
are authored by Cameron Cooper. No upstream commit attribution is invented
for previously uncommitted working-tree additions.

The two test files are pre-existing synthetic offline cases, written before
the hardware controller implementation. They can run here without a target:

    PYTHONDONTWRITEBYTECODE=1 python3 review/j716c-stage1-active-controller-wfi/test_active_offline.py
    PYTHONDONTWRITEBYTECODE=1 python3 review/j716c-stage1-active-controller-wfi/test_passive_offline.py

These check bounded packet authorization, one explicit30s watchdog API arm,
identity/configuration prerequisites, durable intent, partial-send sealing,
read bounds, passive PTY behavior and evidence classification. They are
source-helper tests, not hardware coverage or proof of a particular reset
cause. No new unit cases were written during publication reconstruction.

The production controller/loader/probe sources intentionally retain their
original **workspace path assumptions**: ROOT/build artifacts, scripts,
virtual environment, systemd user unit and inputs.json/catalogs. Those
private catalogs are omitted, and no runtime wiring is created here. Do
not run these exported hardware helpers from this checkout. Their path
relocation is review-only and has no new hardware qualification; they are
not presented as a standalone deployment tool.

j716c-stage1-v3/check-wdt.py is the byte-preserved pre-existing production-
helper source-integrated ADT/MMIO fixture generator. Its default source and
output locations also refer to the original workspace. It checks exact
board/version/resources, seconds, fault points and readbacks using synthetic
metadata only. No generated C/executable or raw unit data is committed.

Qualification of the original frozen recovery path is deliberately narrow:

- The RAM wrong-PARTUUID fixture reached PartitionNotFound and observed USB
  fallback. Its natural160s lease-consistent expiry and baseline recovery
  were observed without post-vector proxy transmission. Passive USB control
  effects are possible; this was not a passive hardware read-only trial.
- A separate bounded post-lookup session verified the proxy/configuration,
  issued the permitted explicit30s API arm and observed counter readbacks,
  final transmit sealing, lease-consistent reset and baseline recovery.
  That is distinct from the natural160s boot expiry result.
- NVMe/ANS/RTKit initialization/shutdown changes hardware ownership. The
  negative fixture reached no namespace/FAT write; that does not mean the
  whole probe performed no MMIO/firmware operation.
- Native Mach-O enrollment, a real FAT payload, native iBoot watchdog
  inheritance and secondary-CPU disk chainload remain unqualified. No
  hardware fault-injection/partial-wire test or reset-cause read was done.

Production refs deliberately stay separate: j716c/bringup-27.2.1 is the
graphics raw Stage2 lineage; j716c/stage1-usb-proxy is the factory USB-only
Stage1 lineage; j716c/stage1-configured-recovery is the exact recovery source.
This fixture topic adds review sources above the latter and does not merge
those roles or change the active boot setup. New Git commit/build tags can
change rebuilt binary bytes; source equality is not a new binary/runtime
qualification.
