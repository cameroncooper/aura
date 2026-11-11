/* SPDX-License-Identifier: MIT */
/* J716c port2 ADT-to-DT qualification. No controller access or ownership transfer. */
#include <malloc.h>
#include <string.h>

#include "kboot_usb4_j716c.h"
#include "adt.h"
#include "assert.h"
#include "firmware.h"
#include "kboot.h"
#include "libfdt/libfdt.h"
#include "pmgr.h"
#include "utils.h"

#define OPT    "j716c,usb4-handoff"
#define STATUS "j716c,usb4-handoff-status"
#define PORT   "/arm-io/acio2"
#define PHY    "/arm-io/atc-phy2"
#define PCIE   "/arm-io/apciec2"
#define FN     __attribute__((minsize, noinline))

enum { ROUTER, NHI, DART, TUNNEL, TUNNEL_PORT, TUNNEL_DART, COMBO, MBOX, RESET, NODES };
static const char *const paths[NODES] = {
    "/soc@200000000/acio@381ac0000",
    "/soc@200000000/acio@381ac0000/nhi@f00000",
    "/soc@200000000/acio@381ac0000/iommu@a80000",
    "/pcie-tunnel-2/pcie@1c70000000",
    "/pcie-tunnel-2/pcie@1c70000000/pci@0,0",
    "/pcie-tunnel-2/iommu@384000000",
    "/soc@200000000/phy@383000000",
    "/soc@200000000/mailbox@381108000",
    "/soc@200000000/reset-controller@280620000",
};

