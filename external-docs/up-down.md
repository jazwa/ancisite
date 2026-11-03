# Up and down

```
anci <env> up
anci <env> up --recreate [--force]
```

```
anci <env> down
```

Up creates or starts the env's box and brings it in line with `anci.toml`. Down stops it.

## Up

### What changes

- **The box exists and is running.** The first up creates the VM (on your machine, or on the remote host set in
  `ip`) and gives it the first private address that no other anci box on that machine is using. Later ups start it
  if it's stopped, and the box keeps its address, even through `--recreate`.
- **The address is recorded.** For a remote env, it's saved in `envs/<env>/address`; commit it, like
  `envs/<env>/hostkey`, so everyone reaches the same box. For a local env, it stays on your machine, in
  `~/.local/state/anci/addresses/`. Other commands read it from there; before the first up, they say there's no
  box yet.
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

## Recreate

`--recreate` deletes the box, disk included, and builds a new one: every deployed exe, database, secret, log and
release record on it is gone. anci asks for confirmation first; `--force` skips the prompt.

Afterwards, run `anci <env> regen` to restore the replicated dbs, then `migrate` and `deploy`.

## Down

Down shuts the box down cleanly and keeps its disk. The next `up` starts it as it was. If the box doesn't shut down
within 60 seconds, it's powered off, and its most recent writes may be lost.

## Good to know

- Up is safe to repeat; with nothing to do, it does nothing.
- On your machine, up and down ask for your sudo password to manage the VM.

## Next steps

```
anci <env> status            # the env's exes, dbs and ssh command
anci <env> migrate          # create a new box's dbs
anci <env> deploy           # put exes on a new box
```
