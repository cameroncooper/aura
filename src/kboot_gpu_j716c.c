/* SPDX-License-Identifier: MIT AND GPL-2.0-only */

/* Opt-in, GPU-only handoff for the one-die J716c with the identified G17C 27.2 image. */
#include "adt.h"
#include "exception.h"
#include "firmware.h"
#include "malloc.h"
#include "mcc.h"
#include "memory.h"
#include "soc.h"
#include "string.h"
#include "utils.h"

#include "libfdt/libfdt.h"

#define GPU_BUS          "/soc@2300000000"
#define GPU_NODE         GPU_BUS "/gpu@2302600000"
#define GPU_PMGR         GPU_BUS "/power-management@2300e80000"
#define GPU_OPT_IN       "j716c,gpu-handoff"
#define GPU_STATES       14
#define GPU_LANES        4
#define GPU_TEXT_SIZE    0xa4000
#define GPU_DATA_SIZE    0x184000
#define GPU_MAX_PHYS_END (1ULL << 42)

struct gpu_range {
    u64 base;
    u64 size;
};

static const char *const region_path[] = {
    "/reserved-memory/uat-ttbs",
    "/reserved-memory/uat-pagetables",
    "/reserved-memory/uat-l2-pagetables",
    "/reserved-memory/uat-handoff",
    "/reserved-memory/gpu-fw-text",
    "/reserved-memory/gpu-fw-data",
};

static const char *const uat_name[] = {
    "gpu-region",
    "gfx-shared-region",
    "gfx-shared-l2-region",
    "gfx-handoff",
};

static const char *const power_path[] = {
    GPU_PMGR "/power-controller@0",
    GPU_PMGR "/power-controller@100",
    GPU_PMGR "/power-controller@110",
};

static const struct gpu_role {
    const char *asc;
    const char *mailbox;
    const char *role;
    const char *data;
    const char *sro;
    u32 text_size;
} gpu_roles[] = {
    {"/arm-io/gfx-asc", GPU_BUS "/mailbox@2302600000", "GFX", "gfx-data",
     "gfx-data-shared-ro", 0x50000},
    {"/arm-io/gfx1-asc", GPU_BUS "/mailbox@2302e00000", "GFX1", "gfx1-data",
     "gfx1-data-shared-ro", 0x54000},
};

struct gpu_power_handoff {
    fdt32_t main[GPU_STATES][1 + 2 * GPU_LANES];
    fdt32_t aux[GPU_STATES][3];
    fdt32_t aon[3][3];
    u32 mcc;
};

extern void aura_sha256(const void *data, size_t size, u8 digest[32]);

static bool gpu_adt_string(int node, const char *prop, const char *expected)
{
    u32 len;
    const char *value = adt_getprop(adt, node, prop, &len);
    return value && len == strlen(expected) + 1 && !memcmp(value, expected, len);
}

static bool gpu_dt_string(const void *dt, int node, const char *prop, const char *expected)
{
    int len;
    const char *value = fdt_getprop(dt, node, prop, &len);
    return value && len == (int)strlen(expected) + 1 && !memcmp(value, expected, len);
}

static bool gpu_disabled(const void *dt, int node)
{
    return gpu_dt_string(dt, node, "status", "disabled");
}

static bool gpu_adt_range(int sgx, const char *name, struct gpu_range *out)
{
    char prop[40];
    snprintf(prop, sizeof(prop), "%s-base", name);
    if (ADT_GETPROP(adt, sgx, prop, &out->base) != sizeof(out->base))
        return false;
    snprintf(prop, sizeof(prop), "%s-size", name);
    return ADT_GETPROP(adt, sgx, prop, &out->size) == sizeof(out->size);
}

static bool gpu_range_valid(struct gpu_range r)
{
    return r.base && r.size && !((r.base | r.size) & (SZ_16K - 1)) &&
           r.base < GPU_MAX_PHYS_END && r.size <= GPU_MAX_PHYS_END - r.base &&
           ram_base && r.base >= ram_base && r.base - ram_base <= mem_size_actual &&
           r.size <= mem_size_actual - (r.base - ram_base);
}

static bool gpu_ranges_overlap(struct gpu_range a, struct gpu_range b)
{
    return a.base <= b.base ? b.base - a.base < a.size : a.base - b.base < b.size;
}

