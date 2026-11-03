#!/bin/sh
set -u
rc=0
for f in @FWD_DIR@/*.nft; do
    [ -f "$f" ] || continue
    bridge=$(sed -n 's/^# bridge //p' "$f")
    [ -n "$bridge" ] && [ -d "/proc/sys/net/ipv4/conf/$bridge" ] || continue
    nft -f "$f" || { rc=1; continue; }
    echo 1 > "/proc/sys/net/ipv4/conf/$bridge/route_localnet"
done
exit $rc
