/* SPDX-License-Identifier: MIT AND GPL-2.0-only */

#include "../build/build_cfg.h"

#include "wdt.h"
#include "adt.h"
#include "exception.h"
#include "j716c_recovery.h"
#include "stage1_config.h"
#include "types.h"
#include "utils.h"

#define WDT_COUNT 0x10
#define WDT_ALARM 0x14
#define WDT_CTL   0x1c

static u64 wdt_base = 0;
#if defined(CHAINLOADING) && !defined(ESP_STAGE2)
static unsigned wdt_armed_seconds;
static int wdt_arm_j716c(unsigned seconds)
{
    wdt_armed_seconds = 0;
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/wdt", path);
    u64 primary, primary_size, secondary, secondary_size;
    u32 version;
    if (!j716c_recovery_board() || seconds < 1 || seconds > 160 || node < 0 ||
        ADT_GETPROP(adt, node, "wdt-version", &version) != sizeof(version) || version != 3 ||
        adt_get_reg(adt, path, "reg", 0, &primary, &primary_size) || primary != 0x28836c000ULL ||
        primary_size != 0x4000 || adt_get_reg(adt, path, "reg", 2, &secondary, &secondary_size) ||
        secondary != 0x288330008ULL || secondary_size != 4)
        return -1;

    int before = exc_count;
    exc_guard = GUARD_MARK | GUARD_SILENT;
    u32 control = read32(primary + WDT_CTL);
    u32 reset = read32(secondary);
    exc_guard = GUARD_OFF;
    if (exc_count != before || (control != 0 && control != 4) || reset)
        return -1;

    /* The root-qualified WD1 sequence; retain an already active lease throughout. */
    u32 ticks = seconds * 24000000u;
    before = exc_count;
    exc_guard = GUARD_MARK | GUARD_SILENT;
    write32(primary + WDT_ALARM, ticks);
    write32(primary + WDT_COUNT, 0);
    write32(primary + WDT_CTL, 4);
    sysop("dsb sy");
    u32 alarm = read32(primary + WDT_ALARM);
    control = read32(primary + WDT_CTL);
    u32 count = read32(primary + WDT_COUNT);
    exc_guard = GUARD_OFF;
    if (exc_count != before || alarm != ticks || control != 4 || count >= alarm || count > 24000000)
        return -1;

    wdt_base = primary;
    wdt_armed_seconds = seconds;
    return 0;
}
#endif

void wdt_disable(void)
{
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/wdt", path);

    if (node < 0) {
        printf("WDT node not found!\n");
        return;
    }

    if (adt_get_reg(adt, path, "reg", 0, &wdt_base, NULL)) {
        printf("Failed to get WDT reg property!\n");
        return;
    }

    printf("Primary WDT register @ 0x%lx\n", wdt_base);
    write32(wdt_base + WDT_CTL, 0);
    printf("Primary WDT disabled\n");

    // disable secondary watchdog if wdt-version is 2 or 3
    u32 wdt_version;
    if (ADT_GETPROP(adt, node, "wdt-version", &wdt_version) < 0)
        return;

    if (wdt_version == 2 || wdt_version == 3) {
        u64 wdt_2nd = 0;
        if (adt_get_reg(adt, path, "reg", 2, &wdt_2nd, NULL)) {
            printf("Failed to get WDT reg[2] property!\n");
            return;
        }

        printf("Secondary WDT register @ 0x%lx\n", wdt_2nd);
        write32(wdt_2nd, 0);
        printf("Secondary WDT disabled\n");
    }
}

void wdt_reboot(void)
{
    /* A fault before wdt_disable() (early init) must still reset the Mac, not spin */
    if (!wdt_base && adt) {
        int path[8];
        u64 base = 0;
        if (adt_path_offset_trace(adt, "/arm-io/wdt", path) >= 0 &&
            !adt_get_reg(adt, path, "reg", 0, &base, NULL))
            wdt_base = base;
    }
    if (!wdt_base)
        return;

    write32(wdt_base + WDT_ALARM, 0x100000);
    write32(wdt_base + WDT_COUNT, 0);
    write32(wdt_base + WDT_CTL, 4);
}

