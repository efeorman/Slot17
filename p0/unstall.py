#!/usr/bin/env python3
"""Clear a stalled bulk endpoint - the documented step-1 USB recovery.

Microsoft's "How to Recover From USB Pipe Errors" is explicit: control-transfer
errors are cleared automatically by the USB stack, but **data-transfer errors are
the client's job**, and recovery must start with the mildest operation:

    1. reset pipe   -> CLEAR_FEATURE(ENDPOINT_HALT)   <-- this script
    2. reset port   -> libusb_reset_device()          (a no-op under WinUSB)
    3. cycle port   -> pnputil /restart-device        (tears down the PnP node)

An earlier attempt at step 1 failed with "Entity not found" because the interface
had not been claimed - clear_halt requires it. That is fixed here.

This deliberately does NOT call reset(), set_configuration() or anything touching
PnP. The worst case is that it reports an error and changes nothing.

    python unstall.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import time

import usb.core
import usb.util
import usb.backend.libusb1

VID, PID = 0x1D50, 0x60A1
DLL = paths.libusb_library()


def main():
    backend = usb.backend.libusb1.get_backend(find_library=lambda x: DLL)
    if backend is None:
        print("Could not load libusb from", DLL)
        return 1

    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        print("No Airspy on USB - nothing to unstall.")
        return 1
    print(f"Airspy at bus {dev.bus} address {dev.address}")

    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError as e:
        print("Could not read configuration:", e)
        return 1

    cleared, failed = [], []
    for intf in cfg:
        num = intf.bInterfaceNumber
        claimed = False
        try:
            usb.util.claim_interface(dev, num)
            claimed = True
            print(f"claimed interface {num}")
        except usb.core.USBError as e:
            print(f"could not claim interface {num}: {e}")
            continue

        try:
            for ep in intf:
                addr = ep.bEndpointAddress
                direction = "IN " if addr & 0x80 else "OUT"
                try:
                    dev.clear_halt(addr)
                    print(f"  ep 0x{addr:02x} ({direction}): CLEAR_FEATURE"
                          f"(ENDPOINT_HALT) sent")
                    cleared.append(addr)
                except usb.core.USBError as e:
                    print(f"  ep 0x{addr:02x} ({direction}): failed - {e}")
                    failed.append(addr)
        finally:
            if claimed:
                try:
                    usb.util.release_interface(dev, num)
                    print(f"released interface {num}")
                except usb.core.USBError as e:
                    print(f"release failed: {e}")

    usb.util.dispose_resources(dev)
    time.sleep(1.0)

    print()
    if cleared:
        print(f"Cleared {len(cleared)} endpoint(s). Re-run airspy_info to check.")
    else:
        print("Nothing cleared.")
    return 0 if cleared else 1


if __name__ == "__main__":
    sys.exit(main())
