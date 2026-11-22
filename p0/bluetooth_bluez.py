"""A Bluetooth Low Energy peripheral through BlueZ's D-Bus API: one GATT service and its advertisement.

Linux only. Needs python3-dbus and python3-gi, which Raspberry Pi OS and Debian 13 install by default,
and no group membership: BlueZ's D-Bus policy lets any user register an application. Only
p0/bluetooth_link.py imports this module; nothing else in Slot17 needs D-Bus.

The API is BlueZ's doc/org.bluez.GattManager.rst, org.bluez.GattCharacteristic.rst and
org.bluez.LEAdvertisingManager.rst. Advertising intervals follow Apple's Accessory Design Guidelines
(chapter 57): 20 ms at first, then one of Apple's longer intervals.
"""
from __future__ import annotations

import dbus
import dbus.service

BLUEZ = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
GATT_MANAGER = "org.bluez.GattManager1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHARACTERISTIC = "org.bluez.GattCharacteristic1"
AD_MANAGER = "org.bluez.LEAdvertisingManager1"
ADVERTISEMENT = "org.bluez.LEAdvertisement1"
ROOT = "/org/slot17"


def _path(obj):
    return dbus.ObjectPath(obj.__dbus_object_path__)


class _Object(dbus.service.Object):
    """A D-Bus object that answers Properties.GetAll for its one interface."""

    interface = ""

    def properties(self) -> dict:
        raise NotImplementedError

    @dbus.service.method(PROPERTIES, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != self.interface:
            raise dbus.exceptions.DBusException("org.freedesktop.DBus.Error.InvalidArgs", f"no interface {interface}")
        return self.properties()


class Characteristic(_Object):
    interface = GATT_CHARACTERISTIC

    def __init__(self, bus, path, uuid, flags, service_path, read=None, subscribed=None, written=None):
        super().__init__(bus, path)
        self.path, self.uuid, self.flags, self.service_path = path, uuid, flags, service_path
        self.read, self.subscribed_changed, self.written = read, subscribed, written
        self.notifying = False
        self.value = b""

    def properties(self):
        return {"Service": dbus.ObjectPath(self.service_path), "UUID": self.uuid,
                "Flags": dbus.Array(self.flags, signature="s"), "Descriptors": dbus.Array([], signature="o")}

    @dbus.service.method(GATT_CHARACTERISTIC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        value = self.read(dict(options)) if self.read else self.value
        offset = int(options.get("offset", 0))
        return dbus.Array(value[offset:], signature="y")

    @dbus.service.method(GATT_CHARACTERISTIC, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        if self.written is None:
            raise dbus.exceptions.DBusException("org.bluez.Error.NotSupported", "not writable")
        self.written(bytes(value))

    @dbus.service.method(GATT_CHARACTERISTIC)
    def StartNotify(self):
        if not self.notifying:
            self.notifying = True
            if self.subscribed_changed:
                self.subscribed_changed(True)

    @dbus.service.method(GATT_CHARACTERISTIC)
    def StopNotify(self):
        if self.notifying:
            self.notifying = False
            if self.subscribed_changed:
                self.subscribed_changed(False)

    @dbus.service.signal(PROPERTIES, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        """BlueZ turns a change of Value into a notification to every subscribed phone."""

    def notify(self, data: bytes) -> bool:
        self.value = bytes(data)
        if not self.notifying:
            return False
        self.PropertiesChanged(GATT_CHARACTERISTIC, {"Value": dbus.Array(self.value, signature="y")}, [])
        return True


class Service(_Object):
    interface = GATT_SERVICE

    def __init__(self, bus, path, uuid):
        super().__init__(bus, path)
        self.path, self.uuid, self.characteristics = path, uuid, []

    def properties(self):
        return {"UUID": self.uuid, "Primary": True,
                "Characteristics": dbus.Array([c.path for c in self.characteristics], signature="o")}


class Application(dbus.service.Object):
    """The root BlueZ asks for every object of the application."""

    def __init__(self, bus, service):
        super().__init__(bus, ROOT)
        self.service = service

    @dbus.service.method(OBJECT_MANAGER, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objects = {dbus.ObjectPath(self.service.path): {GATT_SERVICE: self.service.properties()}}
        for c in self.service.characteristics:
            objects[dbus.ObjectPath(c.path)] = {GATT_CHARACTERISTIC: c.properties()}
        return objects


class Advertisement(_Object):
    interface = ADVERTISEMENT

    def __init__(self, bus, path, service_uuid, name, interval_ms):
        super().__init__(bus, path)
        self.path, self.service_uuid, self.name, self.interval_ms = path, service_uuid, name, interval_ms

    def properties(self):
        # No TX power: with the 128-bit service UUID it would leave no room for the name in the 31 bytes.
        low, high = self.interval_ms
        return {"Type": "peripheral", "ServiceUUIDs": dbus.Array([self.service_uuid], signature="s"),
                "LocalName": dbus.String(self.name), "MinInterval": dbus.UInt32(low), "MaxInterval": dbus.UInt32(high)}

    @dbus.service.method(ADVERTISEMENT)
    def Release(self):
        """BlueZ dropped the advertisement (bluetoothd stopped, or the adapter went away)."""


def find_adapter(bus):
    """The first adapter that can serve GATT and advertise, as (path, properties), or (None, None)."""
    manager = dbus.Interface(bus.get_object(BLUEZ, "/"), OBJECT_MANAGER)
    for path, interfaces in sorted(manager.GetManagedObjects().items()):
        if ADAPTER in interfaces and GATT_MANAGER in interfaces and AD_MANAGER in interfaces:
            props = interfaces[ADAPTER]
            return str(path), {"address": str(props.get("Address", "")), "alias": str(props.get("Alias", "")),
                               "powered": bool(props.get("Powered", False))}
    return None, None


def set_alias(bus, adapter_path, name):
    """The adapter's name, which phones show once connected. BlueZ keeps it across restarts."""
    dbus.Interface(bus.get_object(BLUEZ, adapter_path), PROPERTIES).Set(ADAPTER, "Alias", dbus.String(name))


class Peripheral:
    """Registers one service with a notify and a read characteristic, and advertises it.

    `events(kind, value)` hears: "registered" (None), "advertising" (interval), "gatt_failed" and
    "advertising_failed" (BlueZ's message), "subscribed" (bool), "read" (MTU or None), "control" (the bytes a phone wrote). Every BlueZ call here is asynchronous, so nothing waits on
    bluetoothd inside the main loop.
    """

    def __init__(self, bus, adapter_path, service_uuid, frames_uuid, info_uuid, info, events, control_uuid=None):
        self.bus, self.adapter_path, self.service_uuid, self.events = bus, adapter_path, service_uuid, events
        adapter = bus.get_object(BLUEZ, adapter_path)
        self.gatt = dbus.Interface(adapter, GATT_MANAGER)
        self.ads = dbus.Interface(adapter, AD_MANAGER)
        self.service = Service(bus, ROOT + "/service0", service_uuid)
        self.frames = Characteristic(bus, self.service.path + "/frames", frames_uuid, ["read", "notify"],
                                     self.service.path, subscribed=lambda on: events("subscribed", on))
        self.info = Characteristic(bus, self.service.path + "/info", info_uuid, ["read"], self.service.path,
                                   read=self._read_info)
        self.info_bytes = info
        self.service.characteristics = [self.frames, self.info]
        if control_uuid:
            self.control = Characteristic(bus, self.service.path + "/control", control_uuid, ["write"], self.service.path,
                                          written=lambda data: events("control", data))
            self.service.characteristics.append(self.control)
        self.application = Application(bus, self.service)
        self.advert = None
        self.advert_count = 0

    def _read_info(self, options):
        mtu = options.get("mtu")
        self.events("read", int(mtu) if mtu is not None else None)
        return self.info_bytes()

    def register(self):
        self.gatt.RegisterApplication(_path(self.application), {}, reply_handler=lambda: self.events("registered", None),
                                      error_handler=lambda e: self.events("gatt_failed", e.get_dbus_message()))

    def restart(self):
        """After bluetoothd itself restarted: it forgot everything, so register again from scratch."""
        if self.advert is not None:
            self.advert.remove_from_connection()
            self.advert = None
        self.frames.notifying = False
        self.register()

    def rename(self, name):
        set_alias(self.bus, self.adapter_path, name)

    def advertise(self, name, interval_ms):
        """(Re)advertise under `name` at `interval_ms` = (min, max) milliseconds."""
        self.stop_advertising()
        self.advert_count += 1
        self.advert = Advertisement(self.bus, f"{ROOT}/advert{self.advert_count}", self.service_uuid, name, interval_ms)
        self.ads.RegisterAdvertisement(_path(self.advert), {}, reply_handler=lambda: self.events("advertising", interval_ms),
                                       error_handler=lambda e: self.events("advertising_failed", e.get_dbus_message()))

    def stop_advertising(self):
        if self.advert is None:
            return
        advert, self.advert = self.advert, None
        self.ads.UnregisterAdvertisement(_path(advert), reply_handler=lambda: advert.remove_from_connection(),
                                         error_handler=lambda e: advert.remove_from_connection())

    def notify(self, data: bytes) -> bool:
        return self.frames.notify(data)

    def close(self):
        self.stop_advertising()
        try:
            self.gatt.UnregisterApplication(_path(self.application))
        except dbus.exceptions.DBusException:
            pass                                    # bluetoothd is gone already: nothing left to remove
