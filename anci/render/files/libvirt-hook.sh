#!/bin/sh
# rendered by anci — do not edit: re-run @APPLY_SCRIPT@ when libvirt starts a
# network or (re)builds its rules (boot, daemon restart, a guest's port) — nft
# tables and sysctls don't persist. The script is idempotent and cheap.
# Run through sh, not exec'd: Ubuntu's AppArmor profile for libvirtd confines
# its hooks and denies exec outside /bin, /usr/bin, … — sh leaves the profile.
case "$2" in
    started|updated|port-created) /bin/sh @APPLY_SCRIPT@ >/dev/null 2>&1 || true ;;
esac
exit 0
