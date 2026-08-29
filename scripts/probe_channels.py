"""Probe a range of RFCOMM channels for which one currently accepts a
connection (SPP Dev's channel number floats across sessions on this radio).

Usage:
    python scripts/probe_channels.py XX:XX:XX:XX:XX:XX
"""

import socket
import sys


def try_channel(addr: str, channel: int) -> bool:
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(5.0)
    try:
        s.connect((addr, channel))
        s.close()
        return True
    except OSError:
        return False


def main(addr: str) -> None:
    for channel in range(1, 8):
        ok = try_channel(addr, channel)
        print(f"channel {channel}: {'OPEN' if ok else 'refused/timeout'}")


if __name__ == "__main__":
    main(sys.argv[1])
