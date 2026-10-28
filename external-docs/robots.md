# ancisite

anci turns a server binary into a running web app. You give it a binary — one
that serves HTTP, or one that runs on a schedule — plus a small `anci.toml`, and
it provisions a box, runs the binary as a systemd service (or timer), puts a
reverse proxy with automatic TLS in front of it when you set a public `host`,
and sets up a SQLite database with continuous backups. It all runs over ssh, so
you never configure the box by hand.

Everything is a file: your app is a binary, your db is SQLite, your auth is SSH
keys. No containers, no k8s, no managed services.

## Concepts

- **box** — the machine an env runs on. Three kinds:
  - a VM on your dev machine (`ip = '127.0.0.1'`, `anci <env> up`);
  - a VM on a remote libvirt host you own (`ip = '<host>'`, `anci <env> up`);
  - someone else's box, e.g. a rented VPS (`mode = 'adopt'`, `anci <env> up`).
- **env** — a deployment target (`dev`, `prod`, …) = one box.
- **exe** — your binary: an HTTP service, or a scheduled job (`schedule`). Every
  exe runs on every env; `[exe.<name>.env.<env>]` overrides its config there.
- **db** — a SQLite database in an env, with migrations, optionally backed up
  continuously by litestream.
- **target** — where an exe's binary lives on the box: `/var/lib/anci/exe/<exe>/bin`,
  a symlink into a content-addressed blob store (so rollback is a symlink swap).
- **artifact** — a file anci renders from your conf (systemd unit, Caddyfile,
  litestream.yaml, cloud-init). On every run anci compares each artifact's hash
  with what's on the box and changes only what differs.

## Before you start

