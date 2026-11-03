#!/bin/sh
case "$2" in
    started|updated|port-created) /bin/sh @APPLY_SCRIPT@ >/dev/null 2>&1 || true ;;
esac
exit 0
