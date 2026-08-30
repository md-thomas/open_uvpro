"""Connect to the UV-Pro and print its device info, battery, and GPS
position as JSON.

Usage:
    python scripts/connect_test.py [XX:XX:XX:XX:XX:XX] ble
    python scripts/connect_test.py [XX:XX:XX:XX:XX:XX] rfcomm [channel|auto]

device address defaults to radio_config.DEFAULT_DEVICE_UUID (override via
the UV_PRO_ADDR env var) if omitted.

Progress messages go to stderr, so stdout is clean JSON.
"""

import asyncio
import json
import sys

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.controller import RadioController
from radio_config import DEFAULT_DEVICE_UUID
from radio_connect import connect_rfcomm, log

MODES = {"ble", "rfcomm"}


async def report(radio: RadioController) -> None:
    result = {
        "device_info": radio.device_info.model_dump(mode="json"),
        "battery_percent": await radio.battery_level_as_percentage(),
        "gps": None,
    }

    if radio.status.is_gps_locked:
        result["gps"] = (await radio.position()).model_dump(mode="json")

    print(json.dumps(result, indent=2))


async def main_ble(device_uuid: str) -> None:
    async with RadioController.new_ble(device_uuid) as radio:
        await report(radio)


async def main_rfcomm(device_uuid: str, channel_arg: str) -> None:
    async with connect_rfcomm(device_uuid, channel_arg) as radio:
        await report(radio)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in MODES:
        device_uuid = DEFAULT_DEVICE_UUID
        rest = sys.argv[1:]
    else:
        device_uuid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEVICE_UUID
        rest = sys.argv[2:]

    if not rest:
        log(f"Usage: {sys.argv[0]} [XX:XX:XX:XX:XX:XX] ble|rfcomm [channel]")
        sys.exit(1)

    mode = rest[0]

    if mode == "ble":
        asyncio.run(main_ble(device_uuid))
    elif mode == "rfcomm":
        channel_arg = rest[1] if len(rest) > 1 else "auto"
        asyncio.run(main_rfcomm(device_uuid, channel_arg))
    else:
        log(f"Unknown mode: {mode!r} (expected 'ble' or 'rfcomm')")
        sys.exit(1)
