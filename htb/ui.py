"""Terminal output helpers: colour, tables, spinners, prompts."""

from __future__ import annotations

import getpass
import itertools
import json as _json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

JSON_MODE = False
QUIET = False


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("HTB_NO_COLOR"):
        return False
    return stream.isatty()


_COLORS = {
    "bold": "1", "dim": "2", "italic": "3", "underline": "4",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "white": "37", "grey": "90",
    "bgreen": "92", "bred": "91", "byellow": "93", "bcyan": "96",
}


def c(text, *styles) -> str:
    text = str(text)
    if not styles or not use_color():
        return text
    codes = ";".join(_COLORS[s] for s in styles if s in _COLORS)
    return f"\x1b[{codes}m{text}\x1b[0m" if codes else text


def width(text: str) -> int:
    """Display width, counting emoji and CJK glyphs as two columns."""
    plain = _ANSI.sub("", str(text))
    total = 0
    for char in plain:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def term_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


# --- messages ---------------------------------------------------------------

def out(*args, **kwargs) -> None:
    """Human-facing line. Flushed so it interleaves correctly with stderr."""
    if not JSON_MODE:
        kwargs.setdefault("flush", True)
        print(*args, **kwargs)


def info(msg: str) -> None:
    if not QUIET:
        out(f"{c('::', 'cyan', 'bold')} {msg}")


def success(msg: str) -> None:
    out(f"{c('✓', 'green', 'bold')} {msg}")


def warn(msg: str) -> None:
    print(f"{c('!', 'yellow', 'bold')} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{c('✗', 'red', 'bold')} {msg}", file=sys.stderr)


def die(msg: str, code: int = 1):
    error(msg)
    raise SystemExit(code)


def json_dump(obj) -> None:
    print(_json.dumps(obj, indent=2, default=str))


def emit(data, render=None) -> None:
    """Print JSON when --json is set, otherwise call `render`."""
    if JSON_MODE:
        json_dump(data)
    elif render:
        render()


# --- tables -----------------------------------------------------------------

def table(headers, rows, aligns=None, max_width: int = 0) -> None:
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    if not rows:
        return
    ncol = len(headers)
    aligns = aligns or ["l"] * ncol
    widths = [width(h) for h in headers]
    for r in rows:
        for i in range(ncol):
            widths[i] = max(widths[i], width(r[i]))

    limit = max_width or term_width()
    overflow = sum(widths) + 2 * (ncol - 1) - limit
    if overflow > 0:  # shrink the widest column first
        order = sorted(range(ncol), key=lambda i: widths[i], reverse=True)
        for i in order:
            take = min(overflow, max(0, widths[i] - 12))
            widths[i] -= take
            overflow -= take
            if overflow <= 0:
                break

    def cell(text, w, align):
        if width(text) > w:
            plain, kept, used = _ANSI.sub("", text), [], 0
            for char in plain:
                step = width(char)
                if used + step > max(1, w - 1):
                    break
                kept.append(char)
                used += step
            text = "".join(kept) + "…"
        pad = w - width(text)
        return (" " * pad + text) if align == "r" else text + " " * pad

    line = "  ".join(cell(c(h, "bold", "underline"), w, a)
                     for h, w, a in zip(headers, widths, aligns))
    print(line.rstrip())
    for r in rows:
        print("  ".join(cell(v, w, a) for v, w, a in zip(r, widths, aligns)).rstrip())


def kv(pairs, indent: int = 0) -> None:
    pairs = [(k, v) for k, v in pairs if v is not None and v != ""]
    if not pairs:
        return
    klen = max(len(k) for k, _ in pairs)
    pad = " " * indent
    for k, v in pairs:
        print(f"{pad}{c(k.ljust(klen), 'grey')}  {v}")


def rule(title: str = "") -> None:
    w = min(term_width(), 80)
    if title:
        print(c(f"── {title} " + "─" * max(0, w - len(title) - 4), "grey"))
    else:
        print(c("─" * w, "grey"))


# --- spinner ----------------------------------------------------------------

class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: str = ""):
        self.text = text
        self._stop = threading.Event()
        self._thread = None
        self.enabled = sys.stderr.isatty() and not JSON_MODE and not QUIET

    def _run(self):
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r{c(frame, 'cyan')} {self.text}\x1b[K")
            sys.stderr.flush()
            time.sleep(0.08)

    def start(self):
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def update(self, text: str):
        self.text = text

    def stop(self, final: str = ""):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self.enabled:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()
        if final:
            out(final)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


# --- prompts ----------------------------------------------------------------

def confirm(question: str, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{c('?', 'yellow', 'bold')} {question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def secret(prompt: str) -> str:
    return getpass.getpass(f"{c('?', 'yellow', 'bold')} {prompt}: ")


def choose(question: str, options, render=str, assume_yes: bool = False):
    """Pick one item from `options`; returns the item or None."""
    options = list(options)
    if not options:
        return None
    if len(options) == 1 or assume_yes or not sys.stdin.isatty():
        return options[0]
    print(question)
    for i, opt in enumerate(options, 1):
        print(f"  {c(str(i).rjust(2), 'cyan')}  {render(opt)}")
    try:
        raw = input(f"{c('?', 'yellow', 'bold')} Select [1-{len(options)}] (1): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return options[0]
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return None


# --- formatting -------------------------------------------------------------

DIFF_COLORS = {
    "easy": "green", "very easy": "bgreen", "medium": "yellow",
    "hard": "red", "insane": "magenta",
}


def difficulty(text: str) -> str:
    return c(text, DIFF_COLORS.get(str(text).lower(), "white"))


def os_icon(name: str) -> str:
    icons = {"linux": "🐧", "windows": "🪟", "freebsd": "😈", "openbsd": "🐡", "android": "🤖"}
    return icons.get(str(name).lower(), "  ")


def owned(flag) -> str:
    return c("✓", "green") if flag else c("·", "grey")


def rel_time(iso: str) -> str:
    """'2026-09-11T10:00:00.000000Z' -> 'in 23h 12m' / '3d ago'."""
    from datetime import datetime, timezone
    if not iso:
        return ""
    text = str(iso).replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return str(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    future = delta > 0
    delta = abs(delta)
    days, rem = divmod(int(delta), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        span = f"{days}d {hours}h"
    elif hours:
        span = f"{hours}h {minutes}m"
    else:
        span = f"{minutes}m"
    return f"in {span}" if future else f"{span} ago"
