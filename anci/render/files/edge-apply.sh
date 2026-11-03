#!/bin/sh
set -u
route=@ROUTE_FILE@

clear_accept() {
    if nft list table ip libvirt_network >/dev/null 2>&1; then
        for h in $(nft -a list chain ip libvirt_network guest_input 2>/dev/null \
                   | sed -n 's/.*comment "@COMMENT@".*# handle \([0-9]*\)$/\1/p'); do
            nft delete rule ip libvirt_network guest_input handle "$h"
        done
    fi
    if command -v iptables >/dev/null 2>&1; then
        for n in $(iptables -L FORWARD --line-numbers -n 2>/dev/null \
                   | awk '/@COMMENT@/ {print $1}' | sort -rn); do
            iptables -D FORWARD "$n"
        done
    fi
}

if [ "${1:-}" = down ]; then
    nft delete table ip @TABLE@ 2>/dev/null
    clear_accept
    rm -f "$route"
    exit 0
fi

[ -f "$route" ] || exit 0
. "$route"
set -e
nft -f @RULESET_FILE@
clear_accept
if nft list chain ip libvirt_network guest_input >/dev/null 2>&1; then
    nft insert rule ip libvirt_network guest_input \
        oif "$BRIDGE" ip daddr "$IP" ct status dnat accept comment '"@COMMENT@"'
else
    iptables -I FORWARD 1 -d "$IP" -o "$BRIDGE" -m conntrack --ctstate DNAT \
        -m comment --comment @COMMENT@ -j ACCEPT
fi
