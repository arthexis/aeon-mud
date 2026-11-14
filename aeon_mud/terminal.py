"""MUD terminal rendering, dual composer state and watch formatting."""
from __future__ import annotations
import ctypes
import os
import re
import shutil
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .config import *
from .profiles import Profile, HistoryStore
from .terminal_text import *
from .input import InputLayout, layout_input_buffer

@dataclass(frozen=True)
class WatchRule:
    pattern: str
    color: str
    sgr: str
    regex: re.Pattern[str]

    def matches(self, line: str) -> bool:
        return self.regex.match(strip_ansi(str(line))) is not None


def strip_outer_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def resolve_watch_color(color: str) -> tuple[str, str]:
    normalized = color.strip().lower().replace("_", "-")
    if not normalized:
        raise ValueError("color must not be empty")
    code = WATCH_COLOR_CODES.get(normalized)
    if code is not None:
        return normalized, f"\x1b[{code}m"
    if normalized.startswith("bold-"):
        base = normalized[5:]
        code = WATCH_COLOR_CODES.get(base)
        if code is not None:
            return normalized, f"\x1b[1;{code}m"
    raise ValueError("unknown color; use red, green, yellow, blue, magenta, cyan, white, gray, or bright-*")


def compile_watch_pattern(pattern: str) -> re.Pattern[str]:
    text = strip_outer_quotes(pattern)
    if not text:
        raise ValueError("pattern must not be empty")
    regex = "^" + ".*".join(re.escape(part) for part in text.split("*"))
    return re.compile(regex)


def make_watch_rule(pattern: str, color: str) -> WatchRule:
    clean_pattern = strip_outer_quotes(pattern)
    clean_color, sgr = resolve_watch_color(color)
    return WatchRule(
        pattern=clean_pattern,
        color=clean_color,
        sgr=sgr,
        regex=compile_watch_pattern(clean_pattern),
    )


def apply_watch_rules(line: str, rules: list[WatchRule]) -> str:
    for rule in reversed(rules):
        try:
            matched = rule.matches(line)
        except Exception:
            continue
        if matched:
            return f"{rule.sgr}{line}{WATCH_RESET}"
    return line


def describe_watch_rules(rules: list[WatchRule]) -> str:
    if not rules:
        return "No watch rules"
    return "Watches: " + ", ".join(f"{rule.pattern}={rule.color}" for rule in rules)


def parse_watch_arguments(argument: str) -> tuple[str, str]:
    text = argument.strip()
    if not text:
        raise ValueError("usage: /watch pattern color")
    parts = text.rsplit(None, 1)
    if len(parts) != 2:
        raise ValueError("usage: /watch pattern color")
    pattern, color = parts
    pattern = strip_outer_quotes(pattern)
    if not pattern:
        raise ValueError("pattern must not be empty")
    return pattern, color


def watch_rules_to_data(rules: list[WatchRule]) -> list[dict[str, str]]:
    return [{"pattern": rule.pattern, "color": rule.color} for rule in rules]


def watch_rules_from_data(data: Any) -> list[WatchRule]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("watch_rules must be a list")
    rules: list[WatchRule] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"watch rule {index} must be an object")
        pattern = item.get("pattern")
        color = item.get("color")
        if not isinstance(pattern, str) or not isinstance(color, str):
            raise ValueError(f"watch rule {index} must include string pattern and color")
        rules.append(make_watch_rule(pattern, color))
    return rules


def truncate_plain(text: str, width: int) -> str:
    plain = strip_ansi(text)
    if width <= 0:
        return ""
    if len(plain) <= width:
        return plain
    return plain[: max(0, width - 1)] + ">"


