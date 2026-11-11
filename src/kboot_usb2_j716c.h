/* SPDX-License-Identifier: MIT */
#ifndef KBOOT_USB2_J716C_H
#define KBOOT_USB2_J716C_H
#include "types.h"
void kboot_usb2_j716c_reset(void);
int kboot_usb2_j716c_prepare(void *dt, size_t capacity);
int kboot_usb2_j716c_finish(void *dt);
#endif
