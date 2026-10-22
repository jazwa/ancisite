"""Grow the guest root filesystem to fill its block device (local boxes only).

The host side (`vm_reconcile`) grows the qcow2/block device; this is what makes
the guest actually use the space. Remote boxes are user-provided, so anci never
repartitions them. Idempotent: `growpart` reports NOCHANGE and `resize2fs`
no-ops once the filesystem already fills the device.
"""

from __future__ import annotations

from .context import ConvergeContext, DesiredState

# Resolve the root source (e.g. /dev/vda1), split off the partition number, grow
# the partition, then the filesystem. `|| true` on both: an already-grown disk
# makes growpart exit non-zero and resize2fs print "nothing to do".
_GROW = (
    'root_src=$(findmnt -n -o SOURCE / 2>/dev/null || true); '
    'case "$root_src" in '
    '/dev/*[0-9]) '
    'disk=${root_src%[0-9]}; part=${root_src##*[!0-9]}; '
    'growpart "$disk" "$part" >/dev/null 2>&1 || true; '
    'resize2fs "$root_src" >/dev/null 2>&1 || true; '
    'esac'
)


def contribute(ctx: ConvergeContext) -> DesiredState:
    if ctx.conf.envs[ctx.env_name].adopted:
        return DesiredState(name="disk")  # someone else's box — not ours to grow
    if not ctx.grow_disk:
        return DesiredState(name="disk")  # only `up`/`reconcile` can have resized it
    return DesiredState(name="disk", prepare=[_GROW])
