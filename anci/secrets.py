"""Secrets: the env's `KEY=value` file — resolved on the dev machine, couriered to the box.

Tier 1 (recommended): committed sops-encrypted `envs/<env>/secrets.env`,
decrypted with your ssh key. Tier 2: plaintext `.anci/secrets.<env>.env`
(git-ignored). Either way the box gets `layout.SECRETS_FILE` (mode 600), and
apps read the values as env vars.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import bin as bin_mod, errors, layout, paths, ui, versions as versions_mod
from .conf import Conf


def resolve_secrets(conf: Conf, env_name: str) -> tuple[str | None, str]:
    """The plaintext secrets content for an env, from either tier.

    Tier 1 (recommended): committed sops-encrypted `envs/<env>/secrets.env`,
    decrypted with your ssh key via `sops -d`.
    Tier 2 (fallback): plaintext `.anci/secrets.<env>.env` — you keep it,
    anci ships it.

    Returns (content | None, where-it-came-from). None = no secrets for this
    env (the unit renders without an EnvironmentFile).
    """
    encrypted = paths.secrets_encrypted(conf, env_name)
    plaintext = paths.secrets_plaintext(conf, env_name)

    if encrypted.is_file():
        sops = sops_binary(conf, env_name)  # vendored on first use — never a manual install
        p = subprocess.run(
            [str(sops), "-d", str(encrypted)], capture_output=True, text=True, timeout=60
        )
        if p.returncode != 0:
            raise errors.AuthError(
                f"couldn't decrypt {encrypted}",
                why="your ssh key isn't (or is no longer) a recipient of this file",
                try_="check your key is under a user with access to this env, "
                     "then have someone run: anci " + env_name + " secrets",
                debug=p.stderr,
            )
        return p.stdout, f"decrypted {encrypted.name}"

    if plaintext.is_file():
        return plaintext.read_text(), "couriered .anci plaintext"

    return None, ""


def sops_binary(conf, env_name: str) -> Path:
    """The vendored sops binary — fetched hash-pinned on first use.

    sops stays on the dev machine: decryption is client-side, in memory; the guest
    never needs it. The version map carries the sha256 pin; the binary belongs
    to the env's pinned anci-version.
    """
    entry = versions_mod.get(versions_mod.load(), conf.envs[env_name].anci_version)
    plat = bin_mod.local_platform()
    # versioned, per-platform cache name: a pin bump must not keep serving the old binary
    name = f"sops-{entry.sops}-{plat}"
    cached = paths.bin_cache_dir() / name
    if cached.is_file():
        return cached
    if plat not in entry.sops_builds:
        raise errors.EnvError(
            f"anci has no sops build for this machine ({plat})",
            why="secrets are decrypted with sops on your dev machine",
            try_=f"use a supported machine ({', '.join(sorted(entry.sops_builds)) or 'none pinned'})",
        )
    url, sha256 = entry.sops_builds[plat]
    pipe = ui.Pipeline()
    s = pipe.step("fetching sops")
    vendored = bin_mod.vendor(url, None, sha256, name, paths.bin_cache_dir())
    s.done()
    return vendored


# ---------------------------------------------------------------------------
# who can read the encrypted file


_SOPS_RECIPIENT_RE = re.compile(r"^sops_age__list_\d+__map_recipient=(.+)$", re.M)


def _key_id(key: str) -> tuple[str, str] | None:
    """(type, base64) of an ssh public key line; the trailing comment is only a label."""
    parts = key.split()
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def conf_recipients(conf: Conf, env_name: str) -> list[str]:
    """The ssh keys that should be able to read this env's secrets: every key of
    every user with access to it (sorted, validated as sops-supported)."""
    recipients = sorted(
        {k for name in conf.envs[env_name].access_users
         for k in conf.users[name].ssh_pub_keys}
    )
    bad = [k for k in recipients if not (k.startswith("ssh-ed25519 ") or k.startswith("ssh-rsa "))]
    if bad:
        raise errors.ConfError(
            f"unsupported ssh key type for secrets encryption: {bad[0].split()[0]}",
            why="sops supports ssh-ed25519 and ssh-rsa keys only (no ECDSA)",
            try_="use an ed25519 key for the user holding secrets access",
        )
    return recipients


def recipient_changes(sops_file_text: str, wanted: list[str]) -> tuple[list[str], list[str]]:
    """(keys to add, keys to remove) to make an encrypted dotenv's recipients equal `wanted`.

    Reads the recipients sops stored in the file's own metadata. Keys are compared
    by (type, base64), so a different comment never counts as a change.
    """
    have = {_key_id(k): k.strip() for k in _SOPS_RECIPIENT_RE.findall(sops_file_text)}
    want = {_key_id(k): " ".join(k.split()[:2]) for k in wanted}
    add = [want[i] for i in sorted(want, key=str) if i not in have]
    rm = [have[i] for i in sorted(have, key=str) if i not in want]
    return add, rm


def sops_env() -> dict[str, str]:
    """The environment sops runs in: its own discovery, with this machine's ssh key
    pinned when one exists (so an rsa-only user isn't forced onto ed25519)."""
    env = dict(os.environ)
    ssh_key = next(
        (p for p in (Path.home() / ".ssh" / "id_ed25519", Path.home() / ".ssh" / "id_rsa")
         if p.is_file()),
        None,
    )
    if ssh_key is not None:
        env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"] = str(ssh_key)
    return env


def sync_recipients(conf: Conf, env_name: str, *, sops: Path | None = None,
                    env: dict[str, str] | None = None) -> tuple[int, int] | None:
    """Make the committed secrets file readable by exactly the conf's keys.

    `sops edit --age …` only chooses recipients when it creates a file, so a key
    added to (or dropped from) the conf has to be applied with `sops rotate` — by a
    machine that can already decrypt. Returns (added, removed), or None when there is
    no encrypted file or nothing to change. Prints one line when it rewrites the file.
    """
    encrypted = paths.secrets_encrypted(conf, env_name)
    if not encrypted.is_file():
        return None
    add, rm = recipient_changes(encrypted.read_text(), conf_recipients(conf, env_name))
    if not (add or rm):
        return None
    sops = sops or sops_binary(conf, env_name)
    cmd = [str(sops), "rotate", "-i"]
    if add:
        cmd += ["--add-age", ",".join(add)]
    if rm:
        cmd += ["--rm-age", ",".join(rm)]
    p = subprocess.run([*cmd, str(encrypted)], env=env or sops_env())
    if p.returncode != 0:
        raise errors.AuthError(
            "couldn't update who can read the secrets",
            why="this machine's ssh key can't decrypt the file yet, so it can't re-encrypt it",
            try_="run this on a machine whose key is already a recipient, then commit the file",
            debug=f"sops rotate exit {p.returncode}",
        )
    print(f"  {ui.green('✓')} {encrypted.name} re-encrypted for the conf's keys "
          f"(+{len(add)} added, -{len(rm)} removed) — commit it")
    return len(add), len(rm)
