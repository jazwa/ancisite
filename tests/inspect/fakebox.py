"""A box in a temp dir: apply's real scripts run there with bash, unchanged
except that every box path is moved under the temp root, and the commands
that need root or a real systemd are stubs that keep their state in files.

The stubs model what the scripts rely on:
- systemctl: `enable` makes the `*.wants` link the unit's `WantedBy=` names,
  `disable` removes it, `start`/`restart`/`stop` track which units run, a unit
  listed in `crash()` never comes up (`show` reports it waiting to restart),
  and every call is logged (`calls()`);
- useradd/groupadd/usermod/gpasswd/getent/id: accounts and the `anci` group;
- install/chown: owners are dropped (the tests don't run as root);
- sshd: a valid config, password logins off.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest import mock

from anci import auth, boxlock, forward, ssh
from anci.inspect import walk

_PREFIXES = ("/etc/", "/var/", "/usr/local/", "/home/", "/tmp/.anci-")

_STUBS = {
    "systemctl": r'''#!/bin/bash
echo "$*" >> "$FAKE/systemctl.log"
[ -n "$FAIL_SYSTEMCTL" ] && [[ "$*" == *"$FAIL_SYSTEMCTL"* ]] && exit 1
quiet=; now=
args=()
for a in "$@"; do case "$a" in --quiet) quiet=1;; --now) now=1;; *) args+=("$a");; esac; done
cmd=${args[0]}; units=("${args[@]:1}")
active="$FAKE/active"; touch "$active"
crash="$FAKE/crash"; touch "$crash"
if [ "$cmd" = show ]; then   # show <unit> -p <property> --value
  u=${units[0]}; prop=${units[2]}
  if grep -qxF "$u" "$crash"; then
    case "$prop" in ActiveState) echo activating;; NRestarts) echo 1;; esac
  else
    case "$prop" in ActiveState) grep -qxF "$u" "$active" && echo active || echo inactive;; NRestarts) echo 0;; esac
  fi
  exit 0
fi
for u in "${units[@]}"; do
  case "$cmd" in
    is-active) grep -qxF "$u" "$active" || exit 3 ;;
    start|restart) grep -qxF "$u" "$crash" || grep -qxF "$u" "$active" || echo "$u" >> "$active" ;;
    stop) grep -vxF "$u" "$active" > "$active.t"; mv "$active.t" "$active" ;;
    enable)
      f="$FAKE_ROOT/etc/systemd/system/$u"
      [ -e "$f" ] || { echo "Unit file $u does not exist." >&2; exit 1; }
      for t in $(sed -n 's/^WantedBy=//p' "$f"); do
        mkdir -p "$FAKE_ROOT/etc/systemd/system/$t.wants"
        ln -sfn "/etc/systemd/system/$u" "$FAKE_ROOT/etc/systemd/system/$t.wants/$u"
      done
      [ -n "$now" ] && { grep -qxF "$u" "$active" || echo "$u" >> "$active"; } ;;
    disable)
      rm -f "$FAKE_ROOT"/etc/systemd/system/*.wants/"$u"
      [ -n "$now" ] && { grep -vxF "$u" "$active" > "$active.t"; mv "$active.t" "$active"; } ;;
  esac
done
exit 0
''',
    "useradd": r'''#!/bin/bash
name=${@: -1}; echo "$name" >> "$FAKE/passwd"
for a in "$@"; do [ "$a" = --create-home ] && mkdir -p "$FAKE_ROOT/home/$name"; done; exit 0
''',
    "groupadd": '#!/bin/bash\necho "${@: -1}" >> "$FAKE/groups"\n',
    "id": '#!/bin/bash\n[ "$1" = -u ] && grep -qxF "$2" "$FAKE/passwd" 2>/dev/null && echo 1000\n',
    "usermod": '#!/bin/bash\n# usermod -aG <group> <user>\necho "$2 $3" >> "$FAKE/members"\n',
    "gpasswd": r'''#!/bin/bash
# gpasswd -d <user> <group>
touch "$FAKE/members"; grep -vxF "$3 $2" "$FAKE/members" > "$FAKE/m.t"; mv "$FAKE/m.t" "$FAKE/members"
''',
    "getent": r'''#!/bin/bash
# getent group <group>
grep -qxF "$2" "$FAKE/groups" 2>/dev/null || exit 2
echo "$2:x:1000:$(sed -n "s/^$2 //p" "$FAKE/members" 2>/dev/null | sort -u | paste -sd,)"
''',
    "chown": "#!/bin/bash\nexit 0\n",
    "install": r'''#!/bin/bash
args=(); skip=
for a in "$@"; do
  if [ -n "$skip" ]; then skip=; continue; fi
  case "$a" in -o|-g) skip=1;; *) args+=("$a");; esac
done
exec /usr/bin/install "${args[@]}"
''',
    "sshd": r'''#!/bin/bash
case "$1" in -t) exit 0;; -T) printf 'passwordauthentication no\npermitrootlogin prohibit-password\n';; esac
''',
}


class FakeBox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.state = self.root / "_state"
        self.bin = self.root / "_bin"
        for d in ("etc/sudoers.d", "etc/systemd/system", "home", "usr/local/bin", "var/log",
                  "var/lib", "tmp"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.state.mkdir(exist_ok=True)
        self.bin.mkdir(exist_ok=True)
        (self.state / "groups").write_text("")
        (self.state / "passwd").write_text("root\n")
        for name, text in _STUBS.items():
            p = self.bin / name
            p.write_text(text)
            p.chmod(0o755)
        self.fail_systemctl = ""

    # -- paths

    def path(self, box_path: str) -> Path:
        """Where a box path lives in this fake."""
        return Path(self._in(box_path))

    def _in(self, text: str) -> str:
        for p in _PREFIXES:
            text = text.replace(p, f"{self.root}{p}")
        return text

    def _out(self, text: str) -> str:
        return text.replace(str(self.root), "")

    # -- running

    def _run(self, script: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE": str(self.state), "FAKE_ROOT": str(self.root),
               "FAIL_SYSTEMCTL": self.fail_systemctl}
        return subprocess.run(["bash", "-c", self._in(script)], capture_output=True, text=True,
                              env=env, timeout=120)

    def facts(self, conn, probes, *, preamble="", timeout=120, root=False):
        p = self._run(ssh.facts_script(probes, preamble))
        return ssh.parse_facts(self._out(p.stdout))

    def sudo(self, conn, text, *, timeout=120):
        p = self._run(text)
        if p.returncode != 0:
            raise ssh.SSHError(f"remote command failed (exit {p.returncode})",
                               stderr=self._out(p.stderr))
        return ssh.Result(self._out(p.stdout), self._out(p.stderr))

    def put(self, conn, local, remote, *, private=False, timeout=600):
        if "apply.tar" not in remote:
            shutil.copyfile(local, self._in(remote))
            return
        # apply's script travels inside its tar: move its box paths too
        members = {}
        with tarfile.open(local) as tar:
            for m in tar.getmembers():
                members[m.name] = tar.extractfile(m).read()
        run = self._in(members["run.sh"].decode()).encode()
        old = hashlib.sha256(members["run.sh"]).hexdigest()
        members["run.sh"] = run
        members["manifest"] = members["manifest"].replace(
            old.encode(), hashlib.sha256(run).hexdigest().encode())
        with tarfile.open(self._in(remote), "w") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    # -- what the stubs saw

    def calls(self) -> list[str]:
        """Every systemctl call, in order."""
        log = self.state / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    def clear_calls(self) -> None:
        (self.state / "systemctl.log").unlink(missing_ok=True)

    def active(self) -> set[str]:
        f = self.state / "active"
        return set(f.read_text().split()) if f.exists() else set()

    def crash(self, *units: str) -> None:
        """These units fail at every start, from now on."""
        (self.state / "crash").write_text("".join(f"{u}\n" for u in units))

    def members(self) -> set[str]:
        f = self.state / "members"
        return {l.split()[1] for l in f.read_text().splitlines()} if f.exists() else set()

    @contextlib.contextmanager
    def connected(self):
        """Every ssh call goes to this box, as the user `jack`."""
        with mock.patch.object(ssh, "facts", side_effect=self.facts), \
             mock.patch.object(ssh, "sudo", side_effect=self.sudo), \
             mock.patch.object(ssh, "put", side_effect=self.put), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "wait_ready"), \
             mock.patch.object(walk, "SETTLE_S", 0), \
             mock.patch.object(boxlock, "hold", lambda *a, **k: contextlib.nullcontext()), \
             mock.patch.object(auth, "authorize", return_value="jack"), \
             mock.patch.object(forward, "reconcile", return_value={}), \
             mock.patch.object(forward, "installed_ports", return_value={}):
            yield self
