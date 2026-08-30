"""Print a snapshot of the UV-Pro's live status as JSON: device info,
battery, GPS, the actively-displayed channel/frequency/power, and the
dual-watch A/B channel pointers.

Usage:
    python scripts/radio_info.py [XX:XX:XX:XX:XX:XX]

device address defaults to radio_config.DEFAULT_DEVICE_UUID (override via
the UV_PRO_ADDR env var) if omitted. Requires exclusive RFCOMM access to
the radio -- stop the KISS bridge first if it's running.

Progress messages go to stderr, so stdout is clean JSON.
"""

import asyncio
import json
import sys

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from radio_config import DEFAULT_DEVICE_UUID
from radio_connect import connect_rfcomm, log


def channel_summary(channel) -> dict | None:
    if channel is None:
        return None
    power = "high" if channel.tx_at_max_power else "medium" if channel.tx_at_med_power else "low"
    return {
        "channel_id": channel.channel_id,
        "name": channel.name,
        "tx_freq": channel.tx_freq,
        "rx_freq": channel.rx_freq,
        "bandwidth": channel.bandwidth,
        "power": power,
    }


def channel_at(channels, index: int):
    return channels[index] if 0 <= index < len(channels) else None


async def main(device_uuid: str) -> None:
    async with connect_rfcomm(device_uuid, "auto") as radio:
        status = radio.status
        settings = radio.settings
        channels = radio.channels

        result = {
            "device_info": radio.device_info.model_dump(mode="json"),
            "battery_percent": await radio.battery_level_as_percentage(),
            "signal_strength_rssi": status.rssi,
            "gps": None,
            "current_channel": channel_summary(channel_at(channels, status.curr_ch_id)),
            "dual_watch": {
                "active_slot": status.double_channel,  # 'OFF' | 'A' | 'B'
                "channel_a": channel_summary(channel_at(channels, settings.channel_a)),
                "channel_b": channel_summary(channel_at(channels, settings.channel_b)),
            },
        }

        if status.is_gps_locked:
            result["gps"] = (await radio.position()).model_dump(mode="json")

        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    device_uuid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEVICE_UUID
    asyncio.run(main(device_uuid))
