# ancisite

Ancisite is a simple and opinionated Heroku-lite-like web platform. 

**Docs: [external-docs/](external-docs/)** 

## Installing

```
curl -fsSL https://raw.githubusercontent.com/jazwa/ancisite/main/anci-install.sh | sh
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

## Shipping a release

Tag the commit you want to release and push the tag:

```
git tag v1.2.3
git push origin v1.2.3
```

The `release` GitHub Actions workflow
(`.github/workflows/release.yml`) then runs. It writes the tag's version into `anci/__init__.py` and
`pyproject.toml` in its own copy of the repo (nothing is committed), runs `make release`, and creates a GitHub
release for the tag with everything in `dist/release/` attached. `anci-install.sh` installs the latest release from
there.

The version in the repo itself stays a placeholder, so a local `make release` or `make build` reports that
placeholder, not a real release number.

If the workflow fails, commit the fix, then delete the tag and tag the new commit again:

```
git tag -d v1.2.3
git push origin :refs/tags/v1.2.3
```

## Using anci

### Setup

Run `anci init` in your project to write a first `anci.toml`. It asks for your username, your app's name and the
path to its binary (it looks in `target/release/` and a few other usual places), and picks up your ssh public key
from `ssh-agent` or `~/.ssh/`. You get one user, a local `dev` env and one exe. `anci init` won't overwrite an
existing `anci.toml`. See `example/anci.toml` for a fuller one.

Then:

```
anci dev up               # provisions + boots the box (idempotent)
anci dev deploy demo      # deploy a new version of your app (uses your ssh keys)
anci dev status           # this env: versions, state, URLs
```

Handy verb: `anci dev logs demo` (your app's log file, `-f` follows).

**Deploy**: `anci <env> deploy <exe>` ships your latest build of that exe, with its static files, and restarts it
if anything changed. Leave out `<exe>` to deploy every exe. It also applies any other edits to `anci.toml`,
secrets, `host` or replication, and stops exes you removed. It doesn't touch your databases (run
`anci <env> migrate` first if the new code needs a new schema) or the VM's size (that's `up` or `reconcile`). To
roll back, deploy the previous build. See [external-docs/deploy.md](external-docs/deploy.md).

**Status**: `anci <env> status` shows each exe's version, state (`serving`, `failing`, `scheduled`, ...), URL and
who deployed it last, each db's latest migration and how far its backup is behind, and the ssh command for the
box. `--debug` also lists anything on the box that differs from `anci.toml`. See
[external-docs/status.md](external-docs/status.md).

The **env scopes every command** (`anci <env> <verb>`); `anci init` is the only
top-level one.

`anci dev down` stops the box (disk kept); `anci dev up --recreate` rebuilds it from
scratch.

**Secrets**: `anci <env> secrets` manages a SOPS-encrypted `envs/<env>/secrets.env` of `KEY=value` lines,
safe to commit. `deploy`, `migrate` and `reconcile` send it to the env, and your app reads the values as env vars.
For a quick local env, a git-ignored `.anci/secrets.<env>.env` works too.

### `anci.toml` quick reference

```toml
anci-version = '0.0.1'             # optional: default for every env's vm.anci-version

[user.<name>]
ssh-pub-keys = 'ssh-ed25519 …'     # one key or a list

[env.<name>]
ip = '127.0.0.1'                   # 127.0.0.1 = a VM on this machine
access-users = ['<user>']          # which [user.*] can deploy here
mode = 'full'                      # optional: 'full' (anci runs the VM) or 'adopt' (an existing box)
port = 22                          # optional: the box's sshd port
vm = { vcpus = 2, mem = '2gb', disk = '20gb' }   # required for 'full'; 'adopt' takes only anci-version

[exe.<name>]
source = './target/…/web'          # required: the binary
args = ['--flag']                  # optional
env-vars = { KEY = 'value' }       # optional
schedule = 'daily'                 # optional: a scheduled job (systemd OnCalendar), not a service
static = './public/'               # optional: a directory shipped with every deploy
host = 'example.com'               # optional: the public domain (usually set per env, below)

[exe.<name>.env.<env>]             # optional per-env overrides of any key above
host = 'example.com'

[db.<name>]
migrations = './migrations/'       # required
extensions = ['./ext/foo.so']      # optional

[replication]                      # optional: backup defaults shared by every db
bucket = 'my-bucket'               # also: path, endpoint, region, access-key-env,
                                   # secret-key-env, sync-interval, retention

[db.<name>.replication]            # turns on backups for this db (may be empty)
```

- Names (`<name>`, `<env>`) are lowercase letters, digits and `-`, starting with a letter, up to 32 characters.
- Unknown keys are an error.
- `vcpus` is 1–255, `mem` at least `512mb`, `disk` at least `4gb`.
- A local env (`127.0.0.1`) can't have a `host`.
- Each env runs at most 10 services (exes without a `schedule`).

**Replication** backs up a db continuously to S3-compatible storage (AWS S3, Cloudflare R2, MinIO, B2, …) using
litestream. Nothing replicates by default: `[replication]` only holds shared settings, and each db opts in with its
own `[db.<name>.replication]` table, which can be empty to take every setting from `[replication]`.

- AWS S3 needs only `bucket`. Other stores also set `endpoint`, e.g. `'https://<account-id>.r2.cloudflarestorage.com'`.
- `access-key-env` and `secret-key-env` are the *names* of env vars, set together, whose values you put in
  `anci <env> secrets`. Leave both out to use the standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
- Each db's backup goes under `<env>/<db>` in the bucket unless you set `path`.
- Set `retention` (e.g. `'720h'` for 30 days): litestream keeps only 24 hours by default.
- Per env: `[db.<name>.env.<env>.replication]` changes settings for one env, and `replication = false` under
  `[db.<name>.env.<env>]` turns backups off there.
- `anci <env> status` shows how far behind each backup is, and `anci <env> regen` restores from it.

Full reference: [external-docs/robots.md](external-docs/robots.md#configuration-reference).
