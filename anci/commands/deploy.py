"""`anci <env> deploy` and app-facing verbs.

Deploy is a thin entry point: resolve the bytes (a local `source` and `static`
dir), check the arch, then hand them to the single converge pass. The pipeline
itself lives in `anci/inspect/run.py`.
"""

from __future__ import annotations

import shlex
import struct
import subprocess

from .. import auth, errors, layout, ssh, staticfiles, units
from ..conf import Conf, conf_path
from ..inspect.exe import deploy_inputs
from ..inspect.run import converge


_ELF_MAGIC = b"\x7fELF"
_ELF_VMS = {
    3: "x86 (32-bit)",
    8: "mips",
    21: "ppc64",
    22: "s390x",
    40: "arm (32-bit)",
    62: "x86_64",
    183: "arm64",
    243: "riscv64",
}
_MACH_O_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                  b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def binary_arch(path) -> str | None:
    with path.open("rb") as f:
        head = f.read(20)
    if head[:2] == b"#!":
        return "script"
    if head[:4] == _ELF_MAGIC and len(head) >= 20:
        endian = "<" if head[5] == 1 else ">"
        vm = struct.unpack(endian + "H", head[18:20])[0]
        return _ELF_VMS.get(vm, f"elf-{vm}")
    if head[:4] in _MACH_O_MAGICS:
        return "mach-o"
    if head[:2] == b"MZ":
        return "pe"
    if head[:4] == b"\x00asm":
        return "wasm"
    return None


def check_app_arch(path) -> None:
    arch = binary_arch(path)
    if arch == "x86_64":
        return
    if arch == "script":
        raise errors.AppError(
            f"{path.name} is a script — anci runs binaries, not scripts",
            why="a #! script needs its interpreter installed on the box",
            try_="ship a compiled binary (a static Go/Rust build runs anywhere)",
        )
    if arch is None:
        raise errors.AppError(
            f"{path.name} doesn't look like an executable",
            why="it's not a Linux ELF binary",
            try_="deploy a compiled Linux binary",
        )
    if arch in ("mach-o", "pe"):
        os_name = "macOS" if arch == "mach-o" else "Windows"
        raise errors.AppError(
            f"{path.name} is a {os_name} binary — your box is Linux",
            why=f"anci runs x86_64 Linux guests; a {os_name} build can't execute there",
            try_="build for linux/amd64 (Go: GOOS=linux GOARCH=amd64)",
        )
    raise errors.AppError(
        f"{path.name} is built for {arch} — your box is x86_64",
        why="anci targets amd64 guests; a build for another architecture won't run",
        try_="rebuild for linux/amd64 (Go: GOARCH=amd64; "
             "Rust: --target x86_64-unknown-linux-musl)",
    )


def _require_exe(conf: Conf, exe_name: str) -> None:
    if exe_name not in conf.exes:
        raise errors.ConfError(
            f"exe '{exe_name}' isn't in anci.toml",
            why=f"defined exes: {', '.join(sorted(conf.exes)) or '(none)'}",
            try_=f"add [exe.{exe_name}] to anci.toml",
        )


def _local_source(conf: Conf, exe_name: str, env_name: str):
    resolved = conf.exes[exe_name].resolve_for(env_name)
    p = conf_path(conf, resolved.source)
    if not p.is_file():
        raise errors.AppError(
            f"{resolved.source} doesn't exist",
            why=f"that's the app file for exe '{exe_name}', per your anci.toml",
            try_=f"build it first, or fix 'source' under [exe.{exe_name}]",
        )
    return p


def _local_static(conf: Conf, exe_name: str, env_name: str) -> staticfiles.Scanned:
    resolved = conf.exes[exe_name].resolve_for(env_name)
    p = conf_path(conf, resolved.static)
    if not p.is_dir():
        raise errors.AppError(
            f"{resolved.static} isn't a directory",
            why=f"that's the static files dir for exe '{exe_name}', per your anci.toml",
            try_=f"build/create it, or fix 'static' under [exe.{exe_name}]",
        )
    return staticfiles.scan(p)


def deploy(conf: Conf, exe_name: str | None, env_name: str) -> str:
    names = [exe_name] if exe_name is not None else sorted(conf.exes)
    if not names:
        raise errors.ConfError("there are no exes to deploy",
                               try_="add an [exe.<name>] table to anci.toml")
    for name in names:
        _require_exe(conf, name)
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    auth.authorize(conf, env_name)

    return converge(conf, env_name, inputs=local_builds(conf, env_name, names))


def local_builds(conf: Conf, env_name: str, names: list[str], *,
                 skip_unbuilt: bool = False) -> dict:
    binaries, statics, source = {}, {}, {}
    for name in names:
        resolved = conf.exes[name].resolve_for(env_name)
        try:
            blob = _local_source(conf, name, env_name)
            scanned = _local_static(conf, name, env_name) if resolved.static else None
        except errors.AppError:
            if skip_unbuilt:
                continue
            raise
        check_app_arch(blob)
        binaries[name] = blob
        if scanned:
            statics[name] = scanned
        source[name] = resolved.source
    return deploy_inputs(binaries, statics, source)


def log_script(log_file: str, *, follow: bool, lines: int = 50) -> str:
    f = shlex.quote(log_file)
    script = f"{{ tail -n {lines} {f}.1 2>/dev/null; tail -n {lines} {f}; }} | tail -n {lines}"
    return script + (f"\nexec tail -n 0 -F {f}" if follow else "")


def app_logs(conf: Conf, exe_name: str, env_name: str, *, follow: bool) -> None:
    _require_exe(conf, exe_name)
    user = auth.authorize(conf, env_name)
    conn = ssh.connect(conf, env_name, user)
    unit = units.service_name(exe_name)
    log_file = layout.log_path(exe_name)
    if ssh.test(conn, f"sudo -n test -e {log_file}"):
        args = ["sudo", "-n", "sh", "-c", log_script(log_file, follow=follow)]
    else:
        args = ["sudo", "-n", "journalctl", "-u", unit, "--no-pager", "-q"]
        args += ["-f"] if follow else ["-n", "50"]
    try:
        proc = subprocess.run(conn.argv(args))
        if proc.returncode != 0:
            raise errors.EnvError(
                f"couldn't read logs for {exe_name} on {env_name}",
                try_=f"check the app is deployed: anci {env_name} deploy {exe_name}",
            )
    except KeyboardInterrupt:
        pass
