"""Entry point for the zipapp build (see the Makefile).

`python -m zipapp -m "anci.cli:main"` generates a `__main__.py` that calls the
function but ignores its return value, which would drop anci's exit codes. The
build copies this file in as `__main__.py` instead, so `sys.exit(main())` runs.
"""

import sys

from anci.cli import main

if __name__ == "__main__":
    sys.exit(main())
