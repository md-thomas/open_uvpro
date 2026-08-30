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
- `kiss_bridge.py` — bridges the radio's built-in TNC to a real KISS pty,
  so the Linux AX.25 stack (and Pat) can use the radio as a packet TNC
  over Bluetooth. See "Pat / Winlink over Bluetooth" below.

Note: the radio's actively-displayed channel is hardware-dial-controlled
and not remotely settable in this protocol.

## Pat / Winlink over Bluetooth

The radio's TNC data doesn't come out as raw KISS bytes -- it rides
inside the same benlink command protocol as everything else (fragmented
`HT_SEND_DATA`/`DATA_RXD` messages, ≤50 bytes/fragment, gated by the
`kiss_en` setting -- already `True` on this radio, so `kiss_bridge.py`
leaves it alone). `kiss_bridge.py` does the translation: it opens a pty,
speaks real KISS framing (FEND/FESC escaping) on it, and
fragments/reassembles across the radio connection underneath.

Pat's config (`~/.config/pat/config.json`) is already set up for this:
`ax25.engine: "linux"`, `ax25_linux.port: "wl2k"` -- i.e. it expects a
kernel AX.25 interface reachable through an axports entry named `wl2k`.

One-time setup:

```
sudo apt install ax25-tools ax25-apps
echo "wl2k    K0MDT-1 1200    255     2       UV-Pro Bluetooth TNC" | sudo tee -a /etc/ax25/axports
```

