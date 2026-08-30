"""Read and edit UV-Pro channel memories, and set the dual-watch A/B
channel pointers.

The actively displayed/live channel (status.curr_ch_id) is controlled by
the radio's physical channel selector and is read-only over Bluetooth --
there is no command in this protocol to change it remotely. settings'
channel_a/channel_b instead set which channel each dual-watch slot (A/B)
points to; see status.double_channel for which slot (or neither) is
currently active.

Usage:
    python scripts/channel_control.py XX:XX:XX:XX:XX:XX list
    python scripts/channel_control.py XX:XX:XX:XX:XX:XX get <channel_id>
    python scripts/channel_control.py XX:XX:XX:XX:XX:XX set-watch-channel A|B <channel_id>
    python scripts/channel_control.py XX:XX:XX:XX:XX:XX edit <channel_id> key=value [key=value ...]

Editable keys (edit): name, tx_freq, rx_freq, tx_mod (AM|FM|DMR),
rx_mod (AM|FM|DMR), bandwidth (NARROW|WIDE), tx_sub_audio, rx_sub_audio
(a CTCSS tone in Hz, or 'none'), scan, tx_at_max_power, tx_at_med_power,
talk_around, pre_de_emph_bypass, sign, tx_disable, fixed_freq,
fixed_bandwidth, fixed_tx_power, mute (booleans: true/false).

Channel IDs are 0-indexed.
"""

import asyncio
import json
import sys

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.controller import RadioController
from radio_connect import connect_rfcomm, log

BOOL_FIELDS = {
    "scan", "tx_at_max_power", "tx_at_med_power", "talk_around",
    "pre_de_emph_bypass", "sign", "tx_disable", "fixed_freq",
    "fixed_bandwidth", "fixed_tx_power", "mute",
}
FLOAT_FIELDS = {"tx_freq", "rx_freq"}
SUB_AUDIO_FIELDS = {"tx_sub_audio", "rx_sub_audio"}


def parse_value(key: str, raw: str):
    if key in BOOL_FIELDS:
        if raw.lower() not in ("true", "false"):
            raise ValueError(f"{key} must be 'true' or 'false', got {raw!r}")
        return raw.lower() == "true"
    if key in FLOAT_FIELDS:
        return float(raw)
    if key in SUB_AUDIO_FIELDS:
        return None if raw.lower() == "none" else float(raw)
    return raw  # name, tx_mod, rx_mod, bandwidth (pydantic validates Literal values)


def parse_edit_args(args: list[str]) -> dict:
    channel_args = {}
    for arg in args:
        if "=" not in arg:
            raise ValueError(f"Expected key=value, got {arg!r}")
        key, raw = arg.split("=", 1)
        channel_args[key] = parse_value(key, raw)
    return channel_args


async def cmd_list(radio: RadioController) -> None:
    print(json.dumps([c.model_dump(mode="json") for c in radio.channels], indent=2))


async def cmd_get(radio: RadioController, channel_id: int) -> None:
    print(json.dumps(radio.channels[channel_id].model_dump(mode="json"), indent=2))


async def cmd_set_watch_channel(radio: RadioController, slot: str, channel_id: int) -> None:
    if slot not in ("A", "B"):
        raise ValueError(f"slot must be 'A' or 'B', got {slot!r}")
    await radio.set_settings(**{f"channel_{slot.lower()}": channel_id})
    log(f"Set dual-watch channel {slot} to {channel_id}")


async def cmd_edit(radio: RadioController, channel_id: int, channel_args: dict) -> None:
    await radio.set_channel(channel_id, **channel_args)
    print(json.dumps(radio.channels[channel_id].model_dump(mode="json"), indent=2))


async def main(device_uuid: str, channel_arg: str, argv: list[str]) -> None:
    async with connect_rfcomm(device_uuid, channel_arg) as radio:
        command = argv[0]
        if command == "list":
            await cmd_list(radio)
        elif command == "get":
            await cmd_get(radio, int(argv[1]))
        elif command == "set-watch-channel":
            await cmd_set_watch_channel(radio, argv[1], int(argv[2]))
        elif command == "edit":
            await cmd_edit(radio, int(argv[1]), parse_edit_args(argv[2:]))
        else:
            log(f"Unknown command: {command!r}")
            sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        log(__doc__)
        sys.exit(1)

    device_uuid = sys.argv[1]
    command_argv = sys.argv[2:]
    asyncio.run(main(device_uuid, "auto", command_argv))
