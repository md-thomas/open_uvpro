"""Connect to the UV-Pro and print its device info, battery, and GPS
position as JSON.

Usage:
    python scripts/connect_test.py XX:XX:XX:XX:XX:XX ble
    python scripts/connect_test.py XX:XX:XX:XX:XX:XX rfcomm [channel|auto]

Progress messages go to stderr, so stdout is clean JSON.
"""

import asyncio
import json
import sys

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.controller import RadioController
from discover_channel import discover_command_channel


def log(*args) -> None:
    print(*args, file=sys.stderr)


async def run(radio: RadioController) -> None:
    async with radio:
        result = {
            "device_info": radio.device_info.model_dump(mode="json"),
            "battery_percent": await radio.battery_level_as_percentage(),
            "gps": None,
        }

        if radio.status.is_gps_locked:
            result["gps"] = (await radio.position()).model_dump(mode="json")

        print(json.dumps(result, indent=2))


async def main_ble(device_uuid: str) -> None:
    await run(RadioController.new_ble(device_uuid))


async def main_rfcomm(device_uuid: str, channel_arg: str) -> None:
    if channel_arg == "auto":
        log("Discovering command channel...")
        # keep_alive: hand the same live socket straight to benlink instead
        # of closing it and opening a fresh one (the radio's RFCOMM service
        # needs real recovery time between separate connections).
        channel = discover_command_channel(device_uuid, keep_alive=True)
        log(f"Found command channel: {channel}")
    else:
        channel = int(channel_arg)

    await run(RadioController.new_rfcomm(device_uuid, channel=channel))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        log(f"Usage: {sys.argv[0]} XX:XX:XX:XX:XX:XX ble|rfcomm [channel]")
        sys.exit(1)

    device_uuid = sys.argv[1]
    mode = sys.argv[2]

    if mode == "ble":
        asyncio.run(main_ble(device_uuid))
    elif mode == "rfcomm":
        channel_arg = sys.argv[3] if len(sys.argv) > 3 else "auto"
        asyncio.run(main_rfcomm(device_uuid, channel_arg))
    else:
        log(f"Unknown mode: {mode!r} (expected 'ble' or 'rfcomm')")
        sys.exit(1)
