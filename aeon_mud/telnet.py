"""Stateful Telnet negotiation codec, independent of network transport."""
from __future__ import annotations
import struct
from .config import MAX_TELNET_SUBNEGOTIATION_BYTES

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240
NOP = 241

TELNET_ECHO = 1
TELNET_SGA = 3
TELNET_TTYPE = 24
TELNET_NAWS = 31
TELNET_LINEMODE = 34

TTYPE_IS = 0
TTYPE_SEND = 1


class TelnetCodec:
    def __init__(self, terminal_type: str = "ANSI") -> None:
        self.state = "data"
        self.command = 0
        self.sb_data = bytearray()
        self.sb_overflow = False
        self.terminal_type = terminal_type.encode("ascii", "replace")[:40] or b"ANSI"
        self.width = 100
        self.height = 30

    def set_window_size(self, width: int, height: int) -> bytes:
        self.width = max(1, min(width, 65535))
        self.height = max(1, min(height, 65535))
        return self._naws_bytes()

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        output = bytearray()
        responses = bytearray()
        for byte in data:
            if self.state == "data":
                if byte == IAC:
                    self.state = "iac"
                else:
                    output.append(byte)
            elif self.state == "iac":
                if byte == IAC:
                    output.append(IAC)
                    self.state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self.command = byte
                    self.state = "option"
                elif byte == SB:
                    self.sb_data = bytearray()
                    self.sb_overflow = False
                    self.state = "sb"
                else:
                    self.state = "data"
            elif self.state == "option":
                responses.extend(self._negotiate(self.command, byte))
                self.state = "data"
            elif self.state == "sb":
                if byte == IAC:
                    self.state = "sb_iac"
                elif not self.sb_overflow:
                    if len(self.sb_data) < MAX_TELNET_SUBNEGOTIATION_BYTES:
                        self.sb_data.append(byte)
                    else:
                        self.sb_overflow = True
                        self.sb_data.clear()
            elif self.state == "sb_iac":
                if byte == SE:
                    if not self.sb_overflow:
                        responses.extend(self._subnegotiate(bytes(self.sb_data)))
                    self.sb_data.clear()
                    self.sb_overflow = False
                    self.state = "data"
                else:
                    if not self.sb_overflow:
                        escaped = (IAC,) if byte == IAC else (IAC, byte)
                        if len(self.sb_data) + len(escaped) <= MAX_TELNET_SUBNEGOTIATION_BYTES:
                            self.sb_data.extend(escaped)
                        else:
                            self.sb_overflow = True
                            self.sb_data.clear()
                    self.state = "sb"
        return bytes(output), bytes(responses)

    def _cmd(self, command: int, option: int) -> bytes:
        return bytes([IAC, command, option])

    def _negotiate(self, command: int, option: int) -> bytes:
        if command == DO:
            if option in (TELNET_SGA, TELNET_NAWS, TELNET_TTYPE):
                response = bytearray(self._cmd(WILL, option))
                if option == TELNET_NAWS:
                    response.extend(self._naws_bytes())
                return bytes(response)
            return self._cmd(WONT, option)
        if command == WILL:
            if option in (TELNET_ECHO, TELNET_SGA):
                return self._cmd(DO, option)
            return self._cmd(DONT, option)
        if command in (DONT, WONT):
            return b""
        return b""

    def _subnegotiate(self, data: bytes) -> bytes:
        if not data:
            return b""
        option = data[0]
        payload = data[1:]
        if option == TELNET_TTYPE and payload[:1] == bytes([TTYPE_SEND]):
            return bytes([IAC, SB, TELNET_TTYPE, TTYPE_IS]) + self.terminal_type + bytes([IAC, SE])
        return b""

    def _naws_bytes(self) -> bytes:
        width = struct.pack("!H", self.width).replace(bytes([IAC]), bytes([IAC, IAC]))
        height = struct.pack("!H", self.height).replace(bytes([IAC]), bytes([IAC, IAC]))
        return bytes([IAC, SB, TELNET_NAWS]) + width + height + bytes([IAC, SE])


