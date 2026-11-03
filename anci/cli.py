"""anci CLI entry point (Click).

Grammar: **the env scopes every command** — `anci <env> <verb> [args]`. Besides
the envs, the top-level words are `init` (it runs before a conf exists) and
`completion`. Every env in anci.toml is a subcommand group of the root, so the
parser, `--help` and shell completion all come from the commands below. The conf
is found by walking up from the CWD (git-style).

All failures render through the error taxonomy (errors.py): no tracebacks unless
`--debug`. KeyboardInterrupt is a normal exit.
"""

from __future__ import annotations

import sys
import traceback

import click
from click.shell_completion import get_completion_class

from . import __version__, errors, ui, validate
from .conf import Conf, find_conf, load


def _from_ssh(e) -> errors.Error:
    stderr = e.stderr or ""
    if e.host_key:
        return errors.AuthError(
            "the box answering this address is not the one your conf points at",
            why="its host key differs from the pin committed in envs/<env>/hostkey",
            try_="if you rebuilt it on purpose: verify the new key out-of-band, "
                 "then delete envs/<env>/hostkey and reconnect — anci re-records it",
            debug=stderr,
        )
    if e.auth:
        return errors.AuthError(
            "the box refused your ssh login",
            why="your key isn't authorized for that user on the box",
            try_="check the user exists and your key is in its authorized_keys — "
                 "re-run `anci <env> up` to reconcile (a box that's still "
                 "booting may just need a moment); for an adopted box whose "
                 "setup didn't finish: anci <env> up --user <login>",
            debug=stderr,
        )
    if "could not get lock" in stderr.lower() or "dpkg frontend is locked" in stderr.lower():
        return errors.EnvError(
            "the box is still finishing its first boot",
            why="another apt run is holding the lock (cloud-init is installing packages)",
            try_="wait a few seconds and re-run",
            debug=stderr,
        )
    if e.unreachable:
        return errors.EnvError(
            "can't reach your box",
            why="it may be off, or a firewall is blocking ssh",
            try_="anci <env> up",
            debug=stderr,
        )
    return errors.InternalError(
        "a remote command failed unexpectedly",
        try_="re-run with --debug for the raw output",
        debug=stderr or str(e),
    )


def _render_failure(e: BaseException, debug: bool) -> int:
    from . import ssh

    if isinstance(e, ssh.SSHError):
        e = _from_ssh(e)
    if isinstance(e, errors.Error):
        ui.error(e)
        if debug and e.debug:
            print(ui.dim(f"debug: {e.debug}"), file=sys.stderr)
        if debug and e.kind == "internal":
            traceback.print_exc()
        return 1
    ui.error(
        errors.InternalError(
            "something inside anci broke — please report this",
            why=f"{type(e).__name__}: {e}",
            try_="re-run with --debug for details, then file an issue",
        )
    )
    if debug:
        traceback.print_exc()
    return 1


def _take_flag(argv: list[str], name: str) -> tuple[bool, list[str]]:
    return name in argv, [a for a in argv if a != name]


SHELLS = ["bash", "zsh", "fish"]


def _quiet_conf() -> Conf | None:
    try:
        return load(find_conf())
    except Exception:
        return None


def _names(kind: str):
    def complete(ctx, param, incomplete):
        conf = _quiet_conf()
        names = sorted(getattr(conf, kind)) if conf else []
        return [n for n in names if n.startswith(incomplete)]
    return complete


def _info_names(ctx, param, incomplete):
    conf = _quiet_conf()
    kind = ctx.params.get("kind")
    names = sorted(conf.exes if kind == "exe" else conf.dbs if kind == "db" else []) if conf else []
    return [n for n in names if n.startswith(incomplete)]


