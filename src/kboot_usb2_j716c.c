/* SPDX-License-Identifier: MIT */
/* Explicit USB0 retirement transaction; metadata preparation performs no MMIO. */
#include <malloc.h>
#include <string.h>
#include "kboot_usb2_j716c.h"
#include "adt.h"
#include "firmware.h"
#include "memory.h"
#include "pmgr.h"
#include "libfdt/libfdt.h"
#include "tinf/tinf.h"
#include "usb.h"
#include "utils.h"

#define OPT "j716c,usb0-handoff"
#define STATUS "j716c,usb0-handoff-status"
#define SNAPSHOT "j716c,usb0-pmgr-snapshot"
#define LATE_SAME "j716c,usb0-late-tree-unchanged"
#define STATE "j716c,usb0-state-snapshot"
#define CONTRACT "j716c,usb0-retirement-contract"
#define DUAL_RETIRE "j716c,usb0-dual-retire"
#define BANK0_MAP "j716c,usb0-bank0-map"
#define BUS "/soc@200000000"
#define DART0 BUS "/iommu@2a2f00000"
#define FN __attribute__((minsize, noinline))
/* The existing run-once .init slot avoids growing the aligned .text image. */
#define DUAL_FN FN __attribute__((section(".init.usb0_dual")))
enum { PHY, DWC, DART, COMMON, AON, USB, WRAP, NODES };
static const char *const paths[NODES] = {
    BUS "/phy@2a2a90000", BUS "/usb@2a2280000", BUS "/iommu@2a2f80000",
    BUS "/power-management@280600000/power-controller@208",
    BUS "/power-management@288300000/power-controller@148",
    BUS "/power-management@288300000/power-controller@150",
    BUS "/power-management@288300000/power-controller@158",
};
static void *armed_tree, *prepared_tree;
static size_t armed_capacity, armed_size;
static u32 armed_crc;
static u32 inspection_mode;
static bool dual_retire;
static u32 bank0_map;

