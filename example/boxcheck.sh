#!/bin/bash
# Print everything anci manages on the box, to compare against anci.toml.
#   ssh zhao@<vm ip> 'sudo bash -s' < boxcheck.sh
set -u
shopt -s nullglob

h() { printf '\n== %s\n' "$*"; }

h "users: anci group members"
getent group anci | cut -d: -f4 | tr ',' '\n' | sed '/^$/d;s/^/  /'
h "users: sudoers grants (name, mode)"
for f in /etc/sudoers.d/anci-*; do echo "  ${f#/etc/sudoers.d/anci-} $(stat -c %a "$f")"; done
h "users: authorized_keys (user, number of keys)"
for k in /home/*/.ssh/authorized_keys; do u=${k#/home/}; echo "  ${u%%/*} $(grep -c . "$k")"; done

h "exes: rendered units (/etc/anci/units)"
for f in /etc/anci/units/*; do echo "  ${f##*/}"; done
h "exes: systemd links into /etc/anci"
for l in /etc/systemd/system/*; do
  [ -L "$l" ] || continue
  t=$(readlink "$l"); case "$t" in /etc/anci/*) echo "  ${l##*/} -> $t" ;; esac
done
h "exes: start at boot (*.wants links to anci units)"
for l in /etc/systemd/system/*.wants/*; do
  [ -e "/etc/anci/units/${l##*/}" ] && echo "  ${l#/etc/systemd/system/}"
done
h "exes: dirs (current build, static version, owed restart)"
for d in /var/lib/anci/exe/*; do
  n=${d##*/}
  cur=$(readlink -e "$d/current" || echo "MISSING")
  st=$(readlink "$d/static" || echo "-")
  pend=$([ -e "$d/.restart-pending" ] && echo " RESTART-OWED")
  vs=("$d"/static-*)
  echo "  $n  current=${cur##*/}  static=${st##*/}  versions=${#vs[@]}$pend"
done
h "exes: running (anci units) and since when"
for f in /etc/anci/units/*.service /etc/anci/units/*.timer; do
  u=${f##*/}
  echo "  $u $(systemctl is-active "$u") $(systemctl show -p ActiveEnterTimestamp --value "$u")"
done
h "exes: listening ports"
ss -ltnpH | awk '{print "  " $4, $6}'
h "exes: logs and release history (kept after an exe is removed)"
for f in /var/log/anci/*.log; do echo "  ${f##*/}"; done
for d in /var/lib/anci/releases/*; do echo "  releases/${d##*/} $(cat "$d"/* </dev/null 2>/dev/null | wc -l) lines"; done

h "dbs: dirs, data file, replica record"
for d in /var/lib/anci/db/*; do
  rec=$([ -f "$d/.anci-replica" ] && cat "$d/.anci-replica" || echo "-")
  ls_state=$([ -d "$d/.data.sql-litestream" ] && echo " litestream-state")
  echo "  ${d##*/} $(stat -c '%U:%G %a' "$d") data=$([ -f "$d/data.sql" ] && echo yes || echo no) replica=[$rec]$ls_state"
done
h "dbs: tables and applied migrations"
for f in /var/lib/anci/db/*/data.sql; do
  n=${f%/data.sql}; n=${n##*/}
  echo "  $n tables: $(sqlite3 "$f" .tables | tr -s ' \n' ' ')"
  echo "  $n migrations: $(sqlite3 "$f" 'SELECT name FROM _anci_migrations' 2>/dev/null | tr '\n' ' ')"
done
h "dbs: extensions (anything in an ext dir)"
for e in /var/lib/anci/ext/*/*; do echo "  ${e#/var/lib/anci/ext/} -> $(readlink -e "$e" || echo "NOT A LINK OR BROKEN")"; done

h "env: secrets file"
[ -f /etc/anci/secrets.env ] && echo "  $(stat -c '%U:%G %a' /etc/anci/secrets.env) $(grep -c '=' /etc/anci/secrets.env) vars" || echo "  none"
h "env: litestream and caddy files"
for f in /etc/anci/litestream.yaml /etc/anci/caddy/Caddyfile; do [ -e "$f" ] && echo "  $f"; done
echo "  litestream: $(systemctl is-active litestream.service)  caddy: $(systemctl is-active caddy.service)"
h "env: owed restarts of shared services"
for m in /var/lib/anci/.restart-* /var/lib/anci/.reload-* /var/lib/anci/.enable-*; do echo "  $m"; done
h "env: vendored binaries"
for b in caddy litestream sqlite3; do echo "  $b -> $(readlink -e /usr/local/bin/$b || echo MISSING)"; done
h "env: sshd, resolved, logtrim"
for f in /etc/ssh/sshd_config.d/00-anci.conf /etc/systemd/resolved.conf.d/anci.conf /etc/anci/logtrim.sh; do
  echo "  $f $([ -f "$f" ] && sha256sum "$f" | cut -c1-12 || echo MISSING)"
done
echo "  sshd -T: $(/usr/sbin/sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin) ' | tr '\n' ' ')"
echo "  anci-logtrim.timer: $(systemctl is-active anci-logtrim.timer)"

h "store: blobs nothing links to (gc removes these once 5 minutes old)"
real=$(cd /var/lib/anci/store/blobs && pwd -P)
refs=$({ for f in /var/lib/anci/exe/*/current /usr/local/bin/caddy /usr/local/bin/litestream \
           /usr/local/bin/sqlite3 /var/lib/anci/ext/*/*; do readlink -f "$f"; done
         for s in /var/lib/anci/exe/*/static; do v=$(readlink -f "$s") && find "$v" -type l -exec readlink -f {} +; done
       } 2>/dev/null | sort -u)
total=0; loose=0
for b in "$real"/*; do
  total=$((total + 1))
  grep -qxF "$b" <<<"$refs" || { loose=$((loose + 1)); echo "  ${b##*/} $(( ($(date +%s) - $(stat -c %Y "$b")) / 60 )) min old"; }
done
echo "  $total blobs, $loose unreferenced"
h "disk"
echo "  $(nproc) vcpus, $(free -m | awk '/Mem:/{print $2}') MiB memory"
df -h / | tail -1 | awk '{print "  / " $2 " size, " $3 " used"}'
