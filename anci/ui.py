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
    """Override detection (tests, --no-color)."""
    global _ANSI
    _ANSI = enabled


def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _ANSI else s


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Display width of `s`, ignoring ANSI color escapes."""
    return len(_ANSI_RE.sub("", s))


def _pad(s: str, width: int) -> str:
    """Left-justify `s` to `width` visible columns (ANSI-aware)."""
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


# ---------------------------------------------------------------------------
# step lines


class Step:
    """One pipeline step: prints `→ label`, rewrites to ✓/✗ on completion.

    Every completion stamps the step's duration on the right — slow deploys
    explain themselves per step instead of only a whole-run total.
    """

    def __init__(self, label: str):
        self.label = label
        self.t0 = time.monotonic()
        self._running_shown = False
        if _ANSI:
            sys.stdout.write(f"  {dim('→')} {label}\n")  # placeholder line
            sys.stdout.flush()
            self._running_shown = True

    def note(self, text: str) -> None:
        """Say what the running step is doing (e.g. how much it's uploading)."""
        line = f"  {dim('→')} {self.label}  {dim(text)}"
        if _ANSI:
            self._rewrite(line)
            self._running_shown = True
        else:
            print(line, flush=True)

    def _rewrite(self, line: str) -> None:
        if _ANSI and self._running_shown:
            sys.stdout.write(f"\x1b[1A\x1b[2K")  # up one line, clear it
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
    """A command pipeline that prints step-by-step progress."""

    def __init__(self):
        self.t0 = time.monotonic()

    def step(self, label: str) -> Step:
        return Step(label)

    def done(self, note: str = "done") -> None:
        elapsed = time.monotonic() - self.t0
        print(f"\n  {dim(f'{note} in {elapsed:.1f}s')}")


# ---------------------------------------------------------------------------
# spinner (for guest waits)


class Spinner:
    """Time-based spinner on the current line. No-op without ANSI."""

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


# ---------------------------------------------------------------------------
# error rendering (taxonomy voice: what / why / try)


def error(err) -> None:
    """Render a taxonomy Error (anci/errors.py) to stderr."""
    print(f"{red('error:')} {err.what}", file=sys.stderr)
    if err.why:
        for line in err.why.splitlines():   # a multi-line why: one indented line each
            print(f"  {dim(line)}", file=sys.stderr)
    if err.try_:
        print(f"  {bold('try:')} {err.try_}", file=sys.stderr)


# ---------------------------------------------------------------------------
# boxed table (status home screen)


def table(headers: list[str], rows: list[list[str]]) -> str:
    """A minimal fixed-width boxed table. Plain separators without ANSI."""
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
    """The one look for every "are you sure": a red WARNING block, then a red
    `[y/N]` question. Always printed, so even `--force` leaves a record of what it
    was about to do; `force` just skips the question.

    Without a terminal (and without an injected `stdin`) there is nobody to ask:
    raise `no_tty_error` — the caller words it, since only some verbs take --force.
    """
    print(f"\n  {red(bold('WARNING'))}{red(' — ' + heading)}")
    for line in details:
        print("  " + red(line))
    if force:
        return True
    print()   # air between the warning and the question
    if stdin is not None:
        answer = stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            if no_tty_error is not None:
                raise no_tty_error
            return False
        answer = input("  " + red(f"{question} [y/N] ")).strip()
    return answer.lower() in ("y", "yes")
