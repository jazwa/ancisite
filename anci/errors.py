"""The error taxonomy.

Every user-facing failure is one of five classes, rendered as
what / why / try. Mechanism detail lives in `debug` and surfaces only under
`--debug`. Tracebacks never reach the user (internal class reports instead).
"""

from __future__ import annotations

class Error(Exception):
    """A user-facing failure. `kind` (conf | app | env | auth | internal)
    selects the taxonomy voice; each subclass sets its own."""

    kind = "internal"

    def __init__(self, what: str, *, why: str | None = None,
                 try_: str | None = None, debug: str | None = None):
        super().__init__(what)
        self.what = what
        self.why = why
        self.try_ = try_
        self.debug = debug


class ConfError(Error):
    """Something in anci.toml is wrong or inconsistent."""
    kind = "conf"


class AppError(Error):
    """The user's app artifact is missing or unusable."""
    kind = "app"


class EnvError(Error):
    """The environment (local box or remote guest) is the problem."""
    kind = "env"


class AuthError(Error):
    """The user's identity doesn't grant access."""
    kind = "auth"


class InternalError(Error):
    """anci itself broke — report it."""

    def __init__(self, what: str = "something inside anci broke — please report this", **kw):
        super().__init__(what, **kw)
