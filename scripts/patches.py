"""Runtime compatibility patches for benlink.

Import this before connecting.
"""

import asyncio
import os

import benlink  # noqa: F401 (ensures all protocol submodules are imported)
from benlink.protocol.command.bitfield import Bitfield, BFLit

# This UV-Pro's firmware sets bits in fields benlink's protocol definitions
# treat as always-zero reserved padding (e.g. DevInfo._pad, RfCh._pad, ...).
# Relax every such literal-padding field to a plain int across all known
# Bitfield subclasses, so decoding doesn't hard-fail on firmware benlink
# wasn't originally tested against.


def _all_bitfield_subclasses():
    seen = set()
    stack = [Bitfield]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return seen


for _cls in _all_bitfield_subclasses():
    for _name, _field in list(_cls._fields.items()):
        if isinstance(_field, BFLit) and _name.lower().startswith("_pad"):
            _cls._fields[_name] = _field.inner


# bleak's BlueZ backend normally requires a fresh LE advertisement scan
# to locate the device before connecting (BleakScanner.find_device_by_address),
# which fails if the radio isn't actively advertising even though it's
# already bonded/known to BlueZ. Bypass the scan by connecting straight to
# the known, stable D-Bus object path for the bonded device instead.
from benlink import link as _link  # noqa: E402
from bleak import BleakClient  # noqa: E402
from bleak.backends.device import BLEDevice  # noqa: E402

_ADAPTER = os.environ.get("BENLINK_ADAPTER", "hci0")


def _ble_init(self, device_uuid: str) -> None:
    path = f"/org/bluez/{_ADAPTER}/dev_" + device_uuid.replace(":", "_").upper()
    device = BLEDevice(device_uuid, None, details={"path": path})
    self._client = BleakClient(device, timeout=60)


_link.BleCommandLink.__init__ = _ble_init


# The radio's BLE link is briefly unstable right after connecting (likely
# while it's also negotiating classic audio/SPP profiles in parallel), and
# the very first GATT write can fail with "Not connected" even though
# connect()/start_notify() just succeeded. Give it a moment to settle.
_orig_ble_connect = _link.BleCommandLink.connect


async def _ble_connect(self, callback):
    await _orig_ble_connect(self, callback)
    await asyncio.sleep(2.0)


_link.BleCommandLink.connect = _ble_connect


# The radio's RFCOMM command service needs real recovery time between
# sessions -- closing a connection and opening a fresh one shortly after
# gets refused, even after a long wait. So instead of discovering the
# live channel with a throwaway probe connection and then opening a
# *second* connection for the real benlink session, we hand off the same
# still-open socket from the probe directly into benlink's RfcommClient.
import socket as _socket  # noqa: E402

PENDING_SOCKETS: dict[tuple[str, int], _socket.socket] = {}

_orig_rfcomm_connect = _link.RfcommClient.connect


async def _rfcomm_connect_reuse(self, callback):
    loop = asyncio.get_event_loop()
    if self._st is not None:
        raise RuntimeError("Already connected")

    key = (self._device_uuid, self._channel)
    socket_handle = PENDING_SOCKETS.pop(key, None)
    if socket_handle is None:
        await _orig_rfcomm_connect(self, callback)
        return

    socket_handle.setblocking(False)

    async def listen():
        while True:
            data = await loop.sock_recv(socket_handle, self._read_size)
            if not data:
                self._st = None
                break
            callback(data)

    listen_task = loop.create_task(listen())
    self._st = _link.SocketTask(socket_handle, listen_task)


_link.RfcommClient.connect = _rfcomm_connect_reuse