@click.command(help="Provision or boot this env's box (idempotent).")
@click.option("--recreate", is_flag=True, help="Destroy the box and rebuild it from scratch.")
@click.option("--force", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--user", help="Adopted box, first up only: the login for the setup (default: root).")
@click.pass_obj
def up(obj, recreate, force, user):
    from .inspect.run import converge
    from .providers.adopt import up_adopt
    from .providers.libvirt import env_up

    conf, env_name = obj["conf"], obj["env"]
    env = conf.envs[env_name]
    pipe = ui.Pipeline()
    if env.adopted:
        if recreate:
            raise errors.ConfError(
                f"anci didn't provision {env_name}'s box, so it can't recreate it",
                why="it's mode = 'adopt' — someone else's machine",
                try_="rebuild it with your provider, delete "
                     f"envs/{env_name}/hostkey, then: anci {env_name} up",
            )
        up_adopt(conf, env_name, login_user=user, pipe=pipe)
    else:
        if user:
            raise errors.ConfError(
                "--user only applies to an adopted box's first up",
                why=f"anci provisions {env_name}'s VM and creates your account on it",
            )
        if env_up(conf, env_name, recreate=recreate, force=force, pipe=pipe) is None:
            return
    converge(conf, env_name, grow_disk=True, pipe=pipe)
    pipe.done()


@click.command(help="Stop this env's box (keeps its disk).")
@click.pass_obj
def down(obj):
    from .providers.libvirt import env_down

    env_down(obj["conf"], obj["env"])


@click.command(help="Show this env's exes, dbs and box.")
@click.pass_obj
def status(obj):
    from .commands.status import status as show

    show(obj["conf"], obj["env"], debug=obj["debug"])


@click.command(help="Show what differs between anci.toml and the box. Changes nothing.")
@click.pass_obj
def plan(obj):
    from .commands.plan import plan as show

    show(obj["conf"], obj["env"])


@click.command(help="Bring the env in line with anci.toml. Ships no exes and runs no migrations.")
@click.pass_obj
def reconcile(obj):
    from .inspect.run import converge
    from .providers.libvirt import vm_reconcile
    from .secrets import sync_recipients

    conf, env_name = obj["conf"], obj["env"]
    vm_reconcile(conf, env_name)
    sync_recipients(conf, env_name)
    converge(conf, env_name, grow_disk=True)


@click.command(help="Where this env's files live on the box. Shows each file's size.")
@click.argument("kind", required=False, type=click.Choice(["exe", "db"]))
@click.argument("name", required=False, shell_complete=_info_names)
@click.pass_obj
def info(obj, kind, name):
    from .commands.info import info as show

    show(obj["conf"], obj["env"], kind, name)


@click.command(help="Deploy your latest builds. With no exe, every exe.")
@click.argument("exe", required=False, shell_complete=_names("exes"))
@click.pass_obj
def deploy(obj, exe):
    from .commands.deploy import deploy as run

    run(obj["conf"], exe, obj["env"])


@click.command(help="Show an exe's log.")
@click.argument("exe", shell_complete=_names("exes"))
@click.option("-f", "--follow", is_flag=True, help="Keep following the log.")
@click.pass_obj
def logs(obj, exe, follow):
    from .commands.deploy import app_logs

    app_logs(obj["conf"], exe, obj["env"], follow=follow)


@click.command(help="Apply pending migrations. With no db, every db.")
@click.argument("db", required=False, shell_complete=_names("dbs"))
@click.option("--baseline", is_flag=True,
              help="Accept edited or removed migration files as they are "
                   "(rewrites the applied-migrations record; never re-runs them).")
@click.pass_obj
def migrate(obj, db, baseline):
    from .commands.dbs import migrate as run
    from .providers.libvirt import require_running

    require_running(obj["conf"], obj["env"])
    run(obj["conf"], db, obj["env"], baseline=baseline)


@click.command(help="Restore dbs from their replicas. Destructive; with no db, every replicated one.")
@click.argument("db", required=False, shell_complete=_names("dbs"))
@click.option("--force", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--timestamp", metavar="TIME",
              help="Restore the db as of this time (RFC3339, e.g. 2026-10-14T15:00:00Z).")
@click.pass_obj
def regen(obj, db, force, timestamp):
    from .commands.dbs import db_regen

    db_regen(obj["conf"], db, obj["env"], force=force, timestamp=timestamp)


@click.command("local-schema", help="Build a schema-only copy of a db on this machine.")
@click.argument("db", shell_complete=_names("dbs"))
@click.pass_obj
def local_schema(obj, db):
    from .commands.dbs import local_schema as build

    print(build(obj["conf"], db, obj["env"]))


@click.command(help="Edit this env's secrets. Opens them decrypted in $EDITOR; the file stays encrypted.")
@click.pass_obj
def secrets(obj):
    from .commands.dbs import secrets_edit

    secrets_edit(obj["conf"], obj["env"])


ENV_VERBS = [up, down, status, plan, reconcile, info, deploy, logs, migrate, regen, local_schema, secrets]


def _check_env(conf: Conf, env_name: str, verb: str | None) -> None:
    if verb in (None, "secrets"):
        return
    validate.validate_env(conf, env_name)
    env = conf.envs[env_name]
    if sys.platform == "darwin" and not env.adopted and env.ip == "127.0.0.1":
        raise errors.ConfError(
            "a VM on your dev machine needs Linux",
            why="macOS can't run anci's x86_64 boxes natively",
            try_=f"under [env.{env_name}], set ip to a remote Linux libvirt host, "
                 "or mode = 'adopt' with a VPS",
        )


def _verb_rows(ctx: click.Context) -> list[tuple[str, str]]:
    rows = []
    for verb in ENV_VERBS:
        pieces = [p for p in verb.collect_usage_pieces(click.Context(verb, parent=ctx))
                  if p != "[OPTIONS]"]
        rows.append((" ".join([verb.name, *pieces]), verb.get_short_help_str(limit=60)))
    return rows


class _Ordered(click.Group):

    def list_commands(self, ctx):
        return list(self.commands)

    def format_commands(self, ctx, formatter):
        with formatter.section("Verbs"):
            formatter.write_dl(_verb_rows(ctx))


def _env_group(env_name: str) -> click.Group:
    @click.pass_context
    def callback(ctx):
        _check_env(ctx.obj["conf"], env_name, ctx.invoked_subcommand)

    return _Ordered(name=env_name, commands=ENV_VERBS, callback=callback,
                    subcommand_metavar="VERB [ARGS]...",
                    help=f"Commands for env '{env_name}'.")


@click.command(help="Generate a first anci.toml here.")
def init():
    from .commands.init import init as run

    return run()


@click.command(help="Print a shell completion script. Source it from your shell's rc file.")
@click.argument("shell", type=click.Choice(SHELLS))
def completion(shell):
    print(get_completion_class(shell)(root, {}, "anci", "_ANCI_COMPLETE").source())


class _Root(click.Group):

    def format_usage(self, ctx, formatter):
        formatter.write_usage("anci", "[OPTIONS] ENV VERB [ARGS]...")
        formatter.write_usage("anci", "[OPTIONS] COMMAND [ARGS]...", prefix="       ")

    def format_commands(self, ctx, formatter):
        with formatter.section("Commands"):
            formatter.write_dl([(name, cmd.get_short_help_str(limit=60))
                                for name, cmd in self.commands.items()])
        conf = _quiet_conf()
        if conf and conf.envs:
            with formatter.section("Configured Envs"):
                for name in sorted(conf.envs):
                    formatter.write(f"{'':>{formatter.current_indent}}{name}\n")
        with formatter.section("Verbs (anci ENV VERB)"):
            formatter.write_dl(_verb_rows(ctx))

    def list_commands(self, ctx):
        conf = _quiet_conf()
        return [*super().list_commands(ctx), *(sorted(conf.envs) if conf else [])]

    def get_command(self, ctx, name):
        cmd = super().get_command(ctx, name)
        if cmd is not None:
            return cmd
        if ctx.resilient_parsing:
            conf = _quiet_conf()
            return _env_group(name) if conf and name in conf.envs else None
        conf = load(find_conf())
        if name not in conf.envs:
            known = ", ".join(sorted(conf.envs)) or "(none)"
            raise errors.ConfError(
                f"'{name}' isn't an env in anci.toml",
                why=f"defined envs: {known}",
                try_=f"envs come from [env.*] tables — did you mean `anci {sorted(conf.envs)[0]} ...`?"
                if conf.envs else "add an [env.<name>] table to anci.toml",
            )
        ctx.obj["conf"], ctx.obj["env"] = conf, name
        return _env_group(name)


@click.group(cls=_Root, context_settings={"help_option_names": ["-h", "--help"]},
             help="Run your binaries on a box you own.")
@click.option("--debug", is_flag=True, help="Show raw commands and output; status adds a drift report.")
@click.version_option(__version__, "--version", prog_name="anci", message="%(prog)s %(version)s")
def root(debug):
    pass


root.add_command(init)
root.add_command(completion)


def _usage_error(e: click.UsageError) -> errors.ConfError:
    path = e.ctx.command_path if e.ctx else "anci"
    return errors.ConfError(e.format_message(), try_=f"{path} --help")


def _run(cmd: click.Command, argv: list[str], obj: dict, prog: str) -> int:
    try:
        rv = cmd.main(args=argv, prog_name=prog, standalone_mode=False, obj=obj)
    except click.UsageError as e:
        raise _usage_error(e) from None
    except click.Abort:
        raise KeyboardInterrupt from None
    except click.ClickException as e:
        raise errors.ConfError(e.format_message()) from None
    return rv if isinstance(rv, int) else 0


def _run_env(conf: Conf, env_name: str, argv: list[str], debug: bool) -> int:
    return _run(_env_group(env_name), argv, {"conf": conf, "env": env_name, "debug": debug},
                f"anci {env_name}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    debug, argv = _take_flag(argv, "--debug")
    try:
        return _run(root, argv, {"debug": debug}, "anci")
    except errors.Error as e:
        return _render_failure(e, debug)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as e:
        return _render_failure(e, debug)


if __name__ == "__main__":
    sys.exit(main())