static bool gpu_ranges_safe(const void *dt, const struct gpu_range ranges[6])
{
    int reserved = fdt_path_offset(dt, "/reserved-memory");
    int entries = fdt_num_mem_rsv(dt);
    if (entries < 0 || fdt_address_cells(dt, reserved) != 2 ||
        fdt_size_cells(dt, reserved) != 2)
        return false;
    for (size_t i = 0; i < ARRAY_SIZE(region_path); i++) {
        if (!gpu_range_valid(ranges[i]))
            return false;
        for (size_t j = 0; j < i; j++)
            if (gpu_ranges_overlap(ranges[i], ranges[j]))
                return false;
        for (size_t j = 0; j < mcc_carveout_count; j++) {
            struct gpu_range carveout = {mcc_carveouts[j].base, mcc_carveouts[j].size};
            if (carveout.size && gpu_ranges_overlap(ranges[i], carveout))
                return false;
        }
        for (int j = 0; j < entries; j++) {
            uint64_t addr, size;
            if (fdt_get_mem_rsv(dt, j, &addr, &size))
                return false;
            if (size && gpu_ranges_overlap(ranges[i], (struct gpu_range){addr, size}))
                return false;
        }
        int node;
        fdt_for_each_subnode(node, dt, reserved)
        {
            if (gpu_disabled(dt, node))
                continue;
            int len;
            const fdt64_t *reg = fdt_getprop(dt, node, "reg", &len);
            if (!reg)
                continue;
            if (len <= 0 || len % 16)
                return false;
            for (int cell = 0; cell < len / 8; cell += 2) {
                struct gpu_range other = {fdt64_ld(reg + cell), fdt64_ld(reg + cell + 1)};
                if (other.size && gpu_ranges_overlap(ranges[i], other))
                    return false;
            }
        }
    }
    return true;
}

static bool gpu_firmware_ranges(int sgx, struct gpu_range ranges[6])
{
    for (size_t i = 0; i < ARRAY_SIZE(uat_name); i++)
        if (!gpu_adt_range(sgx, uat_name[i], &ranges[i]))
            return false;
    if (ranges[0].size != 0x4000 || ranges[1].size < 0x44000 || ranges[2].size != 0x4000 ||
        ranges[3].size != 0x4000)
        return false;

    struct adt_segment_ranges segments[2][3];
    for (size_t i = 0; i < ARRAY_SIZE(gpu_roles); i++) {
        const struct gpu_role *role = &gpu_roles[i];
        int asc = adt_path_offset(adt, role->asc);
        if (asc < 0 || !gpu_adt_string(asc, "segment-names", "__TEXT;__DATA;__DATA_SHARED_RO") ||
            adt_getprop_copy(adt, asc, "segment-ranges", segments[i], sizeof(segments[i])) !=
                sizeof(segments[i]))
            return false;
        const struct adt_segment_ranges *s = segments[i];
        for (size_t j = 0; j < ARRAY_SIZE(segments[i]); j++)
            if (!gpu_range_valid((struct gpu_range){s[j].phys, s[j].size}) ||
                s[j].remap != s[j].phys)
                return false;
        struct gpu_range data, sro;
        if (!gpu_adt_range(sgx, role->data, &data) || !gpu_adt_range(sgx, role->sro, &sro) ||
            data.base != s[1].phys || data.size != s[1].size || sro.base != s[2].phys ||
            sro.size != s[2].size || s[0].size != role->text_size ||
            s[0].unk != 1 || s[1].unk != 0x10 || s[2].unk != 0 ||
            s[1].phys + s[1].size != s[2].phys)
            return false;
    }
    const struct adt_segment_ranges *gfx = segments[0], *gfx1 = segments[1];
    if (gfx[0].phys + gfx[0].size != gfx1[0].phys ||
        gfx[2].phys + gfx[2].size != gfx1[1].phys ||
        gfx1[2].phys + gfx1[2].size - gfx[1].phys != GPU_DATA_SIZE)
        return false;
    ranges[4] = (struct gpu_range){gfx[0].phys, GPU_TEXT_SIZE};
    ranges[5] = (struct gpu_range){gfx[1].phys, GPU_DATA_SIZE};
    return true;
}

