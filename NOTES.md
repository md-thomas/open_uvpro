# Project notes / handoff

Context for picking this project up in a fresh session (e.g. on a different
machine) without re-deriving everything from scratch.

## Machine topology

The radio is paired via Bluetooth on `macbookair2` (Ubuntu). Radio MAC:
`38:D2:00:01:85:BF`. All scripts in `scripts/` must run on a machine with
Bluetooth hardware and the radio paired/trusted there.

## Protocol gotchas

These are the non-obvious things that took a long debugging session to
find (most are also commented inline where relevant):

- The radio's actual command/data protocol is Bluetooth **Classic RFCOMM**
  (the `SPP Dev` SDP service), not BLE GATT — despite `benlink` advertising
  BLE as the primary transport. BLE connections do complete (even resolve
  GATT services) but the traffic is bridged over BR/EDR ("GATT over
  BR/EDR"), which `bleak` handles flakily. RFCOMM is the reliable path.

- `SPP Dev`'s RFCOMM channel number **floats between sessions** (seen 1, 2,
  4) — it must be (re)discovered live, never hardcoded.
  `scripts/discover_channel.py` does this by sending a real Gaia
  `GetDeviceInfo` probe on each candidate channel and checking for a valid
  reply.

- The radio's embedded Bluetooth stack **refuses a fresh RFCOMM connect
  shortly after any disconnect** — even 60s of backoff wasn't enough to
  fix this. The working fix: never close a connection and reopen a new
  one; hand off the *same live socket* from the discovery probe straight
  into benlink's `RfcommClient` (see `patches.py`'s `PENDING_SOCKETS`
  mechanism and `radio_connect.py`).

- **"Call Audio" must be enabled in the radio's own Bluetooth menu** (a
  physical/UI setting on the radio itself, not software). Without it,
  BlueZ's automatic HFP audio-profile negotiation gets refused and tears
  down the *entire* connection, including the data channel.

- This firmware sets real values in bits that `benlink`'s protocol structs
  treat as always-zero reserved padding (`_pad` fields), and even
  `benlink`'s GitHub `main` branch doesn't perfectly match this firmware's
  field widths in every struct. `patches.py` generically relaxes every
  literal `_pad` field across all `Bitfield` subclasses at import time.

- Install `benlink` from GitHub `main`, not PyPI — the PyPI release
  (0.1.1) is stale and missing many protocol fields this firmware uses
  (see `requirements.txt`).

## Current capabilities

- `connect_test.py` — device info, battery, GPS position (JSON on stdout)
- `channel_control.py` — list/get/edit channel memories (name, freq,
  modulation, bandwidth, tone, power/scan flags), set dual-watch A/B
  channel pointers

Note: the radio's actively-displayed channel is hardware-dial-controlled
and not remotely settable in this protocol.

## Not yet built

- TNC/APRS data send/receive
- Event subscriptions (`add_event_handler`)
