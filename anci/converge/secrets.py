"""Secrets courier: the env's resolved secrets file (mode 600).

The content is resolved once in `plan_env` and handed in via `ctx.secrets`, so
this stays pure. A changed file marks the exes/litestream restart-worthy — that
falls out of the apply result, not from here.
"""

from __future__ import annotations

from .. import layout
from ..apply import FileArtifact
from .context import DesiredState, ConvergeContext


def contribute(ctx: ConvergeContext) -> DesiredState:
    content, _label = ctx.secrets
    if content is None:
        return DesiredState(name="secrets")
    return DesiredState(
        name="secrets",
        files=[
            FileArtifact(
                path=layout.SECRETS_FILE,
                content=content,
                mode=layout.SECRET_MODE,
            )
        ],
    )
