"""anci — the Ancillary Website VM CLI (see external-docs/)."""

import sys

if sys.version_info < (3, 12):
    raise SystemExit(
        "anci needs Python 3.12 or newer (found "
        f"{sys.version_info.major}.{sys.version_info.minor}). "
        "Install a newer Python, or use a self-contained build."
    )

__version__ = "0.1.0"