Everywhere: an ssh key loaded in your agent (`ssh-add -l`), and the `anci` CLI —
either `uv tool install .` from a checkout ([uv](https://docs.astral.sh/uv/)), or
`make build` → `dist/anci`, a single file that needs only Python 3.12+.

The CLI runs on Linux and macOS; on Windows, run it inside WSL2. A **VM on your dev
machine** is Linux-only; on a Mac, use a remote libvirt host or someone else's box.

> **Tested on:** an Ubuntu-family Linux dev machine running local VMs, and adopted
> Debian 13 boxes. **Untested:** macOS and WSL2 as the dev machine, remote libvirt
> hosts, SELinux hosts (Fedora, RHEL), and adopted boxes on other distros.

A **VM** (on your dev machine or a remote host) needs, on the machine that runs it,
any Linux distro with KVM (`/dev/kvm`) and:

- libvirt with QEMU, its `default` network active (`virsh net-start default`)
- `virt-install`, `qemu-img`, `curl`, `nft` (nftables)
- `xorriso` or `genisoimage` (virt-install builds the cloud-init disk with it)

Install them with your distro's packages, e.g.:

```bash
sudo apt install libvirt-daemon-system qemu-system-x86 qemu-utils virtinst curl nftables xorriso   # Debian/Ubuntu
sudo dnf install @virtualization curl nftables xorriso                                              # Fedora/RHEL
```

On your dev machine anci runs these through `sudo`, so `up`/`down` ask for your
password. On a remote host, your login there (the conf user your ssh key matches)
needs passwordless sudo.

Also join the `libvirt` group on your dev machine, so commands that only look at
your VMs (like `plan`) don't need your password:

```bash
sudo usermod -aG libvirt "$USER"    # then log out and back in
```

Without it those commands still work, but leave out what they'd read from the VM.

**Someone else's box** needs only ssh access (see the Quickstart for what it must run).

## Quickstart

```bash
uv tool install --editable .   # or: uv tool install .
anci init                      # generate an anci.toml
anci dev up                    # boot the local box
anci dev migrate main-db       # apply migrations (also starts backups)
anci dev deploy demo           # ship your binary
anci dev status                # what's running here
```

`up` works the same for every kind of box. For someone else's box (a rented VPS,
`mode = 'adopt'`), the first `up` sets it up: it shows the box's host key for
you to confirm, then logs in as `--user` (default `root`) to create your
accounts, and pins the key (commit `envs/<env>/hostkey`) once that succeeds.
Later `up`s log in as you; `--user` again reruns the setup.

```bash
anci prod up --user debian     # first time: confirm the host key, bootstrap
anci prod deploy demo
```

That first `up` **checks the box before touching it** and refuses anything it
can't support — so you never end up with a half-set-up box. The box must be:

- a **glibc** Linux (any distro — Debian, Ubuntu, Fedora, RHEL, openSUSE…; not a
  musl distro like Alpine), on **x86_64** (anci's pinned binaries are glibc amd64);
- actually **booted with systemd** (not just `systemctl` present);
- with **`bash`** available; and
- your login user (root, or `--user <name>`) must have **passwordless `sudo`**.

It also scans and records the box's host key for you to eyeball and commit.

## `anci.toml`

```toml
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA… jack@dev-machine'

[env.dev]
ip = '127.0.0.1'          # local box
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '2gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'       # a rented VPS
mode = 'adopt'
access-users = ['jack']
vm = { anci-version = '0.0.1' }   # adopt: no vcpus/mem/disk

[exe.demo]
source = './target/release/server'

[exe.demo.env.prod]
host = 'app.example.com'  # public site (Caddy + TLS)

[db.main-db]
migrations = './migrations/'          # SQL files, applied in order

[db.main-db.replication]              # optional: continuous backup (s3-compatible)
bucket = 'my-bucket'
```

## Configuration reference

The example above is a full dev → prod setup. Here's each section in detail.

### `[user.<name>]` — who may deploy

```toml
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA… jack@dev-machine'   # one key, or a list
```

Your identity is your SSH key. anci matches the key you present against these,
and an env's `access-users` decides who may deploy there.

**Limit:** the users-and-keys script is sent to the box as a single command
argument, which Linux caps at 128 KiB. Roughly 90 users with 5 keys each is the
ceiling; beyond that `up`/`reconcile`/`deploy` fail with "Argument list too
long". (Secrets encryption scales well past that: 300 keys encrypt in ~0.2s.)

### `[env.<name>]` — a deployment target = a box

```toml
[env.dev]
ip = '127.0.0.1'          # the machine: this dev machine, or a host / VPS address
access-users = ['jack']   # who may deploy here
# mode = 'adopt'          # someone else's box: set it up over ssh instead of provisioning a VM
# port = 2222             # optional: sshd port if it isn't 22
vm = { anci-version = '0.0.1', vcpus = 2, mem = '2gb', disk = '20gb' }
```

- `ip` is "the machine"; `mode` says who provisions the box on it (`full`, the
  default: anci runs a VM; `adopt`: an existing box):
  - `ip = '127.0.0.1'` — a VM anci runs on your dev machine (`anci <env> up`).
  - `ip = '<host>'` (`mode = 'full'`, the default) — a VM anci runs on a remote
    libvirt host (prerequisites in **Before you start**). The VM sits on the host's
    private network; anci reaches it through the host (`ssh -J <you>@<host>`).
    To serve a `host` from it, point the `A` record at the **host's** public
    address and open 80/tcp, 443/tcp and 443/udp in any host/provider firewall —
    the host forwards those ports to the VM. One public env per host: a second
    env with a `host` on the same machine is refused.
  - `mode = 'adopt'` + `ip` — someone else's box; the first `anci <env> up` sets it
    up (see the Quickstart).
- `port` is the box's sshd port (default 22) — set it if your provider moved ssh
  off 22.
- `vm = { … }` is the box's machine config (see below).

### `env.<name>.vm` — the box's machine config

```toml
[env.my-env]
vm = { anci-version = '0.0.1', vcpus = 2, mem = '2gb', disk = '20gb' }
```

- `anci-version` is the one knob that pins the supply chain — you never pin
  caddy/litestream versions individually.
- `vcpus`/`mem`/`disk` apply only to boxes anci provisions (local or on a remote
  host); on an adopted env (`mode = 'adopt'`) they're an error (the provider already sized it).

### `[exe.<name>]` — your binary

```toml
[exe.demo]
source = './target/release/server'   # the compiled binary (scripts aren't supported)
args = ['-config', '/etc/app.toml']  # optional
env-vars = { LOG_LEVEL = 'info' }    # optional
# static = './public/'               # optional: a directory shipped with every
#                                    # deploy (see "Static files" below)
# schedule = 'daily'                 # optional: a scheduled job instead of a
#                                    # service (systemd OnCalendar, not cron;
#                                    # 1-second resolution max)

[exe.demo.env.dev]                    # optional per-env overrides
source = './target/debug/server'      # e.g. dev deploys the debug build
env-vars = { LOG_LEVEL = 'debug' }

[exe.demo.env.prod]
host = 'app.example.com'              # optional, per env: public site (Caddy + TLS)
```

- **Every exe runs on every env.** A `[exe.demo.env.Y]` subtable never switches
  an exe off somewhere — it only overrides its config there (so a new env gains
  every exe automatically, and dev/prod stay as similar as possible).
- Per-env subtables can override `source`/`args`/`env-vars`/`schedule`/`static`/`host`; an env
  with no subtable just uses the exe's base config (above, `dev` deploys the
  debug build while `prod` falls back to the release one — different bytes, so
  separate blobs).
- There is no `target` key: every exe's stable on-guest path is
  `/var/lib/anci/exe/<exe>/bin` — a symlink into the blob store, what systemd
  executes.
- `host` (never on a local env; top-level only when the conf has a single,
  non-local env, otherwise per env) makes it a public website there: point the
  domain's `A` record at the box and Caddy fetches a TLS certificate from Let's
  Encrypt, compresses responses (gzip/zstd) and adds baseline security headers
  (HSTS, nosniff, SAMEORIGIN, referrer-policy; the app can override any of them).
  While the app restarts, Caddy holds each request for up to 3 s, so visitors
  see a short wait, not an error.
  Without one, a service on a remote env is internal to its box (for
  other exes to call). Two exes can't share a `host`.
- On a **local env** every service is at `http://<project>.localhost:<port>`
  (ports from 20000, 10 per env). Browsers treat `*.localhost` as secure, so
  `Secure` cookies work. anci forwards the ports with nftables on your machine;
  `up` and `deploy` ask for sudo when the forward needs installing or reloading.
- Every exe gets the env's runtime variables injected (see **What your app sees**
  below) — no wiring required.
