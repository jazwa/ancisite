"""The hypervisor transport: run root commands where a managed box's VM lives.

This dev machine (`sudo`) or a remote libvirt host (ssh + passwordless sudo, one
shared connection). virsh, provisioning and the edge all go through it.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from . import auth, errors, ssh
from .box import is_remote_host
from .conf import Conf

SSH_PORT = 22



@dataclass
class Hypervisor:
    """This dev machine (`login` None) or a remote host (`login` = "user@host")."""

    login: str | None = None
    # this dev machine only: False runs commands as you, not through sudo — for
    # reads a `libvirt` group member may make without a password
    sudo: bool = True

    @property
    def where(self) -> str:
        return "your dev machine" if self.login is None else self.login.split("@", 1)[1]

    def _argv(self, script: str) -> list[str]:
        if self.login is None:
            return ["sudo", "-n", "bash", "-c", script] if self.sudo else ["bash", "-c", script]
        user, host = self.login.split("@", 1)
        # one shared ssh connection for the many commands an `up` runs
        return ["ssh", *ssh._mux_opts(user, host, SSH_PORT), "-o", "BatchMode=yes",
                self.login, f"sudo -n bash -c {shlex.quote(script)}"]

    def run(self, script: str, *, input: str | None = None) -> subprocess.CompletedProcess:
        """Run `script` as root on the hypervisor; never raises on failure."""
        try:
            return subprocess.run(self._argv(script), input=input,
                                  capture_output=True, text=True)
        except FileNotFoundError as e:
            raise errors.EnvError(
                f"`{'sudo' if self.login is None else 'ssh'}` isn't on your PATH",
                try_="install sudo / an OpenSSH client",
            ) from e

    def root(self, script: str, what: str, *, input: str | None = None) -> str:
        """`run`, raising a taxonomy error on failure. Returns stdout."""
        p = self.run(script, input=input)
        if p.returncode != 0:
            raise self.failure(p, what)
        return p.stdout

    def failure(self, p: subprocess.CompletedProcess, what: str) -> errors.EnvError:
        """The taxonomy error for a failed `run` (sudo password, ssh, or the command)."""
        err = ((p.stderr or "") + (p.stdout or "")).strip()[-2000:]
        if "password is required" in err or "a terminal is required" in err:
            if self.login is None:
                return errors.EnvError(f"{what}: sudo wants your password again",
                                       try_="re-run the command", debug=err)
            user, host = self.login.split("@", 1)
            return errors.EnvError(
                f"{what}: sudo on {host} asked for a password",
                why="anci provisions a remote host's boxes with passwordless sudo",
                try_=f"give {user} passwordless sudo on {host} (a NOPASSWD sudoers rule)",
                debug=err,
            )
        if self.login is not None and p.returncode == 255:
            return errors.EnvError(
                f"{what}: can't ssh to {self.login}",
                why="anci reaches a remote hypervisor as your conf user, with your ssh config",
                try_=f"check `ssh {self.login}` works without a password",
                debug=err,
            )
        return errors.EnvError(f"{what} failed on {self.where}",
                               try_="re-run with --debug for the raw output", debug=err)

    def virsh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        p = self.run("virsh --connect qemu:///system --quiet " + shlex.join(args))
        if check and p.returncode != 0:
            raise errors.EnvError(
                f"virsh {' '.join(args)} failed",
                try_="is the libvirt daemon running? " + self.hint("systemctl start libvirtd"),
                debug=(p.stderr or "").strip(),
            )
        return p

    def probe(self, probes: dict[str, str]) -> dict[str, list[str]]:
        """`ssh.facts`, as root on the hypervisor."""
        return ssh.parse_facts(self.run(ssh.facts_script(probes)).stdout)

    def hint(self, cmd: str) -> str:
        """How the user runs `cmd` as root on this hypervisor themselves."""
        return f"sudo {cmd}" if self.login is None else f"ssh {self.login} sudo {cmd}"


def hypervisor(conf: Conf, env_name: str) -> Hypervisor:
    """The hypervisor of a managed env. A remote host is reached as your conf
    user; locally, sudo asks for your password once, up front."""
    env = conf.envs[env_name]
    if is_remote_host(env):
        return Hypervisor(f"{auth.authorize(conf, env_name)}@{env.ip}")
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        raise errors.EnvError("anci needs sudo to run the VM on your dev machine",
                              try_="re-run and enter your password")
    return Hypervisor()


def libvirt_hypervisor(conf: Conf, env_name: str) -> Hypervisor:
    """`hypervisor`, for work that only calls virsh (reading or resizing a
    running VM). On this dev machine a `libvirt` group member may do that as
    themselves, so sudo is asked for only when libvirt refuses you."""
    if not is_remote_host(conf.envs[env_name]):
        mine = Hypervisor(sudo=False)
        if mine.virsh("uri", check=False).returncode == 0:
            return mine
    return hypervisor(conf, env_name)
