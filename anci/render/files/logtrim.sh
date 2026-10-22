#!/bin/sh
# rendered by anci — do not edit: keep each exe's log under @LIMIT@ bytes. An
# oversized log is copied to <exe>.log.1 (replacing the last one) and truncated:
# systemd appends to it (O_APPEND), so the next write lands at the new end.
for f in @LOG_DIR@/*.log; do
    [ -f "$f" ] || continue
    [ "$(stat -c %s "$f")" -gt @LIMIT@ ] || continue
    cp -p "$f" "$f.1" && truncate -s 0 "$f"
done
