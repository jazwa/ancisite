"""The guest filesystem layout anci owns, in one place.

`up` / deploy / migrate all create these dirs; keeping the paths and their
ownership here stops them drifting (they did: `/var/lib/anci` was `root:root`,
the litestream config was group-writable, blobs were owned by the login user).

**No `<env>` in guest paths.** One env = one box, so the box already *is* the
env — unit names, logs, db dirs, secrets and the litestream config carry no env
segment. (`envs/<env>/…` in the project repo keeps it: a project has many envs.)

Ownership model:
- **shared** dirs (`/var/lib/anci/*`) → `root:anci 2775` (setgid, so the `anci`
  group can write — deployers and the app both belong to it);
- **config** dirs (`/etc/anci/*`) → `root:root 0755` (rendered, root-owned);
- **secrets** (`/etc/anci/secrets.env`) → `root:root 0600` (systemd injects it as root);
- **logs** (`/var/log/anci`) → `anci:anci 0750` (each exe's stdout/stderr file).
"""

from __future__ import annotations

import re

ANCI_USER = "anci"
ANCI_GROUP = "anci"

VAR_DIR = "/var/lib/anci"
STORE_DIR = f"{VAR_DIR}/store"
BLOB_DIR = f"{STORE_DIR}/blobs"
EXE_DIR = f"{VAR_DIR}/exe"
DB_DIR = f"{VAR_DIR}/db"
EXT_DIR = f"{VAR_DIR}/ext"
RELEASES_DIR = f"{VAR_DIR}/releases"
LOG_DIR = "/var/log/anci"

ETC_DIR = "/etc/anci"
UNITS_DIR = f"{ETC_DIR}/units"
SYSTEMD_DIR = "/etc/systemd/system"
CADDY_DIR = f"{ETC_DIR}/caddy"
CADDY_FILE = f"{CADDY_DIR}/Caddyfile"
LITESTREAM_FILE = f"{ETC_DIR}/litestream.yaml"
# the daemon's control socket (`litestream list` reads sync state from it, no S3);
# lives in the unit's RuntimeDirectory so the unprivileged `anci` user can create it
LITESTREAM_SOCKET = "/run/litestream/litestream.sock"
SECRETS_FILE = f"{ETC_DIR}/secrets.env"
LOGTRIM_SCRIPT = f"{ETC_DIR}/logtrim.sh"

BIN_DIR = "/usr/local/bin"
CADDY_BIN = f"{BIN_DIR}/caddy"
LITESTREAM_BIN = f"{BIN_DIR}/litestream"
SQLITE_BIN = f"{BIN_DIR}/sqlite3"


def exe_home(exe_name: str) -> str:
    """An exe's managed dir (`/var/lib/anci/exe/<exe>`)."""
    return f"{EXE_DIR}/{exe_name}"


def db_dir(db_name: str) -> str:
    """Per-db directory: owned by the shared `anci` service user so sqlite can
    create journal/WAL files. Also isolates chown scope between dbs."""
    return f"{DB_DIR}/{db_name}"


def db_path(db_name: str) -> str:
    return f"{db_dir(db_name)}/data.sql"


def litestream_state_dir(db_name: str) -> str:
    """litestream's local record of a db's changes (it names it `.<db file>-litestream`)."""
    return f"{db_dir(db_name)}/.data.sql-litestream"


def replica_record(db_name: str) -> str:
    """The replica target litestream's local state belongs to (see converge/litestream)."""
    return f"{db_dir(db_name)}/.anci-replica"


def ext_dir(db_name: str) -> str:
    return f"{EXT_DIR}/{db_name}"


def current_link(exe_name: str) -> str:
    """The symlink to the exe's deployed blob — what `target` resolves through."""
    return f"{EXE_DIR}/{exe_name}/current"


def default_target(exe_name: str) -> str:
    """An exe's `target` — the stable path systemd executes.

    Inside the managed root, derived from the name; the app can't write here.
    """
    return f"{exe_home(exe_name)}/bin"

def static_link(exe_name: str) -> str:
    """The symlink to the exe's deployed static files — `ANCI_STATIC_DIR`."""
    return f"{exe_home(exe_name)}/static"


def static_version(exe_name: str, static_id: str) -> str:
    """One immutable version of an exe's static files (root-owned, read-only)."""
    return f"{exe_home(exe_name)}/static-{static_id}"


# Blobs are stored under a 16-hex-char prefix of their sha256 (64 bits). 64 bits
# is collision-safe at any realistic blob count, and deploy verifies the *full*
# sha256, so a (astronomically rare) prefix collision is a loud error, never a
# wrong binary. 16 hex keeps `ls`/`info` readable.
BLOB_ID_LEN = 16

SECRET_MODE = "0600"


_STATIC_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def is_static_id(s: str) -> bool:
    """True for a static version id (16 hex, like a blob id)."""
    return _STATIC_ID_RE.match(s) is not None


def blob_id(sha: str) -> str:
    """The on-disk name for a blob (first 16 hex of its sha256)."""
    return sha[:BLOB_ID_LEN]


def blob_path(sha: str) -> str:
    return f"{BLOB_DIR}/{blob_id(sha)}"


SHARED_MODE = "2775"
CONFIG_MODE = "0755"
LOG_MODE = "0750"

# shared, writable by the `anci` group
SHARED_DIRS = (
    VAR_DIR, STORE_DIR, BLOB_DIR, EXE_DIR, DB_DIR, EXT_DIR, RELEASES_DIR,
)
# rendered config, root-owned
CONFIG_DIRS = (ETC_DIR, UNITS_DIR, CADDY_DIR)


def unit_path(unit: str) -> str:
    """Where anci renders a unit (`/etc/anci/units/<unit>`)."""
    return f"{UNITS_DIR}/{unit}"


def unit_link(unit: str) -> str:
    """The systemd link to a rendered unit (`/etc/systemd/system/<unit>`)."""
    return f"{SYSTEMD_DIR}/{unit}"


def log_path(exe_name: str) -> str:
    """Per-exe log file (units `append:` stdout/stderr here)."""
    return f"{LOG_DIR}/{exe_name}.log"


# exe, db and env names become unit names, on-guest paths and CLI words:
# letters/digits first (no `.`/`..`), then letters, digits, `.`, `_`, `-`
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_name(name: str) -> bool:
    """True for a name anci itself could have created. The box's directory
    listings are untrusted (the `anci` group can write them), so a name that
    fails this is never planned against or handed to a shell."""
    return NAME_RE.match(name) is not None


def split_safe(names) -> tuple[list[str], list[str]]:
    """(safe names, rejected names), order kept."""
    safe, rejected = [], []
    for n in names:
        (safe if is_safe_name(n) else rejected).append(n)
    return safe, rejected
