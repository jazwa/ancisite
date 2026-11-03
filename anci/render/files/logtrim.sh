#!/bin/sh
for f in @LOG_DIR@/*.log; do
    [ -f "$f" ] || continue
    [ "$(stat -c %s "$f")" -gt @LIMIT@ ] || continue
    cp -p "$f" "$f.1" && truncate -s 0 "$f"
done
