"""sshd: key-only logins, on every box. anci's own accounts only ever use keys,
but an image's account (a cloud image's `debian`) can carry a password, and
cloud-init's drop-in turns password logins on — root behind one password on a
public port. Every converge checks what sshd actually enforces, so a later
drop-in or `sshd_config` edit that turns passwords back on fails loudly."""

from __future__ import annotations

from .. import errors, layout, ssh
from ..apply import FileArtifact
from .context import AfterApply, DesiredState, ConvergeContext

# sshd keeps the first value it reads, and reads drop-ins in name order: 00-
# beats cloud-init's 50-cloud-init.conf
DROPIN = "/etc/ssh/sshd_config.d/00-anci.conf"

CONTENT = """\
# rendered by anci — do not edit: key-only ssh
PasswordAuthentication no
# KbdInteractiveAuthentication's pre-8.7 name, still accepted: no PAM password prompt
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
"""

# what `sshd -T` must not report (keys are lowercase there; 8.7+ prints
# kbdinteractiveauthentication, older ones challengeresponseauthentication)
_OFF = ("passwordauthentication", "kbdinteractiveauthentication",
        "challengeresponseauthentication")
_ROOT_OK = ("prohibit-password", "without-password", "no")   # without-password: the old name


def _script(changed: bool) -> str:
    validate = (
        # never leave a drop-in that keeps sshd from (re)starting
        f"if ! out=$(sshd -t 2>&1); then rm -f {DROPIN}; echo \"invalid $out\"; exit 0; fi\n"
        "for u in ssh.service sshd.service; do\n"   # Debian's ssh, most others' sshd (often aliases)
        "    if systemctl is-active --quiet \"$u\"; then systemctl reload \"$u\" || exit 1; break; fi\n"
        "done\n"                                     # socket-activated: each login reads the config
    ) if changed else ""
    # sshd lives in sbin, which sudo's PATH may lack; a box whose ssh server isn't
    # OpenSSH (dropbear) has nothing to configure here
    return ("PATH=$PATH:/usr/sbin:/sbin\n"
            "command -v sshd >/dev/null || { echo no-sshd; exit 0; }\n"
            + validate + "sshd -T\n")


def _activate(conn, result, pending=False):
    changed = DROPIN in result.changed_files or pending
    out = ssh.sudo(conn, _script(changed)).stdout
    if out.strip() == "no-sshd":
        return None
    if out.startswith("invalid "):
        raise errors.EnvError(
            "sshd rejected its config with anci's key-only drop-in, so anci removed it",
            why=out[len("invalid "):].strip()[-500:],
            try_="fix the sshd config on the box (`sudo sshd -t` shows the error), then re-run",
        )
    found = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    still_on = [f"{k} yes" for k in _OFF if found.get(k) == "yes"]
    if found.get("permitrootlogin", "no") not in _ROOT_OK:
        still_on.append(f"permitrootlogin {found['permitrootlogin']}")
    if still_on:
        raise errors.EnvError(
            "sshd on the box still accepts password logins",
            why=f"`sshd -T` reports {', '.join(still_on)}\n"
                f"a setting read before {DROPIN} wins, or sshd_config doesn't include "
                "/etc/ssh/sshd_config.d/",
            try_="remove that setting from sshd_config or an earlier-named drop-in in "
                 "/etc/ssh/sshd_config.d/ (or add the Include near the top of sshd_config), then re-run",
        )
    return None   # box hardening, not a restart worth reporting


def contribute(ctx: ConvergeContext) -> DesiredState:
    return DesiredState(
        name="sshd",
        files=[FileArtifact(path=DROPIN, content=CONTENT, mode="0644")],
        after_apply=[AfterApply(label="sshd", phase=2, run=_activate,
                                marker=f"{layout.VAR_DIR}/.reload-sshd", watches=[DROPIN])],
    )
