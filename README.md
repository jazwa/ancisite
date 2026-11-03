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

If the workflow fails, commit the fix, then delete the tag and tag the new commit again:

```
git tag -d v1.2.3
git push origin :refs/tags/v1.2.3
```

## Using anci

### Setup

Run `anci init` in your project to write a first `anci.toml`. 
Then:

```
anci <env> up               # provisions + boots the box (idempotent)
anci <env> deploy           # deploy a new version of your app(s) (uses your ssh keys)
anci <env> status           # this env: versions, state, URLs
```

Handy verb: `anci <env> logs demo` (your app's log file, `-f` follows).

**Secrets**: `anci <env> secrets` manages a SOPS-encrypted `envs/<env>/secrets.env` of `KEY=value` lines,
safe to commit. `deploy`, `migrate` and `reconcile` send it to the env, and your app reads the values as env vars.

### `anci.toml` quick reference

```toml
anci-version = '0.0.1'             # optional: default for every env's vm.anci-version

[user.<name>]
ssh-pub-keys = 'ssh-ed25519 …'     # one key or a list

[env.<name>]
ip = '127.0.0.1'                   # ip of the actual machine running the anci vm.
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

```

- Names (`<name>`, `<env>`) are lowercase letters, digits and `-`, starting with a letter, up to 32 characters.
- A local env (`127.0.0.1`) can't have a `host`.

### Setting up replication

Replication backs up a db continuously to any S3-compatible storage using litestream. This is useful for disaster recovery.

1. Create a bucket and an access key for it with your storage provider.
2. Add the bucket to `anci.toml`, and turn replication on for each db you want backed up:

   ```toml
   [replication]
   bucket = 'my-bucket'
   endpoint = 'https://<your-s3-endpoint>'
   access-key-env = 'S3_ACCESS_KEY_ID'
   secret-key-env = 'S3_SECRET_ACCESS_KEY'
   retention = '720h'                    # optional: 24h by default
   sync-interval = '60s'                 # optional: 1s by default

   [db.main.replication]                 # enables replication on the db 
   ```

3. Put the key in the env's secrets with `anci <env> secrets`, under the names you chose:

   ```
   S3_ACCESS_KEY_ID=...
   S3_SECRET_ACCESS_KEY=...
   ```

4. Run `anci <env> migrate` (or `deploy`, or `reconcile`). Backups start once the db exists.
5. Check `anci <env> status`: each db shows how far behind its backup is, e.g. `2s since last sync`.

Each db's backup goes under `<env>/<db>` in the bucket. To restore, run `anci <env> regen <db>` (see
[external-docs/regen.md](external-docs/regen.md)). To turn backups off on one env, put `replication = false` under
`[db.<name>.env.<env>]`.
