"""Minimal AX.25 (v2.0/2.2, mod-8) frame decoder for channel_monitor_gui.py.

Decodes just enough of the address/control/PID header to show
source/destination/digipeater path, frame type, and protocol ID -- not a
full AX.25 stack (no state machine, no mod-128 extended sequence
numbers). Input is one complete, reassembled AX.25 frame (same bytes
kiss_bridge.py hands to kissattach inside a KISS frame) -- no FEND
framing, no CRC, since KISS already strips both.
"""

from dataclasses import dataclass

# Standard AX.25 PID values (AX.25 v2.2 spec, section 4.3.3.9).
PID_NAMES = {
    0x01: "ISO 8208/X.25 PLP",
    0x06: "Compressed TCP/IP",
    0x07: "Uncompressed TCP/IP",
    0x08: "Segmentation fragment",
    0xC3: "TEXNET datagram",
    0xC4: "Link Quality Protocol",
    0xCA: "AppleTalk",
    0xCB: "AppleTalk ARP",
    0xCC: "ARPA IP",
    0xCD: "ARPA ARP",
    0xCE: "FlexNet",
    0xCF: "NET/ROM",
    0xF0: "No layer 3 (raw text/APRS)",
    0xFF: "Escape (next byte is PID)",
}

# U-frame modifier byte (control with the P/F bit at 0x10 masked out) ->
# name. UI is the one that matters most on the air (APRS, NET/ROM
# beacons, unconnected text); the rest are here mainly so a connected
# AX.25/Winlink session shows something readable instead of "U (0x2F)".
U_FRAME_NAMES = {
    0x2F: "SABM",
    0x6F: "SABME",
    0x43: "DISC",
    0x0F: "DM",
    0x63: "UA",
    0x87: "FRMR",
    0x03: "UI",
}

S_FRAME_NAMES = {0: "RR", 1: "RNR", 2: "REJ", 3: "SREJ"}


@dataclass
class Address:
    callsign: str
    ssid: int
    # Same bit position, different meaning depending on which address this
    # is: command/response (C) bit for dest/source, "has been repeated"
    # (H) bit for a digipeater. Not rendered by __str__ for that reason --
    # see Frame.path, the only place it's meaningful to show.
    flag: bool

    def __str__(self) -> str:
        return f"{self.callsign}-{self.ssid}" if self.ssid else self.callsign


@dataclass
class Frame:
    dest: Address
    source: Address
    digipeaters: list[Address]
    frame_type: str  # "I", "S", or "U"
    subtype: str      # e.g. "I", "RR", "UI", "SABM", or "U (0xNN)" if unrecognized
    poll_final: bool
    ns: int | None
    nr: int | None
    pid: int | None
    payload: bytes
    raw: bytes

    @property
    def path(self) -> str:
        # '*' marks a digipeater that has already repeated this frame
        # (its H-bit) -- standard AX.25 monitor convention (e.g. axlisten).
        return ",".join(f"{a}*" if a.flag else str(a) for a in self.digipeaters)

    @property
    def payload_ascii(self) -> str:
        return "".join(chr(b) if 32 <= b < 127 else "." for b in self.payload)

    @property
    def payload_hex(self) -> str:
        return self.payload.hex(" ")

    @property
    def pid_name(self) -> str:
        return pid_name(self.pid)


class FrameError(ValueError):
    """Raised when `raw` is too short or malformed to be a real AX.25
    frame -- e.g. line noise, or a non-AX.25 KISS hardware-param frame
    that reached decode() by mistake."""


def pid_name(pid: int | None) -> str:
    if pid is None:
        return ""
    return PID_NAMES.get(pid, f"Unknown (0x{pid:02X})")


def _decode_address(raw: bytes) -> Address:
    # Each callsign character is ASCII shifted left 1 bit (bit 0 is used
    # for the address-field extension/SSID flags instead); the SSID byte
    # packs SSID into bits 4-1 with the same left-shift.
    callsign = "".join(chr(b >> 1) for b in raw[:6]).rstrip(" ")
    ssid_byte = raw[6]
    return Address(
        callsign=callsign, ssid=(ssid_byte >> 1) & 0x0F, flag=bool(ssid_byte & 0x80),
    )


def decode(raw: bytes) -> Frame:
    """Decode one complete AX.25 frame. Raises FrameError if it doesn't
    look like one."""
    if len(raw) < 15:  # dest(7) + source(7) + control(1), minimum
        raise FrameError(f"frame too short ({len(raw)} bytes)")

    pos = 0
    addresses = []
    while True:
        if pos + 7 > len(raw):
            raise FrameError("truncated address field")
        chunk = raw[pos:pos + 7]
        addresses.append(_decode_address(chunk))
        pos += 7
        if chunk[6] & 0x01:  # extension bit set: this was the last address
            break
        if len(addresses) >= 10:  # 2 + up to 8 digipeaters, per spec
            raise FrameError("too many address fields (not AX.25?)")

    dest, source, *digipeaters = addresses

    if pos >= len(raw):
        raise FrameError("missing control field")
    control = raw[pos]
    pos += 1

    poll_final = bool(control & 0x10)
    pid = None
    if control & 0x01 == 0:
        frame_type, subtype = "I", "I"
        ns, nr = (control >> 1) & 0x07, (control >> 5) & 0x07
    elif control & 0x03 == 0x01:
        frame_type, ns, nr = "S", None, (control >> 5) & 0x07
        subtype = S_FRAME_NAMES.get((control >> 2) & 0x03, "S?")
    else:
        frame_type, ns, nr = "U", None, None
        modifier = control & ~0x10
        subtype = U_FRAME_NAMES.get(modifier, f"U (0x{control:02X})")

    if frame_type == "I" or subtype == "UI":
        if pos >= len(raw):
            raise FrameError("missing PID byte")
        pid = raw[pos]
        pos += 1

    return Frame(
        dest=dest, source=source, digipeaters=digipeaters,
        frame_type=frame_type, subtype=subtype, poll_final=poll_final,
        ns=ns, nr=nr, pid=pid, payload=raw[pos:], raw=raw,
    )