(`ax25-tools`/`ax25-apps` aren't installed yet as of 2026-08-29 -- the
`ax25` kernel module is present. Nothing above has been run; needs sudo
and wasn't available non-interactively.)

Per session, in one terminal:

```
cd /home/mdthomas/Projects/open_ht && source .venv/bin/activate
python scripts/kiss_bridge.py 38:D2:00:01:85:BF
```

Leave it running (prints the pty path, defaults to
`~/.cache/uvpro-kisstnc`). In another terminal:

```
sudo kissattach ~/.cache/uvpro-kisstnc wl2k
pat connect ax25:///SOME-CALL
```

The pty symlink deliberately defaults under `~/.cache`, not `/tmp`:
`kissattach` runs as root via sudo, and the kernel's `fs.protected_symlinks`
hardening (on by default on Ubuntu) refuses to let root follow a symlink
owned by another user sitting in a sticky world-writable directory like
`/tmp` -- fails with `open: Permission denied` even though the file
permissions look fine. `~/.cache` isn't sticky/world-writable, so the
restriction never triggers. (Hit this live on 2026-08-29 with the /tmp
default before switching it.)

`scripts/start_bridge.sh [device_uuid]` launches both the bridge and
`kissattach` together in a detached `screen` session (`screen -r
uvpro-tnc` to attach; `sudo` still prompts interactively in the
`kissattach` window). Note: `kissattach` daemonizes on success and keeps
running independently of the window/session that launched it -- a stale
one from an earlier run holds the `wl2k` port lock and makes the next
attach fail with "already in use". `start_bridge.sh` checks for this and
tells you to `sudo pkill kissattach` first; do that before every fresh
start if you skip the script.

**Known failure mode, hit live on 2026-08-29: `ax0` can get permanently
stuck at the kernel level.** After enough kill/restart/crash cycles of
the bridge (don't know the precise trigger -- suspect an abrupt kill of
`kiss_bridge.py`, or of `kissattach` itself, without it getting a clean
SIGTERM to detach), `ax0` ends up orphaned: no `kissattach` process
owns it (`ps` shows nothing), but the interface object persists
(`ip link show ax0` still reports it, same ifindex, forever). Symptoms:
a fresh `sudo kissattach ~/.cache/uvpro-kisstnc wl2k` prints only the
usual benign `tty_speed: tcgetattr: Inappropriate ioctl for device`
warning and silently does nothing -- no success line
(`AX.25 port wl2k bound to device ax0`), no new process, `ax0` unchanged.
Tried and confirmed NOT sufficient: `sudo ip link delete ax0` (fails,
`RTNETLINK answers: Operation not supported` -- AX.25 devices don't
support netlink delete), `sudo ip link set ax0 down` (succeeds, but
doesn't release the underlying line-discipline attachment). Confirmed
root cause: `sudo rmmod mkiss` / `rmmod ax25` both fail with
`Module ... is in use` -- a genuine stuck kernel refcount, not just a
missing userspace process. At that point the only reliable fix is a
reboot; there's no live process left to signal and no forced-removal
path that's worth the risk for what it's unlikely to fix. If this
happens again, don't burn time on `ip link`/`rmmod` troubleshooting --
confirm with `rmmod mkiss` once (fast, harmless check) and go straight
to reboot if it reports "in use".

Prevention (unconfirmed, but worth trying next time): always let
`kissattach` receive a real `SIGTERM` (`sudo kill <pid>`, not `-9`) before
starting a new one, and always stop `kiss_bridge.py` with a plain
`kill -INT` (which it handles for clean shutdown) rather than `-9`,
even when it seems unresponsive -- give it a few seconds first.

The bridge honors KISS TXDELAY/TXTAIL parameter frames (maps to the
radio's `kiss_tx_delay`/`kiss_tx_tail` settings, currently 0/0) if
`kissattach`/`kissparms` sends them; if on-air packets come out garbled,
that's the first thing to check.

**Known bug, live on 2026-08-29 (not yet fixed, only mitigated):** the
underlying Gaia-protocol read loop (`listen()` in `patches.py`, wrapping
benlink's frame parser) can desync and die with `ValueError: error in
field 'start' of 'GaiaFrame': expected b'\xff', got b'\xc0'`. It dies
*silently* -- as an unretrieved background task exception, not something
`kiss_bridge.py`'s own code sees -- so every subsequent
`send_tnc_data_fragment` call just hangs forever awaiting a reply that
will never arrive. This is almost certainly what caused the first
"Dial timeout" against N4SER-10: the kernel AX.25 stack successfully
wrote SABM retries to the pty (confirmed via `ip -s link show ax0`), but
the bridge was silently deadlocked on an earlier send and never actually
forwarded anything over Bluetooth. Root cause of the desync itself is
still unknown (seen once right after two concurrent `channel_control.py`
probes hit the radio while the bridge already held its one command
connection open -- maybe related, unconfirmed). Mitigation in place:
`send_tnc_data_fragment` calls are now wrapped in `asyncio.wait_for(...,
timeout=10)`, and the outer connection loop reconnects on any exception
-- so a desync now costs a stall and a fresh RFCOMM connection instead of
a silent, permanent hang.

**Root cause identified (still 2026-08-29):** it's not TX-related. Added
raw-chunk logging to `patches.py`'s `listen()` (prints the offending
bytes on any parse error) and caught it live: the crash fires on
*inbound* real off-air packet traffic, unrelated to anything we send.
The captured chunk was a genuine APRS position report the radio received
over RF (decoded cleanly to `...2754.60NS08159.53W_...Mulberry,
Florida...`), delivered as a `DATA_RXD`/`EventType=12` (`DATA_TXD`, no
dispatch case in benlink's `event_notification_disc`) notification --
and critically, the raw bytes were themselves `0xC0`-delimited (KISS
framing) *inside* the Gaia notification payload. benlink's length-field
arithmetic for that message evidently doesn't account for this
firmware's real-packet framing, desyncing the byte stream for
everything after it. This is a real, fairly frequent trigger in this
area (ambient packet/APRS chatter), not an edge case -- expect the
bridge to reconnect periodically during normal use, not just during our
own connect attempts. `RECONNECT_BACKOFF_SECONDS` is 15 (not 5) to avoid
hammering the radio's BT stack's own "won't reconnect right after a
disconnect" quirk.

**Actually fixed, 2026-08-29:** `GaiaFrame.from_bitstream_batch` already
has a built-in resync mode (`consume_errors=True` -- drop one byte and
keep scanning for the next valid frame instead of raising) that benlink
just doesn't use by default. `patches.py` now replaces
`RfcommCommandLink.connect` to pass `consume_errors=True`. Verified with
a standalone unit test (corrupt-prefix + valid-frame bytes -> the valid
frame still recovers) before relying on it live. This turns a bad
message into a silent single-byte skip instead of killing the whole
RFCOMM connection -- much cheaper than the reconnect-based mitigation,
and (important) means a real reply arriving right when ambient traffic
also triggers the bug no longer risks getting lost to a torn-down
connection. The reconnect-loop mitigation (wait_for timeout, proactive
listen-task-death detection, 15s backoff) is kept as a second line of
defense for whatever this doesn't catch.

If packet connects still stall mysteriously, check the bridge's log for
`TX:`/`RX:` lines (confirms frames reaching the radio) and `[DEBUG]
parse error on raw chunk: ...` (confirms this bug firing again).

Confirmed on 2026-08-29: channel 13 (index 12) is 144.950 MHz FM, no
tone, WIDE -- matches N4SER-10's published frequency (Sarasota, FL;
matches this radio's GPS fix), and the SABM frame the bridge sent
decoded correctly to a real AX.25 connect request addressed to N4SER.

Also on 2026-08-29: with the fixes above (proactive listen-task-death
detection especially) plus APRS decoding disabled on the radio itself
(user turned it off mid-session, removing the main trigger for the
desync bug), the bridge went fully stable -- 4 clean SABM
retransmissions over a full ~45s AX.25 retry cycle, all sent and acked,
zero crashes. Software chain (Pat -> kernel AX.25 -> bridge -> BT ->
radio) confirmed working end-to-end. Still no reply heard from N4SER-10
in that run, so `kiss_bridge.py` now also defaults `kiss_tx_delay` to
300ms / `kiss_tx_tail` to 50ms on connect if they're 0 (this radio's
default) -- 0ms TXDELAY risks keying up and sending data before the
PA/transmitter stabilizes, clipping the start of every packet. Not yet
confirmed whether that gets an actual reply -- if not, next things to
check are physical (does the radio visibly key up / TX on send?
antenna? is N4SER-10 actually up right now?), not the bridge.

## Not yet built

- Nothing tracked right now.
