# Plan

```
anci <env> plan
```

Plan shows what on the box differs from `anci.toml`. It changes nothing.

## Output

One line per difference:

```
+ exe.api
~ exe.report  kind: service → job, unit, timer
- user.old-dev
```

| Mark | Meaning |
|---|---|
| `+` | in `anci.toml`, not on the box yet |
| `~` | on the box, but different from `anci.toml` |
| `-` | on the box, no longer in `anci.toml` |

A change lists what differs: `box value → anci.toml value` when the value is readable, or just its name (a file's
contents, a link). A box that matches says "no changes".

## What it covers

Users, exes (units, start at boot, links), dbs (extensions, replica), caddy, litestream, log trimming, the sshd and
DNS drop-ins, and the secrets file.

For a VM anci runs, also its size, as one line: `~ vm  vcpus: 2 → 4, disk: 20 GiB → 40 GiB`. On your dev machine
this is read without sudo, which works when you're in the `libvirt` group; otherwise the line is left out. `up` and
`reconcile` resize the VM.

A db you take out of `anci.toml` isn't listed: anci never deletes a db's data, so it stays on the box. Add the db
back with the same name and it picks that data up again.

## Next steps

```
anci <env> reconcile             # bring the box in line with anci.toml
anci <env> status --debug        # the same list, under the status report
```
