/* SPDX-License-Identifier: MIT AND GPL-2.0-only */
#ifndef J716C_RECOVERY_H
#define J716C_RECOVERY_H

#include "adt.h"
#include "string.h"
#include "utils.h"

static inline bool j716c_recovery_board(void)
{
    if (chip_id != T6050 || board_id != 0xe)
        return false;
    u32 model_len = 0, target_len = 0;
    const char *model = adt_getprop(adt, 0, "model", &model_len);
    const char *target = adt_getprop(adt, 0, "target-type", &target_len);
    return model && model_len == sizeof("Mac17,6") && !memcmp(model, "Mac17,6", model_len) &&
           target && target_len == sizeof("J716c") && !memcmp(target, "J716c", target_len);
}

#endif