static int wdt_arm(unsigned seconds)
{
#if defined(CHAINLOADING) && !defined(ESP_STAGE2)
    if (stage1_config_target() && j716c_recovery_board())
        return wdt_arm_j716c(seconds);
#endif
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/wdt", path);
    u64 primary, primary_size, secondary, secondary_size;
    u32 version;
    if (seconds < 1 || seconds > 178 || node < 0 ||
        ADT_GETPROP(adt, node, "wdt-version", &version) < 0 || version != 2 ||
        adt_get_reg(adt, path, "reg", 0, &primary, &primary_size) ||
        adt_get_reg(adt, path, "reg", 2, &secondary, &secondary_size) || (primary & 3) ||
        primary_size < 0x20 || secondary_size != 4 || read32(secondary))
        return -1;

    u32 alarm = seconds * 24000000u;
    write32(primary + WDT_CTL, 0);
    write32(primary + WDT_COUNT, 0);
    write32(primary + WDT_ALARM, alarm);
    write32(primary + WDT_CTL, 4);
    sysop("dsb sy");
    if (read32(primary + WDT_ALARM) != alarm || !(read32(primary + WDT_CTL) & 4))
        return -1;
    wdt_base = primary;
    return 0;
}

int wdt_arm_seconds(unsigned seconds)
{
#if defined(CHAINLOADING) && !defined(ESP_STAGE2)
    if (stage1_config_target() && j716c_recovery_board())
        return wdt_arm_j716c(seconds);
#endif
    if (chip_id != T8140 && chip_id != T6040)
        return -1;
    return wdt_arm(seconds);
}

/*
 * A trial boot (aura-try, chainload.c) has nobody to reset it if a stage hangs, so each aura
 * stage of it disarms iBoot's watchdog as usual and then arms its own, from the ADT's version 2
 * layout, until Linux's driver finds it running and keeps it fed. The next boot takes the
 * normal path, as the trial marker is already spent.
 */
static bool wdt_boot_armed;

int wdt_arm_trial(void)
{
    wdt_boot_armed = false;
#if defined(CHAINLOADING) && !defined(ESP_STAGE2)
    if (stage1_config_target() && j716c_recovery_board())
        return wdt_arm_j716c(160);
#endif
    wdt_disable();
    return wdt_arm(WDT_TRIAL_SECONDS);
}

/*
 * A plain boot disarms iBoot's watchdog as soon as Stage 1 starts, and nothing re-arms one until
 * Linux, so a Stage 1 or early Stage 2 that stops (an NVMe command that never completes, a failed
 * read that drops it into the proxy with no host) stays on screen until someone holds the power
 * button. Stage 1 arms the primary watchdog for its longest period instead. The first host it
 * sees (a synced window or proxy request) disarms it, so a debugging session is never reset
 * under it; Stage 2's own wdt_disable() at its start disarms it on the normal path. Its reads
 * take well under a second, and its bounded host windows are 10-100 s.
 */
int wdt_arm_boot(void)
{
    unsigned seconds = WDT_BOOT_SECONDS;
#if defined(CHAINLOADING) && !defined(ESP_STAGE2)
    if (stage1_config_target() && j716c_recovery_board()) {
        seconds = 160;
        wdt_boot_armed = false;
    }
#endif
    if (wdt_arm(seconds))
        return -1;
    wdt_boot_armed = true;
    return 0;
}

bool wdt_boot_watchdog_armed(void)
{
    return wdt_boot_armed;
}

unsigned wdt_timeout_seconds(void)
{
#if defined(CHAINLOADING) && !defined(ESP_STAGE2)
    if (stage1_config_target() && j716c_recovery_board())
        return wdt_armed_seconds;
#endif
    return WDT_BOOT_SECONDS;
}

/* Only the boot watchdog: a trial's or a host-armed one is not ours to disarm. */
void wdt_host_attached(void)
{
    if (!wdt_boot_armed)
        return;
    wdt_boot_armed = false;
    wdt_disable();
    printf("WDT: host attached, boot watchdog off\n");
}