static bool gpu_firmware_matches(u64 base)
{
    static const struct { u32 offset, size; } filled[] = {
        {0x224, 4}, {0x23c, 8}, {0x49764, 0x740}, {0x49ea4, 0x231},
        {0x50224, 4}, {0x5023c, 8}, {0x9d1ec, 0x740}, {0x9d92c, 0x231},
    };
    static const u32 entropy[][2] = {{0x49ea4, 0x49f87}, {0x9d92c, 0x9da0f}};
    static const u8 expected[] = {
        0x66, 0x48, 0xec, 0xe7, 0xe8, 0xc6, 0xb7, 0xb8, 0x28, 0x44, 0x36, 0x8f, 0x0e, 0x07, 0x5a,
        0x21, 0x80, 0xa0, 0x8c, 0xa3, 0x5f, 0xee, 0xd0, 0xb6, 0xe3, 0xf5, 0x48, 0x71, 0x0d, 0x4d,
        0xdb, 0x81,
    };

    u32 *copy = malloc(GPU_TEXT_SIZE);
    if (!copy)
        return false;
    int before = exc_count;
    exc_guard = GUARD_MARK | GUARD_SILENT;
    for (size_t i = 0; i < GPU_TEXT_SIZE / sizeof(*copy) && exc_count == before; i++)
        copy[i] = read32(base + i * sizeof(*copy));
    sysop("dsb sy");
    sysop("isb");
    exc_guard = GUARD_OFF;
    bool ok = exc_count == before;
    u8 *bytes = (u8 *)copy;
    for (size_t i = 0; ok && i < ARRAY_SIZE(entropy); i++)
        ok = !memcmp(bytes + entropy[i][0], "GKTS\x08\0\0\0", 8) &&
             !memcmp(bytes + entropy[i][1], "ECAP\x08\0\0\0", 8);
    if (ok) {
        u8 digest[sizeof(expected)];
        for (size_t i = 0; i < ARRAY_SIZE(filled); i++)
            memset(bytes + filled[i].offset, 0, filled[i].size);
        aura_sha256(copy, GPU_TEXT_SIZE, digest);
        ok = !memcmp(digest, expected, sizeof(expected));
    }
    free(copy);
    return ok;
}

static bool gpu_power_from_adt(int sgx, struct gpu_power_handoff *out)
{
    struct perf_state {
        u32 hz;
        u32 mv;
    } main[GPU_LANES][GPU_STATES], sram[GPU_LANES][GPU_STATES];
    u32 count, lanes, max_state;
    if (ADT_GETPROP(adt, sgx, "perf-state-count", &count) != sizeof(count) ||
        ADT_GETPROP(adt, sgx, "perf-state-table-count", &lanes) != sizeof(lanes) ||
        ADT_GETPROP(adt, sgx, "gpu-num-perf-states", &max_state) != sizeof(max_state) ||
        ADT_GETPROP(adt, sgx, "active-mcc-bit-vector", &out->mcc) != sizeof(out->mcc) ||
        count != GPU_STATES || lanes != GPU_LANES || max_state != GPU_STATES - 1 ||
        !out->mcc || (out->mcc & ~0xffu) ||
        adt_getprop_copy(adt, sgx, "perf-states", main, sizeof(main)) != sizeof(main) ||
        adt_getprop_copy(adt, sgx, "perf-states-sram", sram, sizeof(sram)) != sizeof(sram))
        return false;

    for (u32 state = 0; state < GPU_STATES; state++) {
        u32 hz = main[0][state].hz;
        if ((state ? hz <= main[0][state - 1].hz : hz != 0) || hz % 1000000 ||
            hz > 2000000000)
            return false;
        out->main[state][0] = cpu_to_fdt32(hz / 1000000);
        for (u32 lane = 0; lane < GPU_LANES; lane++) {
            u32 core_mv = main[lane][state].mv, sram_mv = sram[lane][state].mv;
            if (main[lane][state].hz != hz || sram[lane][state].hz != hz || !core_mv ||
                !sram_mv || core_mv > 1200 || sram_mv > 1200 || sram_mv < core_mv)
                return false;
            out->main[state][1 + lane] = cpu_to_fdt32(core_mv);
            out->main[state][1 + GPU_LANES + lane] = cpu_to_fdt32(sram_mv);
        }
    }

    /* The two auxiliary domains share one firmware table. The final word is the SRAM floor. */
    u64 aux[2 + 2 * GPU_STATES + 1];
    u32 aux_len;
    const void *afr = adt_getprop(adt, sgx, "afr-perf-states", &aux_len);
    if (!afr || aux_len != sizeof(aux) ||
        adt_getprop_copy(adt, sgx, "cs-perf-states", aux, sizeof(aux)) != sizeof(aux) ||
        memcmp(aux, afr, sizeof(aux)) || aux[0] != 1 || aux[1] != GPU_STATES)
        return false;
    u64 floor_uv = aux[ARRAY_SIZE(aux) - 1];
    if (floor_uv != 775000)
        return false;
    for (u32 state = 0; state < GPU_STATES; state++) {
        u64 uv = aux[2 + state * 2], hz = aux[3 + state * 2];
        if (!uv || uv > 1200000 || uv % 1000 || !hz || hz % 1000000 || hz > 2000000000 ||
            (state && hz <= aux[1 + state * 2]))
            return false;
        out->aux[state][0] = cpu_to_fdt32(hz / 1000000);
        out->aux[state][1] = cpu_to_fdt32(uv / 1000);
        out->aux[state][2] = cpu_to_fdt32((uv < floor_uv ? floor_uv : uv) / 1000);
    }

    static const u32 tags[] = {0x67305452, 0x67315452, 0x72305452}; /* g0TR, g1TR, r0TR */
    int aon = adt_path_offset(adt, "/arm-io/aon-ptd");
    u32 len;
    const u8 *rows = aon < 0 ? NULL : adt_getprop(adt, aon, "ptd_data_table", &len);
    if (!rows || !len || len % 12)
        return false;
    for (size_t i = 0; i < ARRAY_SIZE(tags); i++) {
        bool found = false;
        for (u32 offset = 0; offset < len; offset += 12) {
            u32 row[3];
            memcpy(row, rows + offset, sizeof(row));
            if (row[0] != tags[i])
                continue;
            if (found || !row[1] || !row[2])
                return false;
            found = true;
            for (size_t word = 0; word < ARRAY_SIZE(row); word++)
                out->aon[i][word] = cpu_to_fdt32(row[word]);
        }
        if (!found)
            return false;
    }
    return true;
}

