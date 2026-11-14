"""Safe ANSI processing shared by transcripts and terminal rendering."""
from __future__ import annotations
import codecs
import re
from dataclasses import dataclass
from .config import OUTBOUND_BUFFER_CHARS, OUTBOUND_SPOOF_PREFIX

ANSI_CSI_RE = re.compile(r"\x1b\[([0-?]*[ -/]*[@-~])")
ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
ANSI_DROP_RE = re.compile(r"\x1b(?:P|X|\^|_).*?\x1b\\", re.DOTALL)
ANSI_UNSAFE_ESC_RE = re.compile(r"\x1b(?!\[[0-9;]*m).")
ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
WATCH_COLOR_CODES = {
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "orange": "33",
    "blue": "34",
    "magenta": "35",
    "purple": "35",
    "cyan": "36",
    "white": "37",
    "default": "39",
    "gray": "90",
    "grey": "90",
    "bright-black": "90",
    "bright-gray": "90",
    "bright-grey": "90",
    "bright-red": "91",
    "bright-green": "92",
    "bright-yellow": "93",
    "bright-blue": "94",
    "bright-magenta": "95",
    "bright-purple": "95",
    "bright-cyan": "96",
    "bright-white": "97",
}
WATCH_RESET = "\x1b[0m"


def sanitize_terminal_text(text: str) -> str:
    """Keep MUD color SGR while dropping risky terminal controls."""
    text = ANSI_OSC_RE.sub("", text)
    text = ANSI_DROP_RE.sub("", text)

    def keep_safe_csi(match: re.Match[str]) -> str:
        sequence = match.group(1)
        if sequence.endswith("m"):
            return match.group(0)
        return ""

    text = ANSI_CSI_RE.sub(keep_safe_csi, text)
    text = ANSI_UNSAFE_ESC_RE.sub("", text)
    return text



class TerminalStreamDecoder:
    """Decode fragmented text and allow only complete, safe SGR sequences."""

    def __init__(self, encoding: str) -> None:
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.state = "text"
        self.escape = ""
        self.max_escape = 128

    def feed(self, data: bytes) -> str:
        result: list[str] = []
        for char in self.decoder.decode(data):
            if self.state == "text":
                if char == "\x1b":
                    self.state = "esc"
                elif char not in ("\x7f",) and not ("\x80" <= char <= "\x9f"):
                    result.append(char)
            elif self.state == "esc":
                if char == "[":
                    self.state = "csi"
                    self.escape = ""
                elif char == "]":
                    self.state = "osc"
                elif char in ("P", "X", "^", "_"):
                    self.state = "string"
                else:
                    self.state = "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    if char == "m" and re.fullmatch(r"[0-9;]*", self.escape):
                        result.append("\x1b[" + self.escape + "m")
                    self.state = "text"
                    self.escape = ""
                elif len(self.escape) >= self.max_escape:
                    self.state = "discard_csi"
                    self.escape = ""
                else:
                    self.escape += char
            elif self.state == "discard_csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state in ("osc", "string"):
                if char == "\x07" and self.state == "osc":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_esc" if self.state == "string" else "osc_esc"
            elif self.state in ("osc_esc", "string_esc"):
                if char == "\\":
                    self.state = "text"
                elif char != "\x1b":
                    self.state = "osc" if self.state == "osc_esc" else "string"
        return "".join(result)


def strip_ansi(text: str) -> str:
    return ANSI_CSI_RE.sub("", ANSI_OSC_RE.sub("", text))


def _split_at_whitespace(text: str, capacity: int) -> int:
    """Prefer a whole-word boundary; hard-split only when unavoidable."""
    if len(text) <= capacity:
        return len(text)
    for index in range(capacity - 1, -1, -1):
        if text[index].isspace():
            return index + 1
    return capacity


def iter_outbound_buffers(line: str):
    """Yield exact canonical chunks without retaining the whole output list.

    The public list-returning splitter stays unchanged for Python/Rust contract
    consumers; live sends use this iterator to keep transient RAM bounded.
    """
    if len(line) <= OUTBOUND_BUFFER_CHARS:
        yield line
        return

    match = re.match(r"^([^=\\r\\n]+=)", line)
    command_prefix = match.group(1) if match else ""
    if command_prefix and len(command_prefix) < OUTBOUND_BUFFER_CHARS:
        position = len(command_prefix)
        prefix = continuation_prefix = command_prefix
    else:
        position = 0
        prefix = ""
        continuation_prefix = OUTBOUND_SPOOF_PREFIX

    while position < len(line):
        capacity = OUTBOUND_BUFFER_CHARS - len(prefix)
        # Only inspect a bounded window. Slicing the entire suffix on every
        # iteration was quadratic for very long input.
        cutoff = _split_at_whitespace(line[position : position + capacity + 1], capacity)
        yield prefix + line[position : position + cutoff]
        position += cutoff
        prefix = continuation_prefix


def split_outbound_buffers(line: str) -> list[str]:
    """Legacy materialized splitter used by the shared Python/Rust fixtures."""
    return list(iter_outbound_buffers(line))
