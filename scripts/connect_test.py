"""Connect to the UV-Pro and print its device info.

Usage:
    python scripts/connect_test.py XX:XX:XX:XX:XX:XX ble
    python scripts/connect_test.py XX:XX:XX:XX:XX:XX rfcomm [channel|auto]
"""

import asyncio
import sys

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.controller import RadioController
from discover_channel import discover_command_channel


async def run(radio: RadioController) -> None:
    async with radio:
        print(radio.device_info)
        print(f"Battery: {await radio.battery_level_as_percentage()}%")


async def main_ble(device_uuid: str) -> None:
    await run(RadioController.new_ble(device_uuid))


async def main_rfcomm(device_uuid: str, channel_arg: str) -> None:
    if channel_arg == "auto":
        print("Discovering command channel...")
        # keep_alive: hand the same live socket straight to benlink instead
        # of closing it and opening a fresh one (the radio's RFCOMM service
        # needs real recovery time between separate connections).
        channel = discover_command_channel(device_uuid, keep_alive=True)
        print(f"Found command channel: {channel}")
    else:
        channel = int(channel_arg)

    await run(RadioController.new_rfcomm(device_uuid, channel=channel))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} XX:XX:XX:XX:XX:XX ble|rfcomm [channel]")
        sys.exit(1)

    device_uuid = sys.argv[1]
    mode = sys.argv[2]

    if mode == "ble":
        asyncio.run(main_ble(device_uuid))
    elif mode == "rfcomm":
        channel_arg = sys.argv[3] if len(sys.argv) > 3 else "auto"
        asyncio.run(main_rfcomm(device_uuid, channel_arg))
    else:
        print(f"Unknown mode: {mode!r} (expected 'ble' or 'rfcomm')")
        sys.exit(1)