def truncate_ansi(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out: list[str] = []
    visible = 0
    index = 0
    for match in ANSI_SGR_RE.finditer(text):
        if match.start() > index:
            chunk = text[index : match.start()]
            for char in chunk:
                if visible >= width:
                    return "".join(out)
                out.append(char)
                visible += 1
        out.append(match.group(0))
        index = match.end()
    if index < len(text):
        for char in text[index:]:
            if visible >= width:
                break
            out.append(char)
            visible += 1
    return "".join(out)


def update_sgr_state(active: str, sequence: str) -> str:
    params = sequence[2:-1]
    raw_codes = ["0"] if not params else params.split(";")
    codes: list[int] = []
    for raw_code in raw_codes:
        try:
            codes.append(int(raw_code or "0"))
        except ValueError:
            return active + sequence if len(active) + len(sequence) <= MAX_ACTIVE_SGR_CHARS else "\x1b[0m"
    if len(sequence) > MAX_ACTIVE_SGR_CHARS:
        return "\x1b[0m"
    if 0 not in codes:
        if len(active) + len(sequence) <= MAX_ACTIVE_SGR_CHARS:
            return active + sequence
        # A hostile stream must not accumulate an ever-growing SGR carry.
        # Reset at the next wrap boundary rather than replaying stale styles.
        return "\x1b[0m" + sequence
    reset_index = max(index for index, code in enumerate(codes) if code == 0)
    return sequence if any(code != 0 for code in codes[reset_index + 1 :]) else ""


def wrap_ansi(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    rows = [""]
    visible = 0
    active_sgr = ""
    index = 0
    for match in ANSI_SGR_RE.finditer(text):
        for char in text[index : match.start()]:
            if visible >= width:
                rows.append(active_sgr)
                visible = 0
            rows[-1] += char
            visible += 1
        sequence = match.group(0)
        rows[-1] += sequence
        active_sgr = update_sgr_state(active_sgr, sequence)
        index = match.end()
    for char in text[index:]:
        if visible >= width:
            rows.append(active_sgr)
            visible = 0
        rows[-1] += char
        visible += 1
    return rows


class MudUi:
    def __init__(self, profile: Profile, no_alt_screen: bool = False) -> None:
        self.profile = profile
        self.no_alt_screen = no_alt_screen
        self.lines: list[str] = [""]
        self.input_buffer = ""
        self.cursor = 0
        self.history = HistoryStore().list_for(profile)
        self.history_index: int | None = None
        # The active composer uses the existing fields; the inactive composer
        # has independent draft, cursor, and history navigation state.
        self.active_input = 0
        self.other_input_buffer = ""
        self.other_cursor = 0
        self.other_history_index: int | None = None
        self.history_draft = ""
        self.other_history_draft = ""
        self.scroll_offset = 0
        self.status_message = ""
        self.modal_message = ""
        self.secret_entry = False
        self.connected = True
        self.watch_rules: list[WatchRule] = []
        self.profile_config_path: Path | None = None
        self.lock = threading.RLock()
        self.started = False
        self.original_stdout_mode: int | None = None
        self._display_cache_width: int | None = None
        self._display_cache_rows: list[str] = []

    def _invalidate_display_cache(self) -> None:
        self._display_cache_width = None
        self._display_cache_rows = []

    def start(self) -> None:
        with self.lock:
            self._enable_vt()
            if not self.no_alt_screen:
                sys.stdout.write("\x1b[?1049h")
            self.started = True
            self.render(full_clear=True)

    def close(self) -> None:
        with self.lock:
            if self.started:
                sys.stdout.write("\x1b[?25h\x1b[0m")
                if not self.no_alt_screen:
                    sys.stdout.write("\x1b[?1049l")
                sys.stdout.flush()
            self.started = False
            if self.original_stdout_mode is not None and os.name == "nt":
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-11)
                kernel32.SetConsoleMode(handle, self.original_stdout_mode)
                self.original_stdout_mode = None

    def _enable_vt(self) -> None:
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            self.original_stdout_mode = mode.value
            kernel32.SetConsoleMode(handle, mode.value | 0x0004 | 0x0008)

    def set_connected(self, value: bool) -> None:
        with self.lock:
            self.connected = value
            self.render()

    def set_secret_entry(self, value: bool) -> None:
        with self.lock:
            if value and self.active_input != 0:
                # Keep password entry in the primary buffer, preserving both drafts.
                self.input_buffer, self.other_input_buffer = self.other_input_buffer, self.input_buffer
                self.cursor, self.other_cursor = self.other_cursor, self.cursor
                self.history_index, self.other_history_index = self.other_history_index, self.history_index
                self.history_draft, self.other_history_draft = self.other_history_draft, self.history_draft
                self.active_input = 0
            self.secret_entry = value
            self.render()

    def set_status(self, message: str) -> None:
        with self.lock:
            self.status_message = message
            self.render()

    def set_modal(self, message: str) -> None:
        with self.lock:
            self.modal_message = message
            self.render()

    def clear_modal(self) -> None:
        with self.lock:
            self.modal_message = ""
            self.render()

    def add_watch(self, pattern: str, color: str) -> WatchRule:
        rule = make_watch_rule(pattern, color)
        with self.lock:
            self.watch_rules = [existing for existing in self.watch_rules if existing.pattern != rule.pattern]
            self.watch_rules.append(rule)
            self._invalidate_display_cache()
            self.render()
        return rule

    def remove_watch(self, pattern: str) -> int:
        clean_pattern = strip_outer_quotes(pattern)
        with self.lock:
            original_count = len(self.watch_rules)
            self.watch_rules = [rule for rule in self.watch_rules if rule.pattern != clean_pattern]
            removed = original_count - len(self.watch_rules)
            if removed:
                self._invalidate_display_cache()
                self.render()
            return removed

    def clear_watches(self) -> int:
        with self.lock:
            removed = len(self.watch_rules)
            self.watch_rules = []
            if removed:
                self._invalidate_display_cache()
                self.render()
            return removed

    def replace_watches(self, rules: list[WatchRule]) -> None:
        with self.lock:
            self.watch_rules = list(rules)
            self._invalidate_display_cache()
            self.render()

    def describe_watches(self) -> str:
        with self.lock:
            return describe_watch_rules(self.watch_rules)

    def append_output(self, text: str) -> None:
        # Cap one received batch as well as retained lines. A long physical
        # line must not bypass max_scrollback (which counts lines only).
        text = text[-MAX_SCROLLBACK_TOTAL_CHARS:]
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        with self.lock:
            parts = text.split("\n")
            for index, part in enumerate(parts):
                if index > 0:
                    self.lines.append("")
                combined = self.lines[-1] + part
                # A clipped line must not start in the middle of an ANSI escape.
                self.lines[-1] = (strip_ansi(combined) if len(combined) > MAX_SCROLLBACK_LINE_CHARS else combined)[-MAX_SCROLLBACK_LINE_CHARS:]
            if len(self.lines) > self.profile.max_scrollback:
                self.lines = self.lines[-self.profile.max_scrollback:]
            retained = []
            budget = MAX_SCROLLBACK_TOTAL_CHARS
            for line in reversed(self.lines):
                if budget <= 0:
                    break
                value = line[-budget:]
                retained.append(value)
                budget -= len(value)
            self.lines = list(reversed(retained)) or [""]
            self._invalidate_display_cache()
            self.render()

    def _body_height(self) -> int:
        width, height = shutil.get_terminal_size((100, 30))
        input_rows, _, _ = self._input_window(width, height)
        return max(1, height - 1 - len(input_rows))

    def _display_lines(self, width: int) -> list[str]:
        if self._display_cache_width == width:
            return self._display_cache_rows
        rows: deque[str] = deque(maxlen=MAX_DISPLAY_CACHE_ROWS)
        rules = list(self.watch_rules)
        for line in self.lines:
            raw_line = str(line)
            bounded = (strip_ansi(raw_line) if len(raw_line) > MAX_SCROLLBACK_LINE_CHARS else raw_line)[-MAX_SCROLLBACK_LINE_CHARS:]
            try:
                display_line = apply_watch_rules(bounded, rules)
                rows.extend(wrap_ansi(display_line, width))
            except Exception:
                rows.extend(wrap_ansi(strip_ansi(bounded), width))
        self._display_cache_width = width
        self._display_cache_rows = list(rows) or [""]
        return self._display_cache_rows

    @staticmethod
    def _visible_input_rows(layout: InputLayout, limit: int) -> tuple[list[str], int]:
        if len(layout.rows) <= limit:
            return layout.rows, layout.cursor_row
        start = min(max(0, layout.cursor_row - limit + 1), len(layout.rows) - limit)
        return layout.rows[start : start + limit], layout.cursor_row - start

    def _input_window(self, width: int, height: int) -> tuple[list[str], int, int]:
        # Both input fields use the same bare prompt; focus is shown by the
        # terminal cursor, without labels or a separator row.
        # Prevent a small terminal from consuming all output space.
        max_rows = max(2, min(8, height - 2))
        active_limit = max(1, max_rows // 2)
        other_limit = max(1, max_rows - active_limit)
        active = layout_input_buffer(
            self.input_buffer, self.cursor, width, "> ", self.secret_entry,
        )
        inactive = layout_input_buffer(
            self.other_input_buffer, self.other_cursor, width, "> ",
            self.secret_entry,
        )
        active_rows, active_cursor_row = self._visible_input_rows(active, active_limit)
        other_rows, other_cursor_row = self._visible_input_rows(inactive, other_limit)
        if self.active_input == 0:
            return active_rows + other_rows, active_cursor_row, active.cursor_col
        return other_rows + active_rows, len(other_rows) + active_cursor_row, active.cursor_col

    def switch_input(self) -> None:
        with self.lock:
            # Keep passwords in the primary composer; don't expose a draft
            # while a secret prompt is being answered.
            if self.secret_entry:
                self.set_status("Switching inputs is disabled during secret entry")
                return
            self.input_buffer, self.other_input_buffer = self.other_input_buffer, self.input_buffer
            self.cursor, self.other_cursor = self.other_cursor, self.cursor
            self.history_index, self.other_history_index = self.other_history_index, self.history_index
            self.history_draft, self.other_history_draft = self.other_history_draft, self.history_draft
            self.active_input = 1 - self.active_input
            self.render()

    def delete_to_end(self) -> None:
        with self.lock:
            self.input_buffer = self.input_buffer[:self.cursor]
            self.history_index = None
            self.render()

    def move_word(self, direction: int) -> None:
        with self.lock:
            text = self.input_buffer
            pos = self.cursor
            if direction < 0:
                while pos > 0 and text[pos - 1].isspace():
                    pos -= 1
                while pos > 0 and not text[pos - 1].isspace():
                    pos -= 1
            else:
                while pos < len(text) and not text[pos].isspace():
                    pos += 1
                while pos < len(text) and text[pos].isspace():
                    pos += 1
            self.cursor = pos
            self.render()

    def scroll(self, amount: int) -> None:
        with self.lock:
            width = shutil.get_terminal_size((100, 30)).columns
            max_offset = max(0, len(self._display_lines(width)) - self._body_height())
            new_offset = min(max_offset, max(0, self.scroll_offset + amount))
            if new_offset != self.scroll_offset:
                self.scroll_offset = new_offset
                self.render()

    def bottom(self) -> None:
        with self.lock:
            if self.scroll_offset == 0:
                return
            self.scroll_offset = 0
            self.render()

    def insert_text(self, text: str) -> None:
        with self.lock:
            self.input_buffer = self.input_buffer[: self.cursor] + text + self.input_buffer[self.cursor :]
            self.cursor += len(text)
            self.history_index = None
            self.render()

    def replace_input(self, text: str) -> None:
        with self.lock:
            self.input_buffer = text
            self.cursor = len(text)
            self.render()

    def clear_input(self) -> None:
        with self.lock:
            self.history_index = None
            self.history_draft = ""
        self.replace_input("")

    def backspace(self) -> None:
        with self.lock:
            if self.cursor > 0:
                self.input_buffer = self.input_buffer[: self.cursor - 1] + self.input_buffer[self.cursor :]
                self.cursor -= 1
            self.render()

    def delete(self) -> None:
        with self.lock:
            if self.cursor < len(self.input_buffer):
                self.input_buffer = self.input_buffer[: self.cursor] + self.input_buffer[self.cursor + 1 :]
            self.render()

    def delete_word(self) -> None:
        with self.lock:
            left = self.input_buffer[: self.cursor].rstrip()
            cut = len(left)
            while cut > 0 and not left[cut - 1].isspace():
                cut -= 1
            self.input_buffer = self.input_buffer[:cut] + self.input_buffer[self.cursor :]
            self.cursor = cut
            self.render()

    def move_cursor(self, amount: int) -> None:
        with self.lock:
            self.cursor = max(0, min(len(self.input_buffer), self.cursor + amount))
            self.render()

    def history_prev(self) -> None:
        with self.lock:
            if not self.history:
                self.set_status("No command history for this profile")
                return
            if self.history_index is None:
                self.history_draft = self.input_buffer
                self.history_index = len(self.history) - 1
            else:
                self.history_index = max(0, self.history_index - 1)
            self.replace_input(self.history[self.history_index])

    def history_next(self) -> None:
        with self.lock:
            if not self.history:
                return
            if self.history_index is None:
                return
            if self.history_index >= len(self.history) - 1:
                self.history_index = None
                self.replace_input(self.history_draft)
            else:
                self.history_index += 1
                self.replace_input(self.history[self.history_index])

    def remember(self, command: str) -> None:
        if self.secret_entry:
            return
        HistoryStore().add(self.profile, command)
        self.history = HistoryStore().list_for(self.profile)

    def render(self, full_clear: bool = False) -> None:
        if not self.started:
            return
        width, height = shutil.get_terminal_size((100, 30))
        input_rows, input_cursor_row, input_cursor_col = self._input_window(width, height)
        body_height = max(1, height - 1 - len(input_rows))
        display_lines = self._display_lines(width)
        max_offset = max(0, len(display_lines) - body_height)
        self.scroll_offset = min(self.scroll_offset, max_offset)
        end = len(display_lines) - self.scroll_offset
        start = max(0, end - body_height)
        visible = display_lines[start:end]

        connection = "connected" if self.connected else "disconnected"
        log_state = "on" if self.profile.transcript != "off" else "off"
        status = (
            f" {self.profile.label()} {self.profile.host}:{self.profile.port} "
            f"PLAINTEXT TELNET {connection} "
            f"log:{log_state} "
            "TAB switch CTRL+LEFT/RIGHT words CTRL+K trim CTRL+UP/DOWN history CTRL+N newline CTRL+] quit"
        )
        if self.scroll_offset:
            status += f" scrollback:{self.scroll_offset}"
        if self.status_message:
            status += f" | {self.status_message}"
        if self.modal_message:
            status = f" {self.modal_message}"

        output = ["\x1b[?25l\x1b[H"]
        if full_clear:
            output.append("\x1b[2J")
        output.append("\x1b[7m" + truncate_plain(status, width).ljust(width) + "\x1b[0m")
        output.append("\n")

        for line in visible:
            output.append("\x1b[2K" + truncate_ansi(line, width) + "\x1b[0m\n")
        for _ in range(body_height - len(visible)):
            output.append("\x1b[2K\n")

        for index, row in enumerate(input_rows):
            output.append("\x1b[2K" + truncate_plain(row, width))
            if index < len(input_rows) - 1:
                output.append("\n")
        cursor_line = 2 + body_height + input_cursor_row
        output.append(
            f"\x1b[{max(1, min(height, cursor_line))};{max(1, min(width, input_cursor_col))}H"
        )
        output.append("\x1b[?25h")
        sys.stdout.write("".join(output))
        sys.stdout.flush()


