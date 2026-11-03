"""`anci <env> plan` — what differs between anci.toml and the box. Changes nothing.

It looks at the box the way every run does (`inspect.run.look`), with the
builds `deploy` would ship (`deploy.local_builds`): a line only a deploy would
apply says so."""

from __future__ import annotations

from .. import auth, ssh, ui
from ..conf import Conf
from ..inspect import Action, Change, describe, diff, worth_showing
from ..inspect.run import look
from .deploy import local_builds


def plan(conf: Conf, env_name: str) -> None:
    builds = local_builds(conf, env_name, sorted(conf.exes), skip_unbuilt=True)
    user = auth.authorize(conf, env_name)
    conf_only, with_builds, observed = look(conf, env_name, ssh.connect(conf, env_name, user), builds)
    by_reconcile = diff(observed, conf_only)
    changes = worth_showing(diff(observed, with_builds))
    from ..providers.libvirt import vm_differences
    vm = vm_differences(conf, env_name)
    if vm:
        changes.append(Change("vm", Action.UPDATE, vm))
        by_reconcile.append(changes[-1])
    if not changes:
        print(f"{ui.green('✓')} {ui.dim('no changes — the box matches anci.toml')}")
    for c in changes:
        only_deploy = c not in by_reconcile
        print(describe(c) + (f"  {ui.dim(f'(anci {env_name} deploy)')}" if only_deploy else ""))
