"""Open a raw RFCOMM socket and dump whatever bytes come back, bypassing
benlink's strict frame parser entirely. Also sends a raw Gaia-encoded
GetDeviceInfo request so we can see the actual reply bytes on the wire.

Usage:
    python scripts/raw_rfcomm_dump.py XX:XX:XX:XX:XX:XX <channel>
"""

import socket
import sys

sys.path.insert(0, ".")
import patches  # noqa: F401,E402
from benlink import protocol as p  # noqa: E402
from benlink.command import GetDeviceInfo, command_message_to_protocol  # noqa: E402


def build_gaia_frame_bytes() -> bytes:
    msg_bytes = command_message_to_protocol(GetDeviceInfo()).to_bytes()
    gaia_frame = p.GaiaFrame(
        flags=p.GaiaFlags.NONE,
        n_bytes_payload=len(msg_bytes) - 4,
        data=msg_bytes,
    )
    return gaia_frame.to_bytes()


def main(addr: str, channel: int) -> None:
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(5.0)
    print(f"Connecting to {addr} channel {channel}...")
    s.connect((addr, channel))
    print("Connected. Listening for 3s before sending anything...")

    s.settimeout(3.0)
    try:
        data = s.recv(1024)
        print(f"Unsolicited ({len(data)} bytes): {data.hex()}")
    except socket.timeout:
        print("(nothing received unsolicited)")

    frame_bytes = build_gaia_frame_bytes()
    print(f"Sending GetDeviceInfo Gaia frame ({len(frame_bytes)} bytes): {frame_bytes.hex()}")
    s.send(frame_bytes)

    s.settimeout(5.0)
    try:
        for _ in range(5):
            data = s.recv(1024)
            if not data:
                break
            print(f"Reply ({len(data)} bytes): {data.hex()}")
    except socket.timeout:
        print("(no reply within 5s)")

    s.close()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
