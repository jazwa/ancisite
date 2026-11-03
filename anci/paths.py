"""Dev-machine-side project paths, anchored on the conf's directory.

Regenerable state lives under ``<project>/.artifact/`` (gitignored): rendered
scratch dbs and pulled db copies. Anchoring on the conf — not
the CWD — means running ``anci`` from a subdirectory still puts state in the
project (the same rule `conf_path`, pins, and secrets follow).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


ARTIFACT_DIRNAME = ".artifact"


def project_root(conf) -> Path:
    if getattr(conf, "path", None):
        return Path(conf.path).parent
    return Path.cwd()


def secrets_encrypted(conf, env_name: str) -> Path:
    return project_root(conf) / "envs" / env_name / "secrets.env"


def secrets_plaintext(conf, env_name: str) -> Path:
    return project_root(conf) / ".anci" / f"secrets.{env_name}.env"


def box_id(conf) -> str:
    root = project_root(conf).resolve()
    name = re.sub(r"[^a-zA-Z0-9]+", "-", root.name).strip("-").lower() or "project"
    return f"{name}-{hashlib.sha256(str(root).encode()).hexdigest()[:6]}"


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_dir(conf) -> Path:
    return _mkdir(project_root(conf) / ARTIFACT_DIRNAME)


def bin_cache_dir() -> Path:
    return _mkdir(Path.home() / ".local" / "share" / "anci" / "bin")
