"""Accounts + base layout: everything that must exist before anything else.

One root script that reconciles the guest accounts (per `access-users`), the
shared `anci` service user/group, and the base directory skeleton. No restart
step — it's all `prepare` (folded into the single sudo run).
"""

from __future__ import annotations

from ..render import shell
from .context import DesiredState, ConvergeContext


def contribute(ctx: ConvergeContext) -> DesiredState:
    users = ctx.conf.users_for_env(ctx.env_name)
    return DesiredState(
        name="accounts",
        prepare=[
            shell.render_accounts_script(users),
            shell.render_layout_script(),
        ],
    )
