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
LITESTREAM_SOCKET = "/run/litestream/litestream.sock"
SECRETS_FILE = f"{ETC_DIR}/secrets.env"
LOGTRIM_SCRIPT = f"{ETC_DIR}/logtrim.sh"

BIN_DIR = "/usr/local/bin"
CADDY_BIN = f"{BIN_DIR}/caddy"
LITESTREAM_BIN = f"{BIN_DIR}/litestream"
SQLITE_BIN = f"{BIN_DIR}/sqlite3"


def exe_home(exe_name: str) -> str:
    return f"{EXE_DIR}/{exe_name}"


def db_dir(db_name: str) -> str:
    return f"{DB_DIR}/{db_name}"


def db_path(db_name: str) -> str:
    return f"{db_dir(db_name)}/data.sql"


def litestream_state_dir(db_name: str) -> str:
    return f"{db_dir(db_name)}/.data.sql-litestream"


def replica_record(db_name: str) -> str:
    return f"{db_dir(db_name)}/.anci-replica"


def ext_dir(db_name: str) -> str:
    return f"{EXT_DIR}/{db_name}"


def current_link(exe_name: str) -> str:
    return f"{EXE_DIR}/{exe_name}/current"


def default_target(exe_name: str) -> str:
    return f"{exe_home(exe_name)}/bin"

def static_link(exe_name: str) -> str:
    return f"{exe_home(exe_name)}/static"


def static_version(exe_name: str, static_id: str) -> str:
    return f"{exe_home(exe_name)}/static-{static_id}"


BLOB_ID_LEN = 16

SECRET_MODE = "0600"


_STATIC_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def is_static_id(s: str) -> bool:
    return _STATIC_ID_RE.match(s) is not None


def blob_id(sha: str) -> str:
    return sha[:BLOB_ID_LEN]


def blob_path(sha: str) -> str:
    return f"{BLOB_DIR}/{blob_id(sha)}"


SHARED_MODE = "2775"
CONFIG_MODE = "0755"
LOG_MODE = "0750"

SHARED_DIRS = (
    VAR_DIR, STORE_DIR, BLOB_DIR, EXE_DIR, DB_DIR, EXT_DIR, RELEASES_DIR,
)
CONFIG_DIRS = (ETC_DIR, UNITS_DIR, CADDY_DIR)


def unit_path(unit: str) -> str:
    return f"{UNITS_DIR}/{unit}"


def unit_link(unit: str) -> str:
    return f"{SYSTEMD_DIR}/{unit}"


def log_path(exe_name: str) -> str:
    return f"{LOG_DIR}/{exe_name}.log"


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_name(name: str) -> bool:
    return NAME_RE.match(name) is not None


def split_safe(names) -> tuple[list[str], list[str]]:
    safe, rejected = [], []
    for n in names:
        (safe if is_safe_name(n) else rejected).append(n)
    return safe, rejected
