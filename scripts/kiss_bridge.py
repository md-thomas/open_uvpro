"""Bridge the UV-Pro's built-in TNC to a standard KISS pty, so Linux's
AX.25 stack (and anything built on it, e.g. Pat) can use the radio as a
packet TNC over Bluetooth.

The radio doesn't expose raw KISS bytes on the wire -- its TNC data rides
inside the same benlink command protocol as everything else, as
fragmented HT_SEND_DATA / DATA_RXD messages capped at 50 bytes/fragment.
This script does the translation: it opens a pty, speaks real KISS
framing (FEND/FESC escaping) on it, and fragments/reassembles across the
radio connection underneath.

Usage:
    python scripts/kiss_bridge.py XX:XX:XX:XX:XX:XX [pty_symlink_path]

The pty symlink defaults to ~/.cache, not /tmp: kissattach runs as root
via sudo, and the kernel's fs.protected_symlinks hardening (on by
default) refuses to let root follow a symlink owned by another user
sitting in a sticky world-writable directory like /tmp -- it fails with
"open: Permission denied" even though the file permissions look fine.
~/.cache isn't sticky/world-writable, so that restriction never triggers.

Then, in another terminal (see NOTES.md for the one-time axports setup):
    sudo kissattach ~/.cache/uvpro-kisstnc wl2k
    pat connect ax25:///SOME-CALL

Leaves kiss_en enabled on the radio (it's already on by default on this
unit). Enables it if it was off, but doesn't try to restore it on exit --
see the reconnect note below for why.

The radio's Bluetooth link can drop mid-session (known flakiness -- see
NOTES.md). The pty/symlink stay put across a drop; only the RFCOMM
connection is retried (with backoff), so kissattach doesn't need to be
re-run each time -- it just sees a stall while we reconnect.
"""

import asyncio
import errno
import os
import pty
import signal
import sys
import termios

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.command import TncDataFragment, TncDataFragmentReceivedEvent
from benlink.controller import RadioController
from radio_connect import connect_rfcomm, log

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

MAX_FRAGMENT_SIZE = 50
SEND_TIMEOUT_SECONDS = 10
DEFAULT_PTY_PATH = os.path.expanduser("~/.cache/uvpro-kisstnc")


def kiss_encode(payload: bytes, port: int = 0) -> bytes:
    body = bytearray([port << 4])
    for b in payload:
        if b == FEND:
            body += bytes([FESC, TFEND])
        elif b == FESC:
            body += bytes([FESC, TFESC])
        else:
            body.append(b)
    return bytes([FEND, *body, FEND])


class KissDecoder:
    """Incrementally decodes a byte stream into complete KISS frames."""

    def __init__(self):
        self._buf = bytearray()
        self._in_frame = False
        self._escaped = False

    def feed(self, data: bytes) -> list[bytes]:
        frames = []
        for b in data:
            if b == FEND:
                if self._in_frame and self._buf:
                    frames.append(bytes(self._buf))
                self._buf = bytearray()
                self._in_frame = True
                self._escaped = False
                continue
            if not self._in_frame:
                continue  # noise between frames
            if self._escaped:
                if b == TFEND:
                    self._buf.append(FEND)
                elif b == TFESC:
                    self._buf.append(FESC)
                else:
                    self._buf.append(b)  # malformed escape; pass through
                self._escaped = False
            elif b == FESC:
                self._escaped = True
            else:
                self._buf.append(b)
        return frames


async def send_frame_to_radio(radio: RadioController, frame: bytes) -> None:
    if not frame:
        return
    command, payload = frame[0], frame[1:]
    kind = command & 0x0F
    if kind == 0x01 and payload:  # TXDELAY, 10ms units
        await radio.set_settings(kiss_tx_delay=payload[0])
        return
    if kind == 0x04 and payload:  # TXTAIL, 10ms units
        await radio.set_settings(kiss_tx_tail=payload[0])
        return
    if kind != 0x00:
        return  # other hardware param commands (persist/slottime/fullduplex) -- ignored

    chunks = [
        payload[i:i + MAX_FRAGMENT_SIZE]
        for i in range(0, len(payload), MAX_FRAGMENT_SIZE)
    ] or [b""]

    log(f"TX: {len(payload)} bytes -> {len(chunks)} fragment(s): {payload.hex()}")
    for i, chunk in enumerate(chunks):
        # The radio's Gaia-protocol read loop can silently die on a stream
        # desync (a real, recurring bug -- see NOTES.md) without surfacing
        # any error here, so an un-timed-out await hangs forever waiting
        # for a reply that will never come. Bound it so a dead connection
        # turns into an exception the outer reconnect loop can act on.
        await asyncio.wait_for(
            radio._conn.send_tnc_data_fragment(TncDataFragment(
                is_final_fragment=(i == len(chunks) - 1),
                fragment_id=i % 64,
                data=chunk,
            )),
            timeout=SEND_TIMEOUT_SECONDS,
        )
    log("TX: all fragments acked by radio")