void kboot_usb2_j716c_reset(void)
{
    free(prepared_tree);
    prepared_tree = armed_tree = NULL;
    armed_capacity = armed_size = 0;
    inspection_mode = false;
    dual_retire = false;
    bank0_map = 0;
}
static FN int anode(const char *path) { return adt_path_offset(adt, path); }
static FN int number(int node, const char *name, u64 *out)
{
    u32 len;
    const void *p = adt_getprop(adt, node, name, &len);
    if (!p || (len != 4 && len != 8))
        return -1;
    *out = 0;
    memcpy(out, p, len);
    return 0;
}
static FN bool astr(int node, const char *name, const char *want)
{
    u32 len;
    const char *p = adt_getprop(adt, node, name, &len);
    size_t n = strlen(want) + 1;
    if (!p || len < n || len > 512 || memcmp(p, want, n))
        return false;
    for (u32 i = n; i < len; i++)
        if (p[i])
            return false;
    return true;
}
static FN int identity(void *tree)
{
    int chosen = anode("/chosen"), root = anode("/");
    u64 value;
    if (chip_id != T6050 || root < 0 || chosen < 0 ||
        fdt_node_check_compatible(tree, 0, "apple,j716c") ||
        fdt_node_check_compatible(tree, 0, "apple,t6050") ||
        !astr(root, "model", "Mac17,6") || !astr(root, "target-type", "J716c") ||
        !adt_is_compatible(adt, root, "J716cAP") ||
        !astr(chosen, "firmware-version", "mBoot-20457.41.3") ||
        !os_firmware.iboot || strcmp(os_firmware.iboot, "mBoot-20457.41.3"))
        return -1;
    const char *const ids[] = {"chip-id", "board-id"};
    const u64 expected[] = {T6050, 0xe};
    for (unsigned i = 0; i < 2; i++) {
        bool found = false;
        for (unsigned j = 0; j < 2; j++) {
            int n = j ? root : chosen;
            if (!adt_getprop(adt, n, ids[i], NULL))
                continue;
            if (number(n, ids[i], &value) || value != expected[i])
                return -1;
            found = true;
        }
        if (!found)
            return -1;
    }
    return 0;
}
static FN int areg(const char *path, int index, u64 base, u64 size)
{
    int trace[8];
    u64 b, s;
    return adt_path_offset_trace(adt, path, trace) < 0 ||
                   adt_get_reg(adt, trace, "reg", index, &b, &s) || b != base || s != size
               ? -1 : 0;
}
static FN int dbytes(void *tree, int node, const char *prop, const void *want, size_t size)
{
    int len;
    const void *p = fdt_getprop(tree, node, prop, &len);
    return !p || len < 0 || (size_t)len != size || memcmp(p, want, size) ? -1 : 0;
}
static FN int dreg(void *tree, int n, const u64 *pairs, unsigned count)
{
    fdt64_t data[8];
    if (count > 4)
        return -1;
    for (unsigned i = 0; i < count * 2; i++)
        data[i] = cpu_to_fdt64(pairs[i]);
    return dbytes(tree, n, "reg", data, count * 16);
}
static FN int reference(void *tree, int n, const char *prop, int target, bool argument, u32 arg)
{
    u32 handle = fdt_get_phandle(tree, target);
    fdt32_t cells[] = {cpu_to_fdt32(handle), cpu_to_fdt32(arg)};
    return !handle || dbytes(tree, n, prop, cells, argument ? 8 : 4) ? -1 : 0;
}
static FN int tunable(void *tree, int phy, const char *src, const char *dst,
                     const u32 *expected, unsigned count)
{
    u32 len;
    int n = anode("/arm-io/atc-phy0");
    const u32 *p = adt_getprop(adt, n, src, &len);
    fdt32_t data[6];
    if (!p || count > 2 || len != count * 12)
        return -1;
    for (unsigned i = 0; i < count; i++) {
        if (p[i * 3] != (0x20000000 | expected[i * 3]) ||
            p[i * 3 + 1] != expected[i * 3 + 1] || p[i * 3 + 2] != expected[i * 3 + 2])
            return -1;
        for (unsigned j = 0; j < 3; j++)
            data[i * 3 + j] = cpu_to_fdt32(expected[i * 3 + j]);
    }
    /* Source seeds may be present, but a changed seed must never be silently replaced. */
    if (dbytes(tree, phy, dst, data, count * 12))
        return -1;
    return fdt_setprop(tree, phy, dst, data, count * 12);
}
/* The two FAB parents have no physical state register on this unit.
 * Verify that exact virtual chain before flattening it out of Linux. */