static bool gpu_dt_reg(const void *dt, int node, const u64 *expected, size_t count)
{
    int len;
    const fdt64_t *reg = fdt_getprop(dt, node, "reg", &len);
    if (!reg || len != (int)(count * sizeof(*reg)))
        return false;
    for (size_t i = 0; i < count; i++)
        if (fdt64_ld(reg + i) != expected[i])
            return false;
    return true;
}

static bool gpu_dt_regions(const void *dt, int gpu)
{
    static const char *const names[] = {"ttbs", "pagetables", "l2-pagetables", "handoff",
                                        "fw-text", "fw-data"};
    int len;
    const fdt32_t *regions = fdt_getprop(dt, gpu, "memory-region", &len);
    if (!regions || len != (int)(ARRAY_SIZE(region_path) * sizeof(*regions)) ||
        fdt_stringlist_count(dt, gpu, "memory-region-names") != ARRAY_SIZE(region_path))
        return false;
    for (size_t i = 0; i < ARRAY_SIZE(region_path); i++) {
        int node = fdt_path_offset(dt, region_path[i]);
        int index = fdt_stringlist_search(dt, gpu, "memory-region-names", names[i]);
        if (node < 0 || index < 0 || fdt_node_offset_by_phandle(dt, fdt32_ld(regions + index)) != node ||
            !gpu_disabled(dt, node) || !fdt_getprop(dt, node, "no-map", &len) || len ||
            !gpu_dt_reg(dt, node, (u64[]){0, 0}, 2))
            return false;
    }
    return true;
}

