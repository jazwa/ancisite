"""Terminal rendering primitives.

Plain ANSI: step lines (→ ✓ ✗), dim/bold, a time-based spinner, a boxed table.
Respects NO_COLOR and non-TTY stdout (plain, quiet output; no control codes,
only final lines). Kept intentionally small; if this outgrows ~200 lines,
reassess `rich`.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from itertools import cycle


def _detect_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") in ("dumb", ""):
        return False
    return sys.stdout.isatty()


_ANSI = _detect_ansi()


def set_ansi(enabled: bool) -> None:
    global _ANSI
    _ANSI = enabled


def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _ANSI else s


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _visible_len(s))


def green(s): return _c("32", s)
def red(s): return _c("31", s)
def yellow(s): return _c("33", s)
def bold(s): return _c("1", s)
def dim(s): return _c("2", s)


def human_size(n: int) -> str:
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


class Step:

    def __init__(self, label: str):
        self.label = label
        self.t0 = time.monotonic()
        self._running_shown = False
        if _ANSI:
            sys.stdout.write(f"  {dim('→')} {label}\n")
            sys.stdout.flush()
            self._running_shown = True

    def note(self, text: str) -> None:
        line = f"  {dim('→')} {self.label}  {dim(text)}"
        if _ANSI:
            self._rewrite(line)
            self._running_shown = True
        else:
            print(line, flush=True)

    def _rewrite(self, line: str) -> None:
        if _ANSI and self._running_shown:
            sys.stdout.write(f"\x1b[1A\x1b[2K")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        self._running_shown = False

    def _elapsed(self) -> str:
        seconds = time.monotonic() - self.t0
        return f"{seconds:.1f}s"

    def _finish(self, line: str, detail: str = "") -> None:
        suffix = f"  {dim(detail)}" if detail else ""
        self._rewrite(f"{line}{suffix}  {dim(self._elapsed())}")

    def done(self, detail: str = "") -> None:
        self._finish(f"  {green('✓')} {self.label}", detail)

    def skip(self, detail: str = "") -> None:
        self._finish(f"  {dim('–')} {dim(self.label)}", detail)

    def fail(self) -> None:
        self._finish(f"  {red('✗')} {self.label}")


class Pipeline:

    def __init__(self):
        self.t0 = time.monotonic()

    def step(self, label: str) -> Step:
        return Step(label)

    def done(self, note: str = "done") -> None:
        elapsed = time.monotonic() - self.t0
        print(f"\n  {dim(f'{note} in {elapsed:.1f}s')}")


class Spinner:

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        if _ANSI:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def _spin(self):
        for frame in cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r  {dim(frame)} {self.label}")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def stop(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def error(err) -> None:
    print(f"{red('error:')} {err.what}", file=sys.stderr)
    if err.why:
        for line in err.why.splitlines():
            print(f"  {dim(line)}", file=sys.stderr)
    if err.try_:
        print(f"  {bold('try:')} {err.try_}", file=sys.stderr)


def table(headers: list[str], rows: list[list[str]]) -> str:
    all_rows = [headers] + rows
    widths = [max(_visible_len(r[i]) for r in all_rows) for i in range(len(headers))]

    def line(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def fmt(row):
        return "│" + "│".join(f" {_pad(c, w)} " for c, w in zip(row, widths)) + "│"

    lines = [line("┌", "┬", "┐"), fmt(headers), line("├", "┼", "┤")]
    lines += [fmt(r) for r in rows]
    lines.append(line("└", "┴", "┘"))
    return "\n".join(lines)


def confirm(heading: str, details: list[str], question: str, *, force: bool = False,
            no_tty_error: Exception | None = None, stdin=None) -> bool:
    print(f"\n  {red(bold('WARNING'))}{red(' — ' + heading)}")
    for line in details:
        print("  " + red(line))
    if force:
        return True
    print()
    if stdin is not None:
        answer = stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            if no_tty_error is not None:
                raise no_tty_error
            return False
        answer = input("  " + red(f"{question} [y/N] ")).strip()
    return answer.lower() in ("y", "yes")