static FN int power_metadata(void)
{
    static const struct { u16 id, parent0, parent1; bool virt; u64 address; const char *name; } gates[] = {
        {49, 0, 0, true, 0, "FAB0_PIOGW"},
        {50, 49, 0, true, 0, "FAB3_SOC"},
        {55, 50, 0, false, 0x280600208ULL, "ATC0_COMMON"},
        {260, 0, 0, false, 0x288300148ULL, "ATC0_USB_AON"},
        {261, 55, 260, false, 0x288300150ULL, "ATC0_USB"},
        {262, 55, 260, false, 0x288300158ULL, "ATC0_PHYMXWRAP"},
    };
    for (unsigned i = 0; i < sizeof(gates) / sizeof(gates[0]); i++) {
        struct pmgr_gate gate = {0};
        if (pmgr_device_gate(0, gates[i].id, &gate) || !gate.found || gate.die ||
            gate.id != gates[i].id || !gate.name || strncmp(gate.name, gates[i].name, 16) ||
            gate.virt != gates[i].virt || gate.psreg != gates[i].address ||
            gate.parent[0] != gates[i].parent0 || gate.parent[1] != gates[i].parent1)
            return -1;
    }
    return 0;
}
static FN int real_parent_pair(void *tree, int node, int common, int aon)
{
    u32 c = fdt_get_phandle(tree, common), a = fdt_get_phandle(tree, aon);
    fdt32_t pair[] = {cpu_to_fdt32(c), cpu_to_fdt32(a)};
    return !c || !a || dbytes(tree, node, "power-domains", pair, sizeof(pair)) ? -1 : 0;
}
static FN int prepare(void *tree)
{
    static const u64 phy_regs[] = {0x2a2a90000ULL, 0x4000, 0x2a2800000ULL, 0x4000,
                                    0x2a2a84000ULL, 0x4000};
    static const u64 dwc_regs[] = {0x2a2280000ULL, 0xcd00};
    static const u64 dart_regs[] = {0x2a2f80000ULL, 0xc000};
    static const u32 defaults[] = {8, 0x7003, 3, 0x18, 0x703, 0x103};
    static const u32 device[] = {8, 0x7003, 3};
    static const char *const compat[] = {"apple,j716c-usb2-device-phy",
        "apple,j716c-usb2-device-dwc3", "apple,j716c-usb2-device-dart"};
    static const char *const labels[] = {"atc0-common", "atc0-usb-aon", "atc0-usb", "atc0-phymxwrap"};
    int n[NODES];
    if (identity(tree) || power_metadata())
        return -1;
    int bus = fdt_path_offset(tree, BUS);
    const fdt32_t ranges[] = {cpu_to_fdt32(2), 0, cpu_to_fdt32(2), 0,
                             cpu_to_fdt32(3), cpu_to_fdt32(0xc0000000)};
    if (bus < 0 || fdt_address_cells(tree, bus) != 2 || fdt_size_cells(tree, bus) != 2 ||
        dbytes(tree, bus, "ranges", ranges, sizeof(ranges)))
        return -1;
    for (unsigned i = 0; i < NODES; i++) {
        n[i] = fdt_path_offset(tree, paths[i]);
        if (n[i] < 0 || dbytes(tree, n[i], "status", "disabled", sizeof("disabled")))
            return -1;
        if (i < COMMON ? fdt_node_check_compatible(tree, n[i], compat[i]) :
            dbytes(tree, n[i], "label", labels[i - COMMON], strlen(labels[i - COMMON]) + 1))
            return -1;
    }
    const char *const phantom[] = {
        BUS "/power-management@280600000/power-controller@1e0",
        BUS "/power-management@280600000/power-controller@1d8",
    };
    for (unsigned i = 0; i < 2; i++) {
        int v = fdt_path_offset(tree, phantom[i]);
        if (v < 0 || dbytes(tree, v, "status", "disabled", sizeof("disabled")))
            return -1;
    }
    int dart0 = fdt_path_offset(tree, BUS "/iommu@2a2f00000");
    if (dart0 < 0 || dbytes(tree, dart0, "status", "disabled", sizeof("disabled")) ||
        dreg(tree, n[PHY], phy_regs, 3) || dreg(tree, n[DWC], dwc_regs, 1) ||
        dreg(tree, n[DART], dart_regs, 1) ||
        reference(tree, n[DWC], "iommus", n[DART], true, 1) ||
        reference(tree, n[DWC], "phys", n[PHY], false, 0) ||
        reference(tree, n[DWC], "resets", n[PHY], false, 0) ||
        reference(tree, n[DWC], "power-domains", n[USB], false, 0) ||
        reference(tree, n[DART], "power-domains", n[USB], false, 0) ||
        real_parent_pair(tree, n[PHY], n[WRAP], n[USB]) ||
        real_parent_pair(tree, n[USB], n[COMMON], n[AON]) ||
        real_parent_pair(tree, n[WRAP], n[COMMON], n[AON]) ||
        fdt_getprop(tree, n[COMMON], "power-domains", NULL) ||
        fdt_getprop(tree, n[AON], "power-domains", NULL) ||
        dbytes(tree, n[DWC], "dr_mode", "peripheral", sizeof("peripheral")) ||
        dbytes(tree, n[DWC], "maximum-speed", "high-speed", sizeof("high-speed")))
        return -1;
    int dart = anode("/arm-io/dart-usb0");
    if (dart < 0 || !adt_is_compatible(adt, dart, "dart,t8110"))
        return -1;
    u64 sids;
    if (number(dart, "sid-count", &sids) || sids != 16)
        return -1;
    int phy = anode("/arm-io/atc-phy0"), drd = anode("/arm-io/usb-drd0"),
        hpm = anode("/arm-io/nub-spmi-a0/hpm0"), mapper = anode("/arm-io/dart-usb0/mapper-usb0");
    u64 value, handle;
    if (phy < 0 || drd < 0 || hpm < 0 || mapper < 0 ||
        !adt_is_compatible(adt, phy, "atc-phy,t6050") ||
        !adt_is_compatible(adt, drd, "usb-drd,t6050") ||
        !adt_is_compatible(adt, hpm, "usbc,sn201202x,spmi") ||
        adt_getprop(adt, drd, "usb-device-disable", NULL) ||
        !astr(hpm, "port-location", "left-back") ||
        number(mapper, "reg", &value) || value != 1 ||
        number(phy, "AAPL,phandle", &handle) || number(drd, "atc-phy-parent", &value) ||
        handle != value)
        return -1;
    for (unsigned i = 0; i < 3; i++) {
        int source = i == 0 ? phy : i == 1 ? drd : hpm;
        if (number(source, "port-number", &value) || value != 1 ||
            number(source, "port-type", &value) || value != 2)
            return -1;
    }
    if (areg("/arm-io/atc-phy0", 0, phy_regs[0], phy_regs[1]) ||
        areg("/arm-io/atc-phy0", 1, phy_regs[2], phy_regs[3]) ||
        areg("/arm-io/usb-drd0", 3, phy_regs[4], phy_regs[5]) ||
        areg("/arm-io/usb-drd0", 0, dwc_regs[0], 0x11800) ||
        areg("/arm-io/dart-usb0", 1, dart_regs[0], dart_regs[1]) ||
        tunable(tree, n[PHY], "tunable_USB2PHY_DFLT", "apple,tunable-usb2-default", defaults, 2) ||
        tunable(tree, n[PHY], "tunable_USB2PHY_DEV", "apple,tunable-usb2-device", device, 1))
        return -1;
    return 0;
}
static DUAL_FN u32 interrupt_parent(void *tree, int node)
{
    for (; node >= 0; node = fdt_parent_offset(tree, node)) {
        int len;
        const fdt32_t *p = fdt_getprop(tree, node, "interrupt-parent", &len);
        if (p) {
            u32 handle = len == 4 ? fdt32_ld(p) : 0;
            return handle && fdt_node_offset_by_phandle(tree, handle) >= 0 ? handle : 0;
        }
    }
    return 0;
}
/* Dual admission starts from the same static disabled/single-bank tree.
 * Check the second bank and shared resources before any target MMIO. */
