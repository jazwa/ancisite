#!/bin/sh
# rendered by anci — do not edit: load every local env's localhost forward
# (its nft table, then route_localnet on its bridge). Must not call virsh: it
# runs inside libvirt's hook.
set -u
rc=0
for f in @FWD_DIR@/*.nft; do
    [ -f "$f" ] || continue
    bridge=$(sed -n 's/^# bridge //p' "$f")
    # the bridge exists once libvirt starts its network; the hook re-runs this then
    [ -n "$bridge" ] && [ -d "/proc/sys/net/ipv4/conf/$bridge" ] || continue
    # the table first: its guard must be in before route_localnet opens the bridge
    # to 127.0.0.0/8 (a failed load is atomic, so the old table stays)
    nft -f "$f" || { rc=1; continue; }
    echo 1 > "/proc/sys/net/ipv4/conf/$bridge/route_localnet"
done
exit $rc