static FN int node(void *dt, int which)
{
    return fdt_path_offset(dt, paths[which]);
}
static FN int adt_node(const char *path)
{
    return adt_path_offset(adt, path);
}
static FN int prop64(int n, const char *name, u64 *value)
{
    u32 len;
    const void *raw = adt_getprop(adt, n, name, &len);
    if (!raw || (len != 4 && len != 8))
        return -1;
    *value = 0;
    memcpy(value, raw, len);
    return 0;
}
static FN bool adt_string(int n, const char *name, const char *want)
{
    u32 len;
    const char *raw = adt_getprop(adt, n, name, &len);
    size_t size = strlen(want) + 1;
    if (!raw || len < size || len > 512 || memcmp(raw, want, size))
        return false;
    for (u32 i = size; i < len; i++)
        if (raw[i])
            return false;
    return true;
}
static FN int adt_reg(const char *path, int index, u64 *base, u64 *size)
{
    int trace[8];
    return adt_path_offset_trace(adt, path, trace) < 0
               ? -1
               : adt_get_reg(adt, trace, "reg", index, base, size);
}
static FN u64 be_cells(const fdt32_t *p, int count)
{
    u64 value = 0;
    for (int i = 0; i < count; i++)
        value = (value << 32) | fdt32_ld(p + i);
    return value;
}
/* Decode each bus's actual cell widths and translate complete resource windows. */
static FN int dt_reg(void *dt, int n, int index, u64 *base, u64 *size)
{
    int bus = fdt_parent_offset(dt, n), len, ac = fdt_address_cells(dt, bus),
        sc = fdt_size_cells(dt, bus);
    const fdt32_t *p = fdt_getprop(dt, n, "reg", &len);
    if (bus < 0 || ac < 1 || ac > 2 || sc < 1 || sc > 2 || !p || index < 0 ||
        len % (4 * (ac + sc)) || index >= len / (4 * (ac + sc)))
        return -1;
    p += index * (ac + sc);
    *base = be_cells(p, ac);
    *size = be_cells(p + ac, sc);
    if (!*size || *base > UINT64_MAX - *size)
        return -1;
    for (int depth = 0; bus && depth < 16; depth++) {
        int parent = fdt_parent_offset(dt, bus), pac = fdt_address_cells(dt, parent);
        ac = fdt_address_cells(dt, bus);
        sc = fdt_size_cells(dt, bus);
        p = fdt_getprop(dt, bus, "ranges", &len);
        if (parent < 0 || !p || ac < 1 || ac > 2 || pac < 1 || pac > 2 || sc < 1 || sc > 2 ||
            len % (4 * (ac + pac + sc)))
            return -1;
        bool found = !len;
        for (int i = 0; i < len / 4; i += ac + pac + sc) {
            u64 start = be_cells(p + i, ac), target = be_cells(p + i + ac, pac),
                span = be_cells(p + i + ac + pac, sc);
            if (*base >= start && *base - start <= span && *size <= span - (*base - start)) {
                u64 offset = *base - start;
                if (target > UINT64_MAX - offset || target + offset > UINT64_MAX - *size)
                    return -1;
                *base = target + offset;
                found = true;
                break;
            }
        }
        if (!found)
            return -1;
        bus = parent;
    }
    return bus ? -1 : 0;
}
static FN int named_reg(void *dt, int which, const char *name, u64 *base, u64 *size)
{
    int n = node(dt, which);
    return dt_reg(dt, n, fdt_stringlist_search(dt, n, "reg-names", name), base, size);
}
static FN int match_reg(void *dt, int which, const char *name, const char *path, int index)
{
    u64 a, s, b, z;
    int n = node(dt, which), i = name ? fdt_stringlist_search(dt, n, "reg-names", name) : 0;
    return dt_reg(dt, n, i, &a, &s) || adt_reg(path, index, &b, &z) || a != b || s != z ? -1 : 0;
}
static FN int check_board(void)
{
    u64 value;
    int chosen = adt_node("/chosen");
    if (chip_id != T6050 || !adt_string(0, "model", "Mac17,6") ||
        !adt_string(0, "target-type", "J716c") || prop64(chosen, "board-id", &value) ||
        value != 0xe || !os_firmware.iboot || strcmp(os_firmware.iboot, "mBoot-20457.41.3") ||
        !adt_is_compatible(adt, adt_node(PORT), "acio,v2") ||
        !adt_is_compatible(adt, adt_node(PHY), "atc-phy,t6050") ||
        !adt_is_compatible(adt, adt_node(PCIE), "apciec,t6050"))
        return -1;
    return 0;
}
static FN int check_links(void *dt)
{
    int len;
    const fdt32_t *p = fdt_getprop(dt, node(dt, NHI), "iommus", &len);
    int dart = node(dt, DART);
    u32 ph = fdt_get_phandle(dt, dart);
    if (!p || len != 24 * 8 || !ph)
        return -1;
    for (int i = 0; i < 24; i++)
        if (fdt32_ld(p + 2 * i) != ph || fdt32_ld(p + 2 * i + 1) != (u32)(i < 12 ? i : i + 4))
            return -1;
    p = fdt_getprop(dt, node(dt, TUNNEL), "iommu-map", &len);
    if (!p || len != 16 || !fdt_get_phandle(dt, node(dt, TUNNEL_DART)) || fdt32_ld(p) ||
        fdt32_ld(p + 1) != fdt_get_phandle(dt, node(dt, TUNNEL_DART)) || fdt32_ld(p + 2) ||
        fdt32_ld(p + 3) != 64)
        return -1;
    for (int i = 0; i < 2; i++) {
        const char *path = i ? "/arm-io/dart-apciec2" : "/arm-io/dart-acio2";
        int n = node(dt, i ? TUNNEL_DART : DART), src = adt_node(path);
        u64 count, page;
        if (fdt_node_check_compatible(dt, n, "apple,t8110-dart") ||
            prop64(src, "sid-count", &count) || count != (u64)(i ? 256 : 28) ||
            prop64(src, "page-size", &page) || page != 16384)
            return -1;
        p = fdt_getprop(dt, n, "#iommu-cells", &len);
        if (!p || len != 4 || fdt32_ld(p) != 1)
            return -1;
        p = fdt_getprop(dt, n, "apple,bypass-sids", &len);
        if (!p || len != (i ? 8 : 4) || fdt32_ld(p) != (u32)(i ? 253 : 15) ||
            (i && fdt32_ld(p + 1) != 255) ||
            !adt_getprop(adt, src, i ? "bypass-253" : "bypass-15", NULL) ||
            (i && !adt_getprop(adt, src, "bypass-255", NULL)))
            return -1;
    }
    u64 vm, span;
    p = fdt_getprop(dt, dart, "apple,dma-range", &len);
    if (!p || len != 16 || prop64(adt_node("/arm-io/dart-acio2"), "vm-base", &vm) ||
        prop64(adt_node("/arm-io/dart-acio2"), "vm-size", &span))
        return -1;
    u64 base = be_cells(p, 2), size = be_cells(p + 2, 2);
    if (base < vm || base - vm > span || !size || size > span - (base - vm))
        return -1;
    p = fdt_getprop(dt, node(dt, ROUTER), "mboxes", &len);
    if (!p || len != 4 || fdt32_ld(p) != fdt_get_phandle(dt, node(dt, MBOX)))
        return -1;
    p = fdt_getprop(dt, node(dt, ROUTER), "resets", &len);
    if (!p || len != 8 || fdt32_ld(p) != fdt_get_phandle(dt, node(dt, RESET)) ||
        fdt32_ld(p + 1) != 2)
        return -1;
    return 0;
}
/* Resolve PMGR provider registers from the ADT table only; never inspect power state. */
static FN int check_gate(void *dt, int consumer, int slot, const char *path, const char *name)
{
    int len;
    struct pmgr_gate gate;
    bool found = false;
    for (u32 index = 0; index < 16 && !pmgr_adt_gate(path, "power-gates", index, &gate); index++)
        if (gate.found && !gate.virt && gate.name && !strncmp(gate.name, name, 16)) {
            found = true;
            break;
        }
    const fdt32_t *p = fdt_getprop(dt, node(dt, consumer), "power-domains", &len);
    if (!p || len % 4 || slot >= len / 4 || !found || !gate.psreg)
        return -1;
    int provider = fdt_node_offset_by_phandle(dt, fdt32_ld(p + slot));
    int parent = fdt_parent_offset(dt, provider);
    u64 base, size;
    p = fdt_getprop(dt, provider, "#power-domain-cells", &len);
    if (!p || len != 4 || fdt32_ld(p) || parent < 0 ||
        fdt_node_check_compatible(dt, provider, "apple,t6050-pmgr-pwrstate") ||
        fdt_address_cells(dt, parent) != 1 || fdt_size_cells(dt, parent) != 1 ||
        dt_reg(dt, parent, 0, &base, &size) || size < 4)
        return -1;
    /* PMGR children are provider offsets, not independent MMIO bus resources. */
    p = fdt_getprop(dt, provider, "reg", &len);
    return !p || len != 8 || fdt32_ld(p + 1) != 4 || fdt32_ld(p) > size - 4 ||
                   base + fdt32_ld(p) != gate.psreg
               ? -1
               : 0;
}
static FN int check_resources(void *dt)
{
    static const char *const compat[NODES] = {"apple,t6050-usb4-acio",
                                              "apple,t6050-usb4-nhi",
                                              "apple,t8110-dart",
                                              "apple,t6050-pciec",
                                              NULL,
                                              "apple,t8110-dart",
                                              "apple,t6050-combo-phy",
                                              "apple,asc-mailbox-v4",
                                              "apple,t6050-cio-reset"};
    for (int i = 0; i < NODES; i++)
        if (node(dt, i) < 0 || (compat[i] && fdt_node_check_compatible(dt, node(dt, i), compat[i])))
            return -1;
    if (match_reg(dt, NHI, "nhi", PORT, 0) || match_reg(dt, ROUTER, "pcie-adapter", PORT, 4) ||
        match_reg(dt, ROUTER, "router-ap-fw", PORT, 5) ||
        match_reg(dt, ROUTER, "cpu", "/arm-io/acio-cpu2", 1) ||
        match_reg(dt, MBOX, NULL, "/arm-io/acio-cpu2", 0) ||
        match_reg(dt, DART, NULL, "/arm-io/dart-acio2", 0) ||
        match_reg(dt, TUNNEL_DART, NULL, "/arm-io/dart-apciec2", 0))
        return -1;
    u64 base, size, a, s;
    if (named_reg(dt, ROUTER, "rc", &base, &size) || size != 3 * 0x4000)
        return -1;
    for (int i = 1; i <= 3; i++)
        if (adt_reg(PORT, i, &a, &s) || a != base + (i - 1) * 0x4000 || s != 0x4000)
            return -1;
    static const struct {
        const char *name;
        int index;
    } pcie[] = {{"config", 0}, {"rc", 1},    {"port0", 2},   {"fabric", 3},
                {"debug", 4},  {"ltssm", 6}, {"intr2axi", 7}};
    for (size_t i = 0; i < ARRAY_SIZE(pcie); i++)
        if (match_reg(dt, TUNNEL, pcie[i].name, PCIE, pcie[i].index))
            return -1;
    if (adt_reg("/arm-io/pmgr", 0, &a, &s) || dt_reg(dt, node(dt, RESET), 0, &base, &size) ||
        base != a + 0x20000 || size != 0x4000)
        return -1;
    static const char *const gates[] = {"ATC2_CIO", "ATC2_CIO_PCIE", "ATC2_CIO_USB",
                                        "ATC2_CIO_AUSS"};
    for (int i = 0; i < 4; i++)
        if (check_gate(dt, ROUTER, i, PORT, gates[i]))
            return -1;
    if (check_gate(dt, ROUTER, 4, PCIE, "ATC2_PCIE") ||
        check_gate(dt, TUNNEL, 0, PCIE, "ATC2_PCIE") ||
        check_gate(dt, TUNNEL_DART, 0, PCIE, "ATC2_PCIE") ||
        check_gate(dt, DART, 0, PORT, "ATC2_CIO"))
        return -1;
    return check_links(dt);
}
/* The __DATA SRAM is the image location consumed by this kernel's Type7 boot path. */
static FN int check_firmware(void *dt)
{
    int src = adt_node("/arm-io/acio-cpu2/iop-acio2-nub");
    u64 value, base, size;
    u32 len;
    const struct adt_segment_ranges *seg = adt_getprop(adt, src, "segment-ranges", &len);
    if (prop64(src, "running", &value) || value != 1 || prop64(src, "pre-loaded", &value) ||
        value != 1 || !adt_string(src, "segment-names", "__TEXT;__DATA") || !seg ||
        len != 2 * sizeof(*seg) || named_reg(dt, ROUTER, "sram", &base, &size))
        return -1;
    struct adt_segment_ranges data;
    memcpy(&data, seg + 1, sizeof(data));
    return data.phys != base || data.size != size || data.unk ? -1 : 0;
}
struct table {
    const char *src, *dst, *adt_path, *resource;
    u32 offset, window;
    u8 target;
    bool optional;
};
static const struct table tables[] = {
    {"top_tunables", "apple,tunable-rc", PORT, "rc", 0, 0x4000, ROUTER},
    {"hbw_fabric_tunables", "apple,tunable-rc", PORT, "rc", 0x4000, 0x4000, ROUTER},
    {"lbw_fabric_tunables", "apple,tunable-rc", PORT, "rc", 0x8000, 0x4000, ROUTER},
    {"hi_up_tx_desc_fabric_tunables", "apple,tunable-nhi", PORT, "nhi", 0xe8000, 0x4000, NHI, true},
    {"hi_up_tx_data_fabric_tunables", "apple,tunable-nhi", PORT, "nhi", 0xec000, 0x4000, NHI, true},
    {"hi_up_rx_desc_fabric_tunables", "apple,tunable-nhi", PORT, "nhi", 0xf0000, 0x4000, NHI, true},
    {"hi_up_wr_fabric_tunables", "apple,tunable-nhi", PORT, "nhi", 0xf4000, 0x4000, NHI, true},
    {"hi_up_merge_fabric_tunables", "apple,tunable-nhi", PORT, "nhi", 0xf8000, 0x4000, NHI},
    {"hi_dn_merge_fabric_tunables", "apple,tunable-nhi", PORT, "nhi", 0xfc000, 0x4000, NHI},
    {"fw_int_ctl_management_tunables", "apple,tunable-nhi", PORT, "nhi", 0x4000, 0x4000, NHI},
    {"pcie_adapter_regs_tunables", "apple,tunable-pcie-adapter", PORT, "pcie-adapter", 0, 0x4000,
     ROUTER},
    {"pcie_router_ap_fw", "apple,tunable-router-ap-fw", PORT, "router-ap-fw", 0, 0x4000, ROUTER},
    {"dart-tunables-instance-0", "apple,tunable", "/arm-io/dart-acio2", NULL, 0, 0x4000, DART},
    {"dart-tunables-instance-0", "apple,tunable", "/arm-io/dart-apciec2", NULL, 0, 0x4000,
     TUNNEL_DART},
    {"atc-apcie-debug-tunables", "apple,tunable-debug", PCIE, "debug", 0, 0x2000, TUNNEL},
    {"atc-apcie-fabric-tunables", "apple,tunable-fabric", PCIE, "fabric", 0, 0x4000, TUNNEL},
    {"atc-apcie-rc-tunables", "apple,tunable-rc", PCIE, "rc", 0, 0x4000, TUNNEL},
    {"apcie-config-tunables", "apple,tunable", PCIE, "port0", 0, 0x4000, TUNNEL_PORT},
};
struct acio_record {
    u32 offset, size;
    u64 mask, value;
} PACKED;
static_assert(sizeof(struct acio_record) == 24, "ACIO record size");
static FN int copy_table(void *dt, const struct table *t)
{
    u32 len;
    const u8 *raw = adt_getprop(adt, adt_node(t->adt_path), t->src, &len);
    if (!raw)
        return t->optional ? 0 : -1;
    if (len % sizeof(struct acio_record) || (!t->optional && !len))
        return -1;
    u64 base, size;
    int n = node(dt, t->target);
    int ret = t->resource ? named_reg(dt, t->target == TUNNEL_PORT ? TUNNEL : t->target,
                                      t->resource, &base, &size)
                          : dt_reg(dt, n, 0, &base, &size);
    if (ret)
        return -1;
    if (t->offset > size || t->window > size - t->offset)
        return -1;
    for (u32 i = 0; i < len; i += sizeof(struct acio_record)) {
        struct acio_record r;
        memcpy(&r, raw + i, sizeof(r));
        if (r.size != 4 || r.offset % 4 || r.offset > t->window - 4 || r.mask > UINT32_MAX ||
            r.value > UINT32_MAX)
            return -1;
        fdt32_t cells[3] = {cpu_to_fdt32(t->offset + r.offset), cpu_to_fdt32(r.mask),
                            cpu_to_fdt32(r.value)};
        if (fdt_appendprop(dt, node(dt, t->target), t->dst, cells, sizeof(cells)))
            return -1;
    }
    return 0;
}
static FN int copy_cio(void *dt)
{
    static const struct {
        const char *src, *dst;
        u8 index;
        u32 offset;
    } fields[] = {
        {"tunable_UC_REGS_CIO_DFLT", "apple,tunable-uc-regs-cio", 3, 0x400},
        {"tunable_LN0_RX_TOP_CIO_DFLT", "apple,tunable-lane0-rx-top-cio", 16, 0x9000},
        {"tunable_LN1_RX_TOP_CIO_DFLT", "apple,tunable-lane1-rx-top-cio", 25, 0x19000},
        {"tunable_LN0_RX_EQ_CIO_DFLT", "apple,tunable-lane0-rx-eq-cio", 18, 0xb000},
        {"tunable_LN1_RX_EQ_CIO_DFLT", "apple,tunable-lane1-rx-eq-cio", 27, 0x1b000},
    };
    u64 core, core_size;
    if (named_reg(dt, COMBO, "core", &core, &core_size))
        return -1;
    for (size_t i = 0; i < ARRAY_SIZE(fields); i++) {
        u64 base, size;
        u32 len;
        const u8 *raw = adt_getprop(adt, adt_node(PHY), fields[i].src, &len);
        if (!raw || len % 12 || adt_reg(PHY, fields[i].index, &base, &size) ||
            core + fields[i].offset != base || fields[i].offset >= core_size)
            return -1;
        u64 window = size < core_size - fields[i].offset ? size : core_size - fields[i].offset;
        int n = node(dt, COMBO);
        if (fdt_setprop_empty(dt, n, fields[i].dst))
            return -1;
        for (u32 j = 0; j < len; j += 12) {
            u32 r[3];
            memcpy(r, raw + j, sizeof(r));
            u32 offset = r[0] & 0xffffff;
            if ((r[0] >> 24) != 32 || offset % 4 || window < 4 || offset > window - 4)
                return -1;
            fdt32_t cells[3] = {cpu_to_fdt32(offset), cpu_to_fdt32(r[1]), cpu_to_fdt32(r[2])};
            if (fdt_appendprop(dt, node(dt, COMBO), fields[i].dst, cells, sizeof(cells)))
                return -1;
        }
    }
    return 0;
}
static FN u8 crc8(const u8 *raw, size_t size)
{
    u8 value = 255;
    for (size_t i = 0; i < size; i++) {
        value ^= raw[i];
        for (int bit = 0; bit < 8; bit++)
            value = (value << 1) ^ (value & 128 ? 7 : 0);
    }
    return value;
}
static FN u32 crc32c(const u8 *raw, size_t size)
{
    u32 value = UINT32_MAX;
    for (size_t i = 0; i < size; i++) {
        value ^= raw[i];
        for (int bit = 0; bit < 8; bit++)
            value = (value >> 1) ^ (value & 1 ? 0x82f63b78 : 0);
    }
    return ~value;
}
static FN int copy_drom(void *dt)
{
    u32 size;
    const u8 *raw = adt_getprop(adt, adt_node(PORT), "thunderbolt-drom", &size);
    if (!raw || size < 22 || size > 256 || (raw[13] != 0 && raw[13] != 1 && raw[13] != 3))
        return -1;
    u32 end = 13 + (raw[14] | (raw[15] & 15) << 8), offset = raw[13] == 3 ? 16 : 22, want;
    memcpy(&want, raw + 9, 4);
    if (offset > end || end > size || crc8(raw + 1, 8) != raw[0] ||
        crc32c(raw + 13, end - 13) != want)
        return -1;
    while (offset < end) {
        u32 length = raw[offset];
        if (length < 2 || length > end - offset)
            return -1;
        offset += length;
    }
    u64 uuid = rust_usb4_router_uuid();
    if (!uuid)
        return -1;
    uuid |= 2;
    u8 copy[256];
    memcpy(copy, raw, size);
    memcpy(copy + 1, &uuid, 8);
    copy[0] = crc8(copy + 1, 8);
    return fdt_setprop(dt, node(dt, NHI), "apple,thunderbolt-drom", copy, size);
}
/* Never enable devices. The scratch tree is used to qualify every write and then discarded. */
static FN int prepare(void *dt)
{
    static const struct {
        const char *name;
        u8 target;
    } aliases[] = {{"usb4_2_rc", ROUTER},
                   {"usb4_2_pcie_adapter", ROUTER},
                   {"usb4_2_nhi", NHI},
                   {"usb4_2_pcie", TUNNEL}};
    for (size_t i = 0; i < ARRAY_SIZE(aliases); i++) {
        const char *old = fdt_get_alias(dt, aliases[i].name);
        if ((old && strcmp(old, paths[aliases[i].target])) ||
            fdt_setprop_string(dt, fdt_path_offset(dt, "/aliases"), aliases[i].name,
                               paths[aliases[i].target]))
            return -1;
    }
    for (size_t i = 0; i < ARRAY_SIZE(tables); i++) {
        int ret = fdt_delprop(dt, node(dt, tables[i].target), tables[i].dst);
        if (ret && ret != -FDT_ERR_NOTFOUND)
            return -1;
    }
    for (size_t i = 0; i < ARRAY_SIZE(tables); i++)
        if (copy_table(dt, tables + i))
            return -1;
    return copy_cio(dt) || copy_drom(dt) ? -1 : 0;
}
bool kboot_usb4_j716c_requested(const void *dt)
{
    return chip_id == T6050 && !fdt_node_check_compatible(dt, 0, "apple,j716c") &&
           fdt_getprop(dt, fdt_path_offset(dt, "/chosen"), OPT, NULL);
}
int kboot_usb4_j716c_validate(void *dt)
{
    if (!kboot_usb4_j716c_requested(dt))
        return 0;
    int chosen = fdt_path_offset(dt, "/chosen"), len;
    const fdt32_t *opt = fdt_getprop(dt, chosen, OPT, &len);
    const char *status = "refused-opt-in";
    if (len != 4 || fdt32_ld(opt) != 2)
        goto done;
    status = "refused-board";
    if (check_board())
        goto done;
    status = "refused-resources";
    if (check_resources(dt))
        goto done;
    status = "refused-firmware-metadata";
    if (check_firmware(dt))
        goto done;
    status = "refused-scratch";
    int capacity = fdt_totalsize(dt);
    void *scratch = malloc(capacity);
    if (scratch) {
        if (!fdt_open_into(dt, scratch, capacity) && !prepare(scratch))
            status = "validated-metadata-only";
        free(scratch);
    }
done:
    if (fdt_setprop_string(dt, chosen, STATUS, status))
        printf("USB4 J716c: could not record qualification status\n");
    printf("USB4 J716c: %s; no ownership transfer or device activation\n", status);
    return 0;
}
