"""Terminal keyboard decoding, input layout and reader primitives."""
from __future__ import annotations
import os
import re
import select
import sys
import time
from dataclasses import dataclass
from typing import Any
from .config import FAST_PAGE_SCROLL_PAGES

@dataclass
class Key:
    name: str
    char: str = ""


@dataclass
class InputLayout:
    rows: list[str]
    cursor_row: int
    cursor_col: int


def key_from_control_char(char: str) -> Key:
    if char in ("\r", "\n"):
        return Key("enter")
    if char in ("\b", "\x7f"):
        return Key("backspace")
    if char == "\x03":
        return Key("quit")
    if char == "\x1d":
        return Key("quit")
    if char == "\x0c":
        return Key("redraw")
    if char == "\x15":
        return Key("clear_line")
    if char == "\x17":
        return Key("delete_word")
    if char == "\x01":
        return Key("home")
    if char == "\x05":
        return Key("end")
    if char == "\x16":
        return Key("paste")
    if char == "\x0e":
        return Key("newline")
    if char == "\x0b":
        return Key("delete_to_end")
    if char == "\t":
        return Key("switch_input")
    if char >= " ":
        return Key("char", char)
    return Key(f"control:{ord(char)}")


def key_from_ansi_escape(sequence: str) -> Key:
    if sequence == "\x1b":
        return Key("escape")
    if sequence.startswith("\x1bO"):
        return {
            "P": Key("f1"),
            "Q": Key("f2"),
            "R": Key("f3"),
            "S": Key("f4"),
            "H": Key("home"),
            "F": Key("end"),
        }.get(sequence[2:], Key(f"escape:{sequence[2:]!r}"))
    if not sequence.startswith("\x1b["):
        return Key(f"escape:{sequence[1:]!r}")
    code = sequence[2:]
    simple = {
        "A": Key("up"),
        "B": Key("down"),
        "C": Key("right"),
        "D": Key("left"),
        "H": Key("home"),
        "F": Key("end"),
        "1~": Key("home"),
        "3~": Key("delete"),
        "4~": Key("end"),
        "5~": Key("page_up"),
        "6~": Key("page_down"),
        "15~": Key("f5"),
    }
    if code in simple:
        return simple[code]
    ctrl_match = re.fullmatch(r"1;5([ABCD])", code)
    if ctrl_match:
        return {
            "A": Key("ctrl_up"),
            "B": Key("ctrl_down"),
            "C": Key("ctrl_right"),
            "D": Key("ctrl_left"),
        }[ctrl_match.group(1)]
    return Key(f"escape:{code!r}")


def layout_input_buffer(buffer: str, cursor: int, width: int, prompt: str, secret: bool = False) -> InputLayout:
    cursor = max(0, min(len(buffer), cursor))
    continuation = " " * len(prompt)
    prefix = prompt
    rows: list[str] = []
    content = ""
    cursor_row = 0
    cursor_col = len(prompt) + 1
    cursor_recorded = False

    def capacity(current_prefix: str) -> int:
        return max(1, width - len(current_prefix))

    def record_cursor() -> None:
        nonlocal cursor_row, cursor_col, cursor_recorded
        cursor_row = len(rows)
        cursor_col = len(prefix) + len(content) + 1
        cursor_recorded = True

    for index, char in enumerate(buffer):
        if char != "\n" and len(content) >= capacity(prefix):
            rows.append(prefix + content)
            prefix = continuation
            content = ""
        if index == cursor:
            record_cursor()
        if char == "\n":
            rows.append(prefix + content)
            prefix = continuation
            content = ""
            continue
        content += "*" if secret else char
    if not cursor_recorded:
        record_cursor()
    rows.append(prefix + content)
    return InputLayout(rows=rows, cursor_row=cursor_row, cursor_col=cursor_col)


def fast_page_scroll_amount(body_height: int) -> int:
    return max(1, (body_height - 1) * FAST_PAGE_SCROLL_PAGES)


class KeyReader:
    EXTENDED_KEYS = {
        "H": "up",
        "P": "down",
        "K": "left",
        "M": "right",
        "I": "page_up",
        "Q": "page_down",
        "G": "home",
        "O": "end",
        "S": "delete",
        "R": "insert",
        ";": "f1",
        "<": "f2",
        "=": "f3",
        ">": "f4",
        "?": "f5",
        "\x8d": "ctrl_up",
        "\x91": "ctrl_down",
        "s": "ctrl_left",
        "t": "ctrl_right",
    }

    def __init__(self) -> None:
        self.msvcrt = None
        self.termios = None
        self.original_termios: list[Any] | None = None
        if os.name == "nt":
            import msvcrt

            self.msvcrt = msvcrt
            return
        if not sys.stdin.isatty():
            raise SystemExit("This interactive client requires a terminal")
        import termios
        import tty

        self.termios = termios
        self.original_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def close(self) -> None:
        if self.termios is not None and self.original_termios is not None:
            self.termios.tcsetattr(sys.stdin.fileno(), self.termios.TCSADRAIN, self.original_termios)
            self.original_termios = None

    def _read_unix_char(self, timeout: float) -> str | None:
        readable, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
        if not readable:
            return None
        return os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")

    def _read_unix_escape(self) -> Key:
        chars = ["\x1b"]
        first = self._read_unix_char(0.02)
        if first is None:
            return Key("escape")
        chars.append(first)
        if first not in {"[", "O"}:
            return key_from_ansi_escape("".join(chars))
        while True:
            char = self._read_unix_char(0.02)
            if char is None:
                break
            chars.append(char)
            if first == "O" or char.isalpha() or char == "~":
                break
        return key_from_ansi_escape("".join(chars))

    def read_key(self, timeout: float = 0.1) -> Key | None:
        if self.msvcrt is None:
            char = self._read_unix_char(timeout)
            if char is None:
                return None
            if char == "\x1b":
                return self._read_unix_escape()
            return key_from_control_char(char)

        deadline = time.monotonic() + timeout
        while not self.msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)
        char = self.msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            code = self.msvcrt.getwch()
            return Key(self.EXTENDED_KEYS.get(code, f"extended:{ord(code)}"))
        if char == "\x1b":
            return Key("escape")
        return key_from_control_char(char)


