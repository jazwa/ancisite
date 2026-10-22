# Up and down

```
anci <env> up
anci <env> up --recreate [--force]
anci <env> up --user <login>        # adopted env: set the box up as <login>
```

```
anci <env> down
```

Up creates or starts the env's box and brings it in line with `anci.toml`. Down stops it.

`up` depends on the env's [mode](modes.md). With `full`, it creates or starts the VM and applies its size. With
`adopt`, the first `up` sets up the existing box, and later ones only bring it in line with `anci.toml`.

## Up

### What changes

- **The box exists and is running.** The first up creates the VM at its own fixed address (on your machine, or on
  the remote host set in `ip`). Later ups start it if it's stopped.
- **VM size.** Changes to `vcpus`, `mem` and `disk` are applied, as with reconcile.
- **The env matches `anci.toml`.** Same as reconcile: users, config, secrets, `host` and replication take
  effect.
- **ssh is key-only.** Password logins are turned off, and root can log in only with a key. LLMNR is turned off.
- **sqlite3, litestream and caddy.** Their binaries (litestream and caddy are about 40 and 50 MB) are uploaded from
  your machine on the first up, used or not, so turning on replication or a `host` later doesn't wait on that upload.
  A local env gets no caddy.

### What doesn't change

- **Your exes and data.** Up ships no builds and runs no migrations. On a new box, run `migrate` and `deploy` next.
  If you're rebuilding a box that was lost, run `regen` first, to restore every replicated db from its replica, then
  `migrate`.

### Adopted envs

For `mode = 'adopt'`, anci doesn't create anything. The first up logs in as `root`, or as the user given with
`--user`, after you confirm the box's host key fingerprint. It then sets up the box: your users, the `anci` service
account, and the `/var/lib/anci` layout — no packages (the tools anci needs ship vendored on the first converge).
Once that succeeds, the host key is pinned in `envs/<env>/hostkey`; commit it.
Later ups behave like reconcile. `--user` on a box that's already set up runs the setup again, without the host key
check.

The box must run a glibc Linux on x86_64, booted with systemd, with bash and passwordless sudo for the login user.

## Recreate

`--recreate` deletes the box, disk included, and builds a new one: every deployed exe, database, secret, log and
release record on it is gone. anci asks for confirmation first; `--force` skips the prompt.

Afterwards, run `anci <env> regen` to restore the replicated dbs, then `migrate` and `deploy`.

## Down

Down shuts the box down cleanly and keeps its disk. The next `up` starts it as it was. If the box doesn't shut down
within 60 seconds, it's powered off, and its most recent writes may be lost.

Down only applies to envs whose box anci created, not to adopted ones.

## Good to know

- Up is safe to repeat; with nothing to do, it does nothing.
- On your machine, up and down ask for your sudo password to manage the VM.

## Next steps

```
anci <env> status            # the env's exes, dbs and ssh command
anci <env> migrate          # create a new box's dbs
anci <env> deploy           # put exes on a new box
```