- `schedule` turns the exe into a **scheduled job**: a `Type=oneshot` unit
  triggered by a systemd timer. Use systemd's `OnCalendar` syntax (e.g. `daily`,
  `*-*-* 03:00:00`) — cron syntax is *not* translated, and the finest granularity
  is **one second** (`*-*-* *:*:*`). Jobs get no `ANCI_PORT` and aren't routed by
  Caddy (there's no server to reach). `anci <env> status` shows when a job next runs.
- `static` is a directory (relative to `anci.toml`) of files your app serves or
  reads — HTML, CSS, images, templates. See **Static files** below.

### Static files

```toml
[exe.web]
source = './target/release/web'
static = './public/'

[exe.web.env.prod]
static = './public-prod/'            # optional per-env override
```

`anci <env> deploy web` ships the directory along with the binary, and your app
finds it at `$ANCI_STATIC_DIR`. Your app serves the files itself (e.g. Go's
`http.FileServer(http.Dir(os.Getenv("ANCI_STATIC_DIR")))`); Caddy isn't involved.

- **Versioned like the binary.** Each deploy installs the files as an immutable
  version (`/var/lib/anci/exe/<exe>/static-<id>/`, `<id>` = a hash of the file
  paths and contents) and swaps the `static` symlink to it atomically. Same files
  ⇒ same id, so an unchanged directory isn't uploaded again.
- **Only changed files are sent.** Each file goes into the same content-addressed
  store as the binary, named by its sha256; the version is then a tree of
  symlinks into that store. A file the box already has (unchanged, or shared with
  another version or exe) isn't uploaded and is stored once. So a one-line CSS
  fix in a 1 GB bundle uploads one file. It all goes over plain ssh — no rsync,
  and nothing to install on either side.
- **New files restart the service.** A deploy that changes the files restarts
  it, like a new binary (a job sees them on its next run). Open them through
  `$ANCI_STATIC_DIR`, not the version path it resolves to: old versions are
  deleted 5 minutes after they stop being used.
- **Read-only.** The files are root-owned and the app can't change them. Files
  your app writes (uploads, …) belong next to its db (`ANCI_DB_*`), not here.
- **Plain files only.** Symlinks, devices, and names with newlines or that
  aren't UTF-8 are refused at deploy; empty directories aren't shipped; every
  file is installed `0644`.
- `ANCI_STATIC_DIR` is set as soon as the conf has `static`, but the directory
  exists only once a deploy has shipped it (`up`/`reconcile` don't ship files).
  Removing `static` unlinks it on the next deploy or reconcile.
- Jobs can have `static` too (files a job reads).

### `[db.<name>]` — a SQLite database

```toml
[db.main-db]
migrations = './migrations/'         # .sql files, applied in filename order
extensions = ['./ext/uuid.so']       # optional: sqlite extensions
```

- `migrations` is a directory of `.sql` files, run in order. Each runs in its own transaction, so a file must not
  `BEGIN`/`COMMIT` or set `journal_mode`; anci makes every db WAL itself. A foreign key violation left by the
  migrations fails the migrate.
- Dbs are **env-scoped**: `main-db` on `dev` and `main-db` on `prod` are separate
  files (nothing is shared between envs). Every exe on an env gets `ANCI_DB_*`
  for every db on that env.
- `extensions` are SQLite `.so` files (FTS5, vector search, …) loaded for your
  migrations; their directory is exposed to the app as `ANCI_DB_<DB>_EXT_DIR`.
  Each file is shipped once to anci's content-addressed store (unchanged ones
  aren't re-uploaded) and symlinked into that directory; dropping one from
  `extensions` removes it on the next deploy or migrate.

### `[replication]` / `[db.<name>.replication]` — continuous backups

Replication is **s3-compatible storage only** (AWS S3, Cloudflare R2, MinIO,
Backblaze B2, …) via [litestream](https://litestream.io). A db with a
`[db.<name>.replication]` table is backed up continuously; a db without one
isn't.

```toml
[replication]                                     # shared defaults — nothing replicates by itself
bucket = 'my-bucket'
endpoint = 'https://<account-id>.r2.cloudflarestorage.com'   # optional: non-AWS S3-compatible stores
region = 'us-east-1'                              # optional: signing region (AWS auto-discovers when unset)
access-key-env = 'R2_ACCESS_KEY_ID'               # optional, paired: env var *names*
secret-key-env = 'R2_SECRET_ACCESS_KEY'           #   (see below)
sync-interval = '1s'                              # optional: how often the WAL ships (default 1s)
retention = '720h'                                # optional: how long replica data is kept (default 24h!)

[db.main-db.replication]     # opts main-db in — inherits everything above
path = 'main-db'             # optional: key prefix in the bucket (defaults to <env>/<db>)

[db.sessions.replication]    # opt-in another db: one line is enough
```

- **AWS S3 needs only `bucket`** — no endpoint, no key config. Anything else
  (R2, MinIO, B2, …) sets `endpoint`; litestream uses path-style addressing
  automatically for those.
- `region` is optional: AWS signs and locates the bucket without it (litestream
  looks the bucket's region up on startup), but setting it skips that lookup and
  works on locked-down networks. S3-compatible stores that validate a signing
  region (R2's `auto`, some MinIO/B2 setups) may want it explicitly.
- **Credentials come from the secrets file by env-var name.** Put the value in
  `anci <env> secrets` (or your plaintext `.anci/…` file) under the
 variables you named — here `R2_ACCESS_KEY_ID=…` / `R2_SECRET_ACCESS_KEY=…`.
  They're injected into the litestream service like your app's env vars, and
  the config references them as `${R2_ACCESS_KEY_ID}`. Leave both keys unset to
  use the standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` chain.
- Two dbs, or two envs, into the same bucket don't clash: each replica gets its
  own prefix (`<env>/<db>`, unless you set `path`).
- **Per env:** `[db.<name>.env.<env>.replication]` replicates the db only on
  that env, or overrides the base table's keys there. `replication = false`
  under `[db.<name>.env.<env>]` turns it off there. A `path` you set is used
  as-is, so don't give two envs the same one.
- `retention` is worth setting: litestream's **default is only 24 hours** —
  older replica data is pruned. `720h` = 30 days.

## What your app sees

Every exe is handed a few environment variables — no wiring:

| variable | meaning |
|---|---|
| `ANCI_ENV` | the env name (`dev`, `prod`) |
| `ANCI_BIND` | the address to bind: `0.0.0.0` on a local env, else `127.0.0.1` (services only) |
| `ANCI_PORT` | the port to bind — **services only** (scheduled jobs get none) |
| `ANCI_URL` | where it's reached: `http://<project>.localhost:<port>` on a local env, `https://<host>`, or `http://127.0.0.1:<port>` for a remote service without a `host` — **services only** |
| `ANCI_DB_<DB>_PATH` | path to a SQLite db file (`main-db` → `ANCI_DB_MAIN_DB_PATH`) |
| `ANCI_DB_<DB>_EXT_DIR` | the directory holding that db's extensions (when `extensions` is set) |
| `ANCI_STATIC_DIR` | the exe's static files, read-only (when `static` is set) |
| `ANCI_PORT_<EXE>` | each service's port on this box (`my-api` → `ANCI_PORT_MY_API`), to reach it at `127.0.0.1` — every exe, jobs included |

Bind to `$ANCI_BIND:$ANCI_PORT`. In `args`, `${VAR}` expands any of these
(e.g. `'http://127.0.0.1:${ANCI_PORT_API}'`). Open
`ANCI_DB_<DB>_PATH` with any SQLite client; `..._EXT_DIR` is a directory
(`.load` the files in it).

## Build a native binary

Your `source` is uploaded as-is and run on the guest, so it must run on *that*
box. anci deploys **native binaries, not scripts**: a `#!` script needs its
interpreter installed on the box, so scripts (and interpreted apps) are refused
up front. anci's own components (Caddy, litestream, sops) are static Go binaries
and run anywhere; **your binary is the variable**. Someone else's box is whatever the
provider pinned, which is often **older** than anci's pinned Debian-13 image, so the risky
direction is *build newest, run oldest*. Two rules:

1. **Build for amd64.** Every box is x86_64 (`up` checks this); an arm64 build
   fails at runtime with `Exec format error`.
2. **Don't depend on the guest's libc / CPU.** The provider's glibc may be older
   than your build host's, and its CPU may lack newer instructions.

| language | build | why |
|---|---|---|
| **Go** | `CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build` | fully static: no `GLIBC_2.x not found`, and it sidesteps the cgo + `systemd-resolved` DNS quirk |
| **Rust** | `cargo build --release --target x86_64-unknown-linux-musl` | static musl link; also avoid `-C target-cpu=native` (illegal instruction on older CPUs) |
| **Python / Node / …** | compile to a native binary (PyInstaller, Nuitka, `pkg`, …) | interpreted sources are scripts — not deployable |

The [`example/`](example/) app is a small [axum](https://github.com/tokio-rs/axum)
server you can use as a template.

anci checks the file **before uploading**: a script, a non-`x86_64` ELF, a
macOS/Windows binary, or a non-executable file is refused up front. It can't see
your libc version, so a dynamically-linked build against a newer glibc still
needs the static build above.

Two runtime gotchas, whatever the language:

- **Write only to your db dirs.** The unit is hardened (`ProtectSystem=strict`):
  the only writable paths are each db's directory (where `ANCI_DB_<DB>_PATH`
  lives) and a private `/tmp`. The working directory is read-only `/`, so never
  use relative paths.
- **Bind `$ANCI_BIND:$ANCI_PORT`.** anci sets both for you.

## Secrets

Keep secrets out of `anci.toml`. Two options:

- **Encrypted (recommended):** `anci <env> secrets` opens a SOPS-encrypted
  `envs/<env>/secrets.env` in `$EDITOR`; commit it. It's encrypted to every user
  in the env's `access-users` and decrypted with your ssh key at deploy time.
- **Plaintext (local convenience):** a git-ignored `.anci/secrets.<env>.env` of
  `KEY=value` lines — handy for `dev`, not for `prod`.

Either way, `anci <env> deploy` / `anci <env> migrate` courier the values to the env
(`/etc/anci/secrets.env`, mode 600); systemd injects them into your app as
environment variables. Only the *encrypted* file ever lands in git — the
plaintext `.anci/…` file is git-ignored.

## Backups (litestream)

Every db with a `[db.<name>.replication]` table is replicated continuously: one
litestream unit per box streams each SQLite file's WAL to an S3-compatible
store. Credentials ride the secrets file under the env-var names you chose (the
default is the `AWS_*` chain). `anci <env> status` shows the replication lag
per db — e.g. `2s since last sync`; if the replica store has nothing yet it
shows how far a restore would fall short.

Restore a db with `anci <env> regen` (see Commands below). If an empty db was replicated over
the real data (e.g. `migrate` on a rebuilt box before restoring), restore with
`--timestamp` set to a moment before that happened.

## Commands

Global flags: `--debug` (raw commands/stderr, and the `status` drift report),
`--version`. `anci` finds `anci.toml` by walking up from the working directory.

**Envs** — `anci <env> <verb>` (the env scopes every command):

| command | what it does |
|---|---|
| `anci <env> up` | bring the env's box up (idempotent): provision + boot its VM (`--recreate` rebuilds from scratch), resize it to the conf, and converge it (units, secrets, caddy, litestream; no new binaries), or set up an adopted box (`mode = 'adopt'`) on first contact (`--user <login>`, default `root`) |
| `anci <env> down` | stop the env's VM (keeps its disk) |
| `anci <env> status` | this env: versions, state, URLs, plus a db table (last migration, size, replication lag); `--debug` adds the drift report |
| `anci <env> plan` | what on the box differs from `anci.toml`, one line per difference (`+` add, `~` change, `-` remove); changes nothing |
| `anci <env> reconcile` | bring an existing env in line with `anci.toml` (users/keys, units, caddy, litestream, secrets, sshd; anything removed from `anci.toml` is removed from the box) — ships no binaries and runs no migrations |
| `anci <env> deploy [<exe>]` | deploy a new version (idempotent); no exe = every exe, in one converge |
| `anci <env> logs <exe> [-f]` | the exe's log file (`/var/log/anci/<exe>.log`; `-f` follows, journald fallback) |
| `anci <env> migrate [<db>]` | apply pending migrations (also starts backups); no db = every db |
| `anci <env> regen [<db>] [--force] [--timestamp T]` | restore a db from its replica (destructive; shows the replica's size first); no db = every replicated db; `--timestamp` (RFC3339, e.g. `2026-10-14T15:00:00Z`) restores the state as of that time |
| `anci <env> local-schema <db>` | build a schema-only copy of a db on this machine, no box needed |
| `anci <env> secrets` | edit the env's SOPS-encrypted secrets |

Plus one top-level command: `anci init` (wizard: generate a first `anci.toml`).

Every deploy also **reconciles the env**: units the conf no longer declares — a
removed exe, replication turned off, a `host` removed — are stopped, unlinked and
deleted, and the exe dirs of exes that are gone are dropped (their blobs are then
garbage-collected). Data is never deleted: the release log and app log of a removed exe stay on
the box, and the deploy reports them.

## On the box

Everything lives under three trees:

- `/var/lib/anci/` — uploaded binaries (`store/blobs/`), the per-exe `current`
  symlink, the per-exe `static` symlink and its versions, per-db files (`db/<db>/data.sql`), extensions, and the release log
  (the newest 8,192–16,384 deploys per exe).
- `/etc/anci/` — rendered systemd units, the Caddyfile, the litestream config, and
  the couriered `secrets.env`.
- `/var/log/anci/` — each exe's output, `<exe>.log`. An hourly timer
  (`anci-logtrim`) keeps each under 1 GiB: a full log moves to `<exe>.log.1`.

The exe names `caddy`, `litestream` and `anci-logtrim` are reserved.

Accounts:

- **`anci`** — one shared, unprivileged service account. Every app and litestream
  run as it, and it owns the databases.
- **your login account** (one per `[user.<name>]`) — what the CLI ssh's in as; a
  member of the `anci` group with passwordless sudo. `access-users` is the only
  gate, so treat this as admin access.
- **`caddy`** — the front proxy, present only when an env has a `host`.

Every converge (`up`, `deploy`, `migrate`, `reconcile`) also makes sshd key-only
(no password or keyboard-interactive logins, root by key only) and turns off
systemd-resolved's LLMNR.

## How it works

anci renders your systemd unit, Caddy config, litestream config, and db from the
TOML, then diff-and-applies them over ssh. Deploys are content-addressed (sha256
blobs + symlink swap), so re-deploying is a no-op.
Apps run as a shared, unprivileged `anci` service account (never root); your ssh
key maps you to a *login* account, and `access-users` decides what you can touch.
Failures are explained as what went wrong and what to try — add `--debug` for the
raw commands and output.
