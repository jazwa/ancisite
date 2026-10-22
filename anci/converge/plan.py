"""Aggregate every reconciler into one `EnvPlan`."""

from __future__ import annotations

from . import accounts, caddy, dbs, disk, exes, litestream, logtrim, orphans, resolved, secrets, sshd, vendored
from .context import ConvergeContext, EnvPlan

# The reconciler set, in reading order. Aggregation is order-independent, but
# keeping the order stable makes plans and tests easy to read.
RECONCILERS = (accounts, disk, exes, dbs, vendored, caddy, litestream, logtrim, resolved, sshd, secrets, orphans)


def plan_env(ctx: ConvergeContext) -> EnvPlan:
    """Render the whole env's desired state. Pure — no ssh."""
    return EnvPlan(desired_states=[r.contribute(ctx) for r in RECONCILERS])
