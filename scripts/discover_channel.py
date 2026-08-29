"""Find the UV-Pro's command (SPP Dev) RFCOMM channel.

The radio's SDP service records get reassigned across sessions, so the
correct RFCOMM channel number for its command protocol floats (we've seen
it as 1, 2, and 4). Querying SDP (sdptool) to find it is unreliable in
practice, so instead we just probe each plausible channel directly with a
real Gaia GetDeviceInfo request and see which one answers correctly.

The radio's embedded Bluetooth stack also needs real recovery time between
RFCOMM sessions -- closing this probe connection and opening a fresh one
for the real session shortly after gets refused. So on success we leave
the socket open and hand it to patches.PENDING_SOCKETS for benlink's
RfcommClient to reuse directly, instead of closing it here.
"""

import socket

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink import protocol as p
from benlink.command import GetDeviceInfo, command_message_to_protocol

CHANNEL_PROBE_RANGE = range(1, 9)
PROBE_TIMEOUT = 3.0


def _build_gaia_get_device_info() -> bytes:
    msg_bytes = command_message_to_protocol(GetDeviceInfo()).to_bytes()
    gaia_frame = p.GaiaFrame(
        flags=p.GaiaFlags.NONE,
        n_bytes_payload=len(msg_bytes) - 4,
        data=msg_bytes,
    )
    return gaia_frame.to_bytes()


def _try_channel(addr: str, channel: int) -> socket.socket | None:
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(PROBE_TIMEOUT)
    try:
        s.connect((addr, channel))
        s.send(_build_gaia_get_device_info())
        reply = s.recv(1024)
        if len(reply) > 0 and reply[0] == 0xFF:
            return s
    except OSError:
        pass
    s.close()
    return None


def discover_command_channel(addr: str, keep_alive: bool = False) -> int:
    for channel in CHANNEL_PROBE_RANGE:
        s = _try_channel(addr, channel)
        if s is not None:
            if keep_alive:
                patches.PENDING_SOCKETS[(addr, channel)] = s
            else:
                s.close()
            return channel
    raise RuntimeError(
        f"No RFCOMM channel in {list(CHANNEL_PROBE_RANGE)} responded to a Gaia "
        "GetDeviceInfo request. Is the radio on, in range, and paired?"
    )


if __name__ == "__main__":
    import sys

    print(discover_command_channel(sys.argv[1]))
