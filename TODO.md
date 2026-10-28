# anci TODO

Open work, roughly in priority order. Inline `TODO(<tag>)` comments in the code
point back to the matching entry here.

## Security

### Caddy timeouts and limits — decided against, not doing
Looked into and dropped: Caddy's defaults are good enough, and anci leaves them.
The pinned Caddy (2.11.7) already sets `read_header 1m`, `read_body_idle 1m`,
`write_idle 1m`, `idle 5m` and `max_header_size 16KiB`, so header slowloris is
capped. What's left uncovered:
- the upload stall timer resets on every read and has no minimum rate, so a
  client dripping a byte every <1m holds a connection;
- no request-body size cap (apps enforce their own, e.g. Tare's 1 MB images).

If it's ever revisited, these were checked against the vendored binary
(`caddy adapt`): `servers { timeouts { read_header 10s; read_body_idle 30s
1024; idle 2m } }` (the min rate is plain bytes/s — `1KiB` is rejected), plus a
per-site `request_body { max_size 100MB }` backstop. Never set `write`,
`read_body` or a `write_idle` min rate: they break large transfers and streams.
L7 rate limiting stays in the app either way (stock Caddy has none).

### Box hardening — opt-in `harden` step  ·  `TODO(harden)` in `anci/providers/adopt.py`
Converge makes sshd key-only on every box (`anci/inspect/singletons.py`: password
and keyboard-interactive logins off, root by key only), and turns LLMNR off
(`anci/inspect/singletons.py`). The firewall is still the distro default, so a
future `0.0.0.0` bind is exposed. Tare's old Ansible (`devsec.hardening`
+ a UFW role) set, on prod:
- sshd: `PasswordAuthentication no`, `PermitRootLogin no`, pubkey-only,
  `AllowUsers <access-users>`, client-alive timeouts, hardened ciphers/KEX/MACs.
- firewall: default-deny incoming, open only 22/80/443.

Left to add, opt-in: the rest of the sshd settings above, and an nftables
ruleset that default-denies to 22/80/443 **and** caps concurrent connections
per source IP (`ct count`), to blunt single-source L7 floods. Must
be opt-in and must never lock out the login user — adopt is best-effort on a box
anci doesn't fully own.

### fail2ban — optional, after the firewall lands
Bans IPs at the firewall after repeated auth failures. Mainly an ssh
brute-force mitigator; little use against a distributed flood, and it overlaps
the app's per-IP rate limiting. Needs the nftables ruleset above to act on, and
it's a new service to run. Revisit once `harden` exists.