static bool gpu_dt_resources(const void *dt, int gpu, int sgx_path[8])
{
    if (fdt_stringlist_search(dt, gpu, "reg-names", "asc") != 0 ||
        fdt_stringlist_search(dt, gpu, "reg-names", "sgx") != 1)
        return false;
    int len;
    const fdt32_t *mboxes = fdt_getprop(dt, gpu, "mboxes", &len);
    if (!mboxes || len != (int)(ARRAY_SIZE(gpu_roles) * sizeof(*mboxes)))
        return false;
    const fdt32_t *power = fdt_getprop(dt, gpu, "power-domains", &len);
    if (!power || len != sizeof(*power) ||
        fdt_node_offset_by_phandle(dt, fdt32_ld(power)) != fdt_path_offset(dt, power_path[2]))
        return false;
    u64 sgx_base, sgx_size, first_wrapper = 0;
    if (adt_get_reg(adt, sgx_path, "reg", 0, &sgx_base, &sgx_size) ||
        sgx_base != 0x2300000000 || !sgx_size || sgx_size > 0x4000000)
        return false;
    for (size_t i = 0; i < ARRAY_SIZE(gpu_roles); i++) {
        const struct gpu_role *role = &gpu_roles[i];
        int path[8];
        int asc = adt_path_offset_trace(adt, role->asc, path);
        int node = fdt_path_offset(dt, role->mailbox);
        u64 wrapper, wrapper_size, vbar, vbar_size;
        if (asc < 0 || !adt_is_compatible(adt, asc, "iop,ascwrap-v6") ||
            !gpu_adt_string(asc, "role", role->role) || node < 0 ||
            fdt_node_offset_by_phandle(dt, fdt32_ld(mboxes + i)) != node ||
            !gpu_dt_string(dt, node, "apple,firmware-role", role->role) ||
            fdt_node_check_compatible(dt, node, "apple,t6050-j775d-ascwrap-v6") ||
            !gpu_disabled(dt, node) ||
            fdt_stringlist_search(dt, node, "reg-names", "wrapper") != 0 ||
            fdt_stringlist_search(dt, node, "reg-names", "iop-vbar") != 1 ||
            fdt_stringlist_search(dt, node, "interrupt-names", "send-empty") != 0 ||
            fdt_stringlist_search(dt, node, "interrupt-names", "recv-not-empty") != 1 ||
            adt_get_reg(adt, path, "reg", 0, &wrapper, &wrapper_size) ||
            adt_get_reg(adt, path, "reg", 1, &vbar, &vbar_size) || wrapper_size != 0x88000 ||
            vbar_size < 8 || !gpu_dt_reg(dt, node, (u64[]){wrapper, wrapper_size, vbar, 8}, 4))
            return false;
        power = fdt_getprop(dt, node, "power-domains", &len);
        if (!power || len != sizeof(*power) ||
            fdt_node_offset_by_phandle(dt, fdt32_ld(power)) != fdt_path_offset(dt, power_path[2]))
            return false;
        u32 irq[4];
        const fdt32_t *irq_dt = fdt_getprop(dt, node, "interrupts", &len);
        if (!irq_dt || len != 8 * sizeof(*irq_dt) ||
            adt_getprop_copy(adt, asc, "interrupts", irq, sizeof(irq)) != sizeof(irq))
            return false;
        for (size_t j = 0; j < 2; j++)
            if (fdt32_ld(irq_dt + 4 * j) || fdt32_ld(irq_dt + 4 * j + 1) ||
                fdt32_ld(irq_dt + 4 * j + 2) != irq[j + 1] ||
                fdt32_ld(irq_dt + 4 * j + 3) != 4)
                return false;
        if (!i)
            first_wrapper = wrapper;
    }
    return gpu_dt_reg(dt, gpu, (u64[]){first_wrapper, 0x88000, sgx_base, 0x4000000}, 4);
}

static bool gpu_power_nodes(const void *dt)
{
    static const u32 offsets[] = {0, 0x100, 0x110};
    int pmgr = fdt_path_offset(dt, GPU_PMGR), nodes[ARRAY_SIZE(power_path)];
    if (pmgr < 0 || !gpu_dt_string(dt, pmgr, "status", "okay") ||
        fdt_node_check_compatible(dt, pmgr, "apple,t6050-pmgr") ||
        !gpu_dt_reg(dt, pmgr, (u64[]){0x2300e80000, 0x4000}, 2) ||
        fdt_address_cells(dt, pmgr) != 1 || fdt_size_cells(dt, pmgr) != 1)
        return false;
    for (size_t i = 0; i < ARRAY_SIZE(power_path); i++) {
        int node = nodes[i] = fdt_path_offset(dt, power_path[i]), len;
        const fdt32_t *reg = fdt_getprop(dt, node, "reg", &len);
        if (!gpu_disabled(dt, node) ||
            fdt_node_check_compatible(dt, node, "apple,t6050-pmgr-pwrstate") ||
            !reg || len != 2 * sizeof(*reg) || fdt32_ld(reg) != offsets[i] ||
            fdt32_ld(reg + 1) != 4)
            return false;
    }
    int len;
    const fdt32_t *deps = fdt_getprop(dt, nodes[2], "power-domains", &len);
    return deps && len == 2 * sizeof(*deps) &&
           fdt_node_offset_by_phandle(dt, fdt32_ld(deps)) == nodes[1] &&
           fdt_node_offset_by_phandle(dt, fdt32_ld(deps + 1)) == nodes[0];
}

static int gpu_enable_node(void *dt, const char *path)
{
    int node = fdt_path_offset(dt, path);
    return node < 0 ? node : fdt_setprop_string(dt, node, "status", "okay");
}