def make_pty(symlink_path: str) -> int:
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)

    # Raw mode: no echo, no line editing, no CR/NL translation -- KISS is
    # a binary framing and must pass through untouched.
    tty_attrs = termios.tcgetattr(slave_fd)
    tty_attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
    tty_attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR)
    tty_attrs[1] &= ~termios.OPOST
    termios.tcsetattr(slave_fd, termios.TCSANOW, tty_attrs)
    os.close(slave_fd)

    os.makedirs(os.path.dirname(symlink_path) or ".", exist_ok=True)
    if os.path.islink(symlink_path) or os.path.exists(symlink_path):
        os.remove(symlink_path)
    os.symlink(slave_path, symlink_path)

    return master_fd


RECONNECT_BACKOFF_SECONDS = 15


async def run_bridge(device_uuid: str, symlink_path: str) -> None:
    master_fd = make_pty(symlink_path)
    log(f"KISS TNC available at {symlink_path} -> {os.readlink(symlink_path)}")
    log("Attach it with: sudo kissattach " + symlink_path + " wl2k")

    decoder = KissDecoder()
    out_queue: asyncio.Queue[bytes] = asyncio.Queue()

    loop = asyncio.get_running_loop()

    def on_master_readable():
        try:
            data = os.read(master_fd, 4096)
        except OSError as e:
            if e.errno == errno.EIO:
                # No process currently has the pty slave open (e.g. before
                # kissattach attaches, or after it exits). The fd stays
                # readable-with-error in this state, so without backing
                # off here the event loop would call this callback in an
                # unthrottled spin -- 100% CPU, no data, no log output.
                log("pty slave not open yet (EIO); waiting for kissattach...")
            else:
                log(f"pty read error: {e!r}")
            loop.remove_reader(master_fd)
            loop.call_later(1.0, lambda: loop.add_reader(master_fd, on_master_readable))
            return
        if not data:
            loop.remove_reader(master_fd)
            loop.call_later(1.0, lambda: loop.add_reader(master_fd, on_master_readable))
            return
        for frame in decoder.feed(data):
            out_queue.put_nowait(frame)

    loop.add_reader(master_fd, on_master_readable)

    try:
        while True:
            try:
                await _run_one_connection(device_uuid, master_fd, out_queue)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"Radio connection lost ({e!r}); reconnecting in "
                    f"{RECONNECT_BACKOFF_SECONDS}s...")
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
    finally:
        loop.remove_reader(master_fd)
        os.close(master_fd)
        os.remove(symlink_path)


async def _run_one_connection(
    device_uuid: str, master_fd: int, out_queue: "asyncio.Queue[bytes]"
) -> None:
    async with connect_rfcomm(device_uuid, "auto") as radio:
        if not radio.settings.kiss_en:
            await radio.set_settings(kiss_en=True)
            log("Enabled kiss_en on radio")

        # 0ms TXDELAY (this radio's default) risks keying up and sending
        # data before the PA/transmitter has actually stabilized, clipping
        # the start of every packet so the far end can't decode it. Give
        # it a real default unless kissattach/kissparms already set one via
        # a KISS TXDELAY frame, or a previous run of this script did.
        if radio.settings.kiss_tx_delay == 0:
            await radio.set_settings(kiss_tx_delay=30, kiss_tx_tail=5)
            log("Set kiss_tx_delay=300ms, kiss_tx_tail=50ms (were 0/0)")

        reassembly = bytearray()

        def on_event(event):
            nonlocal reassembly
            if isinstance(event, TncDataFragmentReceivedEvent):
                reassembly += event.tnc_data_fragment.data
                if event.tnc_data_fragment.is_final_fragment:
                    log(f"RX: {len(reassembly)} bytes: {bytes(reassembly).hex()}")
                    os.write(master_fd, kiss_encode(bytes(reassembly)))
                    reassembly = bytearray()

        radio.add_event_handler(on_event)

        # The background read loop can die silently (e.g. a Gaia-protocol
        # desync on real off-air packet traffic -- see NOTES.md) without
        # anything here noticing until the next send is attempted, which
        # can be a long time after the connection actually went dead.
        # Watch it directly so a death is caught immediately instead.
        listen_task = radio._conn._link._client._st.listen_task

        log("Bridge active. Ctrl+C to stop.")
        while True:
            get_task = asyncio.ensure_future(out_queue.get())
            done, _ = await asyncio.wait(
                {get_task, listen_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if listen_task in done:
                get_task.cancel()
                exc = listen_task.exception() if not listen_task.cancelled() else None
                raise exc if exc else ConnectionError("radio link closed")
            await send_frame_to_radio(radio, get_task.result())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        log(f"Usage: {sys.argv[0]} XX:XX:XX:XX:XX:XX [pty_symlink_path]")
        sys.exit(1)

    device_uuid = sys.argv[1]
    symlink_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PTY_PATH

    async def main() -> None:
        task = asyncio.ensure_future(run_bridge(device_uuid, symlink_path))
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
