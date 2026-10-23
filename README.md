# ancisite

Ancisite is a simple and opinionated web platform centered around **files**. 

Ancisite handles the **anci**llary (GET IT?!) stuff when it comes to operating a web**site** (WOW!)
so you can get a _good enough_ website up and running _fast_.


Ancisite turns a plain box into a running web app. Give it a server binary and an
`anci.toml`; you get a box, reverse proxy, TLS, a SQLite database, and backups —
all run with systemd over ssh, with your ssh keys as the auth.

Everything is a file on disk: the app, the database, auth, and config. Normal
Unix tools work on it, state is easy to inspect, and backups are just copies.

A box can be a VM on your dev machine, a VM on a remote libvirt host you own, or
someone else's box (e.g. a rented VPS) that anci sets up over ssh.

**Docs: [external-docs/](external-docs/)** — one page per command, plus [modes.md](external-docs/modes.md).

**Bot?: [robots.md](external-docs/robots.md)** — prerequisites, the full `anci.toml` format, and every command.

## Installing

```
curl -fsSL https://codeberg.org/zwa/ancisite/raw/branch/main/anci-install.sh | sh
```

To uninstall: `rm ~/.local/bin/anci` and `~/.cache/nce`.


## Dev

Install `anci` from your checkout as a command on your PATH, and run the tests:

```
uv tool install --editable .
uv run python -m unittest discover -s tests -t .
```


## Building

```
make release      # -> dist/release/anci-<os>-<arch> + SHA256SUMS: what anci-install.sh downloads
make build        # -> dist/anci (+ dist/anci.sha256): a zipapp, needs Python 3.12+ where you run it
```

To publish: tag `vX.Y.Z`, run `make release`, and upload everything in `dist/release/` to the Codeberg release
for that tag.

## Using anci

Define your users, envs (each with its machine config), and exes in `anci.toml`
(run `anci init`, or see `examples/guestbook/anci.toml`), then:

```
anci dev up               # provisions + boots the box (idempotent)
anci dev deploy demo      # deploy a new version of your app (uses your ssh keys)
curl http://<guest-ip>:8000/
anci dev status           # this env: versions, state, URLs
```

Handy verb: `anci dev logs demo` (your app's log file, `-f` follows).

The **env scopes every command** (`anci <env> <verb>`); `anci init` is the only
top-level one.

`anci dev down` stops the box (disk kept); `anci dev up --recreate` rebuilds it from
scratch.

**Secrets**: `anci <env> secrets` manages a SOPS-encrypted `envs/<env>/secrets.env` of `KEY=value` lines,
safe to commit. `deploy`, `migrate` and `reconcile` send it to the env, and your app reads the values as env vars.
For a quick local env, a git-ignored `.anci/secrets.<env>.env` works too.
