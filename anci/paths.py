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
    """The project root = the directory the conf lives in (the project boundary)."""
    if getattr(conf, "path", None):
        return Path(conf.path).parent
    return Path.cwd()  # confs built programmatically (tests) default to the CWD


def secrets_encrypted(conf, env_name: str) -> Path:
    """The committed, sops-encrypted secrets for an env (tier 1)."""
    return project_root(conf) / "envs" / env_name / "secrets.env"


def secrets_plaintext(conf, env_name: str) -> Path:
    """The git-ignored plaintext secrets for an env (tier 2)."""
    return project_root(conf) / ".anci" / f"secrets.{env_name}.env"


def box_id(conf) -> str:
    """A short, per-project box discriminator for local libvirt boxes.

    ``<project dir name>-<6 hex of the absolute project path>`` — so two
    projects on one host get distinct domains/disks/IPs, and two projects that
    happen to share a directory name still differ. The name keeps it readable
    (`anci-ancisite-3f9a2c-dev`); the hash disambiguates.
    """
    root = project_root(conf).resolve()
    name = re.sub(r"[^a-zA-Z0-9]+", "-", root.name).strip("-").lower() or "project"
    return f"{name}-{hashlib.sha256(str(root).encode()).hexdigest()[:6]}"


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_dir(conf) -> Path:
    """``<project>/.artifact/`` (gitignored scratch space)."""
    return _mkdir(project_root(conf) / ARTIFACT_DIRNAME)


def bin_cache_dir() -> Path:
    """Vendored third-party binaries (sops/litestream/caddy) on the dev machine."""
    return _mkdir(Path.home() / ".local" / "share" / "anci" / "bin")
