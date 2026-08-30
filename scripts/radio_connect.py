"""Shared connect helper for UV-Pro scripts (RFCOMM, auto channel discovery)."""

import sys
from contextlib import asynccontextmanager

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.controller import RadioController
from discover_channel import discover_command_channel


def log(*args) -> None:
    print(*args, file=sys.stderr)


@asynccontextmanager
async def connect_rfcomm(device_uuid: str, channel_arg: str = "auto"):
    if channel_arg == "auto":
        log("Discovering command channel...")
        # keep_alive: hand the same live socket straight to benlink instead
        # of closing it and opening a fresh one (the radio's RFCOMM service
        # needs real recovery time between separate connections).
        channel = discover_command_channel(device_uuid, keep_alive=True)
        log(f"Found command channel: {channel}")
    else:
        channel = int(channel_arg)

    async with RadioController.new_rfcomm(device_uuid, channel=channel) as radio:
        yield radio
