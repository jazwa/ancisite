"""The error taxonomy.

Every user-facing failure is one of five classes, rendered as
what / why / try. Mechanism detail lives in `debug` and surfaces only under
`--debug`. Tracebacks never reach the user (internal class reports instead).
"""

from __future__ import annotations

class Error(Exception):

    kind = "internal"

    def __init__(self, what: str, *, why: str | None = None,
                 try_: str | None = None, debug: str | None = None):
        super().__init__(what)
        self.what = what
        self.why = why
        self.try_ = try_
        self.debug = debug


class ConfError(Error):
    kind = "conf"


class AppError(Error):
    kind = "app"


class EnvError(Error):
    kind = "env"


class AuthError(Error):
    kind = "auth"


class InternalError(Error):

    def __init__(self, what: str = "something inside anci broke — please report this", **kw):
        super().__init__(what, **kw)
