"""Shared default radio address for open_uvpro scripts.

All the entry-point scripts (connect_test.py, channel_control.py,
kiss_bridge.py, discover_channel.py, start_radio.sh) fall back to this
when no device address is given on the command line, so day-to-day use
doesn't require typing the MAC each time. Find it with scan_radio.sh if
you don't already know it, or override per-invocation with the
UV_PRO_ADDR environment variable (e.g. for a different radio or a
different machine).
"""

import os

DEFAULT_DEVICE_UUID = os.environ.get("UV_PRO_ADDR", "38:D2:00:01:85:BF")
