/* SPDX-License-Identifier: MIT */

#ifndef USB_DWC3_H
#define USB_DWC3_H

#include "dart.h"
#include "types.h"

typedef struct dwc3_dev dwc3_dev_t;

typedef enum _cdc_acm_pipe_id_t {
    CDC_ACM_PIPE_0,
    CDC_ACM_PIPE_1,
    CDC_ACM_PIPE_MAX
} cdc_acm_pipe_id_t;

dwc3_dev_t *usb_dwc3_init(uintptr_t regs, dart_dev_t *dart);
dwc3_dev_t *usb_dwc3_init_mode(uintptr_t regs, dart_dev_t *dart, bool superspeed);
void usb_dwc3_shutdown(dwc3_dev_t *dev);
int usb_dwc3_shutdown_checked(dwc3_dev_t *dev);
bool usb_dwc3_managed_usb2(dwc3_dev_t *dev, uintptr_t regs, uintptr_t dart, u8 sid);

void usb_dwc3_handle_events(dwc3_dev_t *dev);

ssize_t usb_dwc3_can_read(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe);
bool usb_dwc3_can_write(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe);

u8 usb_dwc3_getbyte(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe);
void usb_dwc3_putbyte(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe, u8 byte);

size_t usb_dwc3_read(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe, void *buf, size_t count);
size_t usb_dwc3_write(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe, const void *buf, size_t count);
size_t usb_dwc3_queue(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe, const void *buf, size_t count);
void usb_dwc3_flush(dwc3_dev_t *dev, cdc_acm_pipe_id_t pipe);

#endif