static DUAL_FN int prepare_dual(void *tree)
{
    static const u64 regs[] = {0x2a2f00000ULL, 0xc000};
    static const char native[] = "apple,t6050-dart\0apple,t8110-dart";
    static const char bank1[] = "apple,j716c-usb2-device-dart\0apple,t8110-dart";
    const fdt32_t irq[] = {0, 0, cpu_to_fdt32(1423), cpu_to_fdt32(4)};
    const fdt32_t cells = cpu_to_fdt32(1);
    int d0 = fdt_path_offset(tree, DART0), d1 = fdt_path_offset(tree, paths[DART]);
    int usb = fdt_path_offset(tree, paths[USB]);
    u32 parent = interrupt_parent(tree, d1);
    if (d0 < 0 || d1 < 0 || usb < 0 || !parent || interrupt_parent(tree, d0) != parent ||
        dreg(tree, d0, regs, 1) || areg("/arm-io/dart-usb0", 0, regs[0], regs[1]) ||
        dbytes(tree, d0, "status", "disabled", sizeof("disabled")) ||
        dbytes(tree, d0, "compatible", native, sizeof(native)) ||
        dbytes(tree, d1, "compatible", bank1, sizeof(bank1)) ||
        dbytes(tree, d0, "#iommu-cells", &cells, sizeof(cells)) ||
        dbytes(tree, d1, "#iommu-cells", &cells, sizeof(cells)) ||
        dbytes(tree, d0, "interrupts", irq, sizeof(irq)) ||
        dbytes(tree, d1, "interrupts", irq, sizeof(irq)) ||
        fdt_getprop(tree, d0, "interrupts-extended", NULL) ||
        fdt_getprop(tree, d1, "interrupts-extended", NULL) ||
        reference(tree, d0, "power-domains", usb, false, 0))
        return -1;
    return 0;
}
static DUAL_FN int prepare_bank0_map(void *tree)
{
    static const char compat[] = "apple,j716c-usb2-device-dart0\0apple,t8110-dart";
    int d0 = fdt_path_offset(tree, DART0), d1 = fdt_path_offset(tree, paths[DART]);
    u32 h0 = fdt_get_phandle(tree, d0), h1 = fdt_get_phandle(tree, d1);
    if (!h0 && (fdt_generate_phandle(tree, &h0) || fdt_setprop_u32(tree, d0, "phandle", h0)))
        return -1;
    const fdt32_t iommus[] = {cpu_to_fdt32(h0), 0, cpu_to_fdt32(h1), cpu_to_fdt32(1)};
    if (!h0 || !h1 || h0 == h1 ||
        fdt_setprop_string(tree, fdt_path_offset(tree, DART0), "status", "okay") ||
        fdt_setprop(tree, fdt_path_offset(tree, DART0), "compatible", compat, sizeof(compat)) ||
        fdt_setprop(tree, fdt_path_offset(tree, paths[DWC]), "iommus", iommus, sizeof(iommus)))
        return -1;
    return 0;
}
int kboot_usb2_j716c_prepare(void *tree, size_t capacity)
{
    kboot_usb2_j716c_reset();
    int chosen = fdt_path_offset(tree, "/chosen"), len, dual_len, map_len;
    const fdt32_t *opt = chosen < 0 ? NULL : fdt_getprop(tree, chosen, OPT, &len);
    const fdt32_t *dual = chosen < 0 ? NULL : fdt_getprop(tree, chosen, DUAL_RETIRE, &dual_len);
    const fdt32_t *map = chosen < 0 ? NULL : fdt_getprop(tree, chosen, BANK0_MAP, &map_len);
    bool requested = dual || map;
    if (requested && (!opt || len != 4 || fdt32_ld(opt) != 1 ||
                      !dual || dual_len != 4 || fdt32_ld(dual) != 1 ||
                      !map || map_len != 4 || fdt32_ld(map) > 1))
        return -1;
    if (!opt)
        return 0;
    if (len != 4 || (fdt32_ld(opt) != 1 && fdt32_ld(opt) != 2 && fdt32_ld(opt) != 3 && fdt32_ld(opt) != 4 && fdt32_ld(opt) != 5) ||
        fdt_getprop(tree, chosen, STATUS, NULL) || fdt_getprop(tree, chosen, CONTRACT, NULL) || capacity < fdt_totalsize(tree))
        return -1;
    u32 mode = fdt32_ld(opt);
    u32 mapping = requested ? fdt32_ld(map) : 0;
    void *scratch = malloc(capacity);
    if (!scratch || fdt_open_into(tree, scratch, capacity) || prepare(scratch) ||
        (requested && prepare_dual(scratch))) {
        free(scratch);
        return -1;
    }
    chosen = fdt_path_offset(scratch, "/chosen");
    if (fdt_setprop_string(scratch, chosen, STATUS,
                          mode == 2 ? "validated-metadata-only" : "prepared") || fdt_pack(scratch)) {
        free(scratch);
        return -1;
    }
    memcpy(tree, scratch, fdt_totalsize(scratch));
    if (mode == 2) {
        free(scratch);
        return 0;
    }
    /* Prepare the entire post-retirement tree before any target register access. */
    if (fdt_open_into(tree, scratch, capacity)) {
        free(scratch);
        return -1;
    }
    for (unsigned i = 0; mode == 1 && i < NODES; i++)
        if (fdt_setprop_string(scratch, fdt_path_offset(scratch, paths[i]), "status", "okay")) {
            free(scratch);
            return -1;
        }
    if (requested && mapping && prepare_bank0_map(scratch)) {
        free(scratch);
        return -1;
    }
    if (fdt_setprop_string(scratch, fdt_path_offset(scratch, "/chosen"), STATUS, mode == 5 ? "inspected-usb-state" : mode == 4 ? "inspected-pmgr-only" : mode == 3 ? "retired-only" : "retired") ||
        fdt_pack(scratch)) {
        free(scratch);
        return -1;
    }
    if (mode == 1 || mode == 3) {
        if (fdt_open_into(scratch, scratch, capacity) ||
            fdt_setprop_u32(scratch, fdt_path_offset(scratch, "/chosen"), CONTRACT, requested ? 3 : 2) ||
            fdt_pack(scratch)) {
            free(scratch);
            return -1;
        }
    }
    if (mode >= 4) {
        char empty[512] = {0};
        if (fdt_open_into(scratch, scratch, capacity) ||
            fdt_setprop(scratch, fdt_path_offset(scratch, "/chosen"), mode == 5 ? STATE : SNAPSHOT, empty, sizeof(empty)) ||
            fdt_setprop_u32(scratch, fdt_path_offset(scratch, "/chosen"), LATE_SAME, 0) ||
            fdt_pack(scratch)) {
            free(scratch);
            return -1;
        }
        inspection_mode = mode;
    }
    armed_tree = tree;
    prepared_tree = scratch;
    armed_capacity = capacity;
    armed_size = fdt_totalsize(tree);
    armed_crc = tinf_crc32(tree, armed_size);
    dual_retire = requested;
    bank0_map = mapping;
    return 0;
}
int kboot_usb2_j716c_finish(void *tree)
{
    if (!prepared_tree)
        return 0;
    if (tree != armed_tree || fdt_check_header(tree) || fdt_totalsize(tree) > armed_capacity ||
        fdt_totalsize(prepared_tree) > armed_capacity)
        return -1;
    bool unchanged = fdt_totalsize(tree) == armed_size && tinf_crc32(tree, armed_size) == armed_crc;
    if (!unchanged && !inspection_mode)
        return -1;
    if (inspection_mode) {
        /* Diagnostic mode owns no device. Revalidate a changed current tree
         * and retain its unrelated late fields; active modes still refuse. */
        if (!unchanged) {
            char empty[512] = {0};
            if (fdt_open_into(tree, prepared_tree, armed_capacity) || prepare(prepared_tree) ||
                fdt_setprop_string(prepared_tree, fdt_path_offset(prepared_tree, "/chosen"),
                                   STATUS, inspection_mode == 5 ? "inspected-usb-state" : "inspected-pmgr-only") ||
                fdt_setprop(prepared_tree, fdt_path_offset(prepared_tree, "/chosen"),
                            inspection_mode == 5 ? STATE : SNAPSHOT, empty, sizeof(empty)) ||
                fdt_setprop_u32(prepared_tree, fdt_path_offset(prepared_tree, "/chosen"), LATE_SAME, 0) ||
                fdt_pack(prepared_tree))
                return -1;
        }
        fdt32_t same = cpu_to_fdt32(unchanged);
        if (fdt_setprop_inplace(prepared_tree, fdt_path_offset(prepared_tree, "/chosen"),
                                LATE_SAME, &same, sizeof(same)))
            return -1;
        char snapshot[512] = {0};
        int result = inspection_mode == 5 ? usb_t6050_snapshot_state(snapshot, sizeof(snapshot))
                                     : usb_t6050_snapshot_pmgr(snapshot, sizeof(snapshot));
        if (result ||
            fdt_setprop_inplace(prepared_tree, fdt_path_offset(prepared_tree, "/chosen"),
                                inspection_mode == 5 ? STATE : SNAPSHOT, snapshot, sizeof(snapshot)))
            return -1;
    } else if (usb_t6050_retire_for_kernel() ||
               (dual_retire && (bank0_map > 1 || usb_t6050_retire_bank0_for_kernel())))
        return -1;
    memcpy(tree, prepared_tree, fdt_totalsize(prepared_tree));
    dc_cvac_range(tree, fdt_totalsize(tree));
    kboot_usb2_j716c_reset();
    printf("USB0: exact final Linux DT transaction complete\n");
    return 0;
}
