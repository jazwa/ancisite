# Reconciling

```
anci <env> reconcile
```

Reconcile brings the env in line with `anci.toml`, without shipping new exes or running db migrations.

## What changes

- **The env matches `anci.toml`.** Same as deploy: config, secrets, `host` and replication take effect.
  Exes you removed from the file are stopped.
- **VM size.** Changes to `vcpus`, `mem` and `disk` are applied, and the filesystem grows into a bigger disk. Raising
  `vcpus` or `mem` above the VM's current maximum takes effect on its next restart (`down`, then `up`).
- **Access.** Users added to or removed from `access-users` gain or lose ssh access to the box.
- **Secrets recipients.** If the users with access changed, the committed secrets file is re-encrypted for them.
  Commit the updated file: until you do, everyone else still has the old copy, which new users can't decrypt and
  removed users still can.

## What doesn't change

- **Your apps' versions.** Every exe keeps the version it has. Apps restart only if their config or secrets
  changed.
- **Your data.** Databases aren't touched.

## Good to know

- Disks can grow but not shrink.
- On an adopted env (`mode = 'adopt'`) there's no VM to resize; everything else applies.

## Next steps

```
anci <env> status            # each exe's state and URL, each db's replication lag
anci <env> status --debug    # also report anything in the env that still differs from anci.toml
```