static bool gpu_publish(void *dt, const struct gpu_range ranges[6],
                        const struct gpu_power_handoff *data)
{
    for (size_t i = 0; i < ARRAY_SIZE(region_path); i++) {
        fdt64_t reg[] = {cpu_to_fdt64(ranges[i].base), cpu_to_fdt64(ranges[i].size)};
        int node = fdt_path_offset(dt, region_path[i]);
        if (node < 0 || fdt_setprop(dt, node, "reg", reg, sizeof(reg)) ||
            gpu_enable_node(dt, region_path[i]))
            return false;
    }
    int gpu = fdt_path_offset(dt, GPU_NODE);
    if (gpu < 0 || fdt_setprop(dt, gpu, "apple,j716c-gpu-perf-states", data->main,
                               sizeof(data->main)) ||
        fdt_setprop(dt, gpu, "apple,j716c-gpu-aux-perf-states", data->aux, sizeof(data->aux)) ||
        fdt_setprop(dt, gpu, "apple,j716c-gpu-aon-records", data->aon, sizeof(data->aon)) ||
        fdt_setprop_u32(dt, gpu, "apple,j716c-gpu-mcc-mask", data->mcc))
        return false;
    for (size_t i = 0; i < ARRAY_SIZE(power_path); i++)
        if (gpu_enable_node(dt, power_path[i]))
            return false;
    for (size_t i = 0; i < ARRAY_SIZE(gpu_roles); i++)
        if (gpu_enable_node(dt, gpu_roles[i].mailbox))
            return false;
    return !gpu_enable_node(dt, GPU_NODE);
}

int dt_set_gpu_j716c(void *dt)
{
    int gpu = fdt_path_offset(dt, GPU_NODE);
    if (gpu < 0)
        return 0;
    if (!gpu_disabled(dt, gpu)) {
        printf("FDT: J716c GPU: source GPU must be disabled\n");
        return -1;
    }

    int len, chosen = fdt_path_offset(dt, "/chosen");
    const fdt32_t *opt = fdt_getprop(dt, chosen, GPU_OPT_IN, &len);
    if (!opt || len != sizeof(*opt) || fdt32_ld(opt) != 1)
        return 0;
    if (chip_id != T6050 || board_id != 0xe || !gpu_adt_string(0, "model", "Mac17,6") ||
        !gpu_adt_string(0, "target-type", "J716c") || !os_firmware.iboot ||
        strcmp(os_firmware.iboot, "mBoot-20457.41.3") ||
        fdt_node_check_compatible(dt, 0, "apple,j716c") ||
        fdt_node_check_compatible(dt, gpu, "apple,agx-g17c") ||
        fdt_node_check_compatible(dt, gpu, "apple,agx-g17s")) {
        printf("FDT: J716c GPU: board, firmware build, or compatible mismatch\n");
        return 0;
    }

    int path[8];
    int sgx = adt_path_offset_trace(adt, "/arm-io/sgx", path);
    if (sgx < 0 || !adt_is_compatible(adt, sgx, "gpu,t6050") ||
        !gpu_dt_resources(dt, gpu, path) || !gpu_dt_regions(dt, gpu)) {
        printf("FDT: J716c GPU: unusable GPU, mailbox or reservation nodes\n");
        return 0;
    }
    if (!gpu_power_nodes(dt)) {
        printf("FDT: J716c GPU: unusable power state nodes\n");
        return 0;
    }

    struct gpu_range ranges[ARRAY_SIZE(region_path)];
    struct gpu_power_handoff data;
    if (!gpu_firmware_ranges(sgx, ranges) || !gpu_ranges_safe(dt, ranges)) {
        printf("FDT: J716c GPU: invalid GPU firmware or UAT ranges\n");
        return 0;
    }
    if (!gpu_firmware_matches(ranges[4].base)) {
        printf("FDT: J716c GPU: 27.2 G17C firmware identity mismatch\n");
        return 0;
    }
    if (!gpu_power_from_adt(sgx, &data)) {
        printf("FDT: J716c GPU: invalid per-unit power tables\n");
        return 0;
    }

    u32 size = fdt_totalsize(dt);
    void *scratch = malloc(size);
    if (!scratch)
        return 0;
    memcpy(scratch, dt, size);
    if (gpu_publish(scratch, ranges, &data) && !fdt_check_full(scratch, size)) {
        memcpy(dt, scratch, size);
        printf("FDT: J716c GPU: 27.2 G17C handoff published, MCC mask %#x\n", data.mcc);
    } else {
        printf("FDT: J716c GPU: FDT transaction refused\n");
    }
    free(scratch);
    return 0;
}
