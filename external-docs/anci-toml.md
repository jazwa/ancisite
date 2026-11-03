# anci.toml reference

Every key anci accepts. What the keys *do* at runtime is covered in [robots.md](robots.md#configuration-reference).

## General rules

- Unknown keys are an error.
- No control characters (newlines, tabs, ...) anywhere, keys or values.
- User, env, exe and db names: lowercase letters, digits and `-`, start with a letter, at most 32 characters.
- Reserved names: `root`, `anci`, `caddy`, `litestream`, `anci-logtrim`, `ssh`, `sshd`, `networking`,
  `systemd-networkd`, `init`, `completion`.
- You need at least one `[env.<name>]`.
- Paths are relative to `anci.toml`.

## Top level

| Key | Type | Notes |
|---|---|---|
| `anci-version` | string | Optional. Default for every env's `vm.anci-version`. |

## `[user.<name>]`

| Key | Type | Notes |
|---|---|---|
| `ssh-pub-keys` | string or list | Required. Each one is a whole line from a `.pub` file, no options in front. Two users can't share a key. |

## `[env.<name>]`

| Key | Type | Notes |
|---|---|---|
| `ip` | string | Required. `127.0.0.1` means a VM on this machine; anything else is a remote libvirt host. |
| `access-users` | string or list | Required. Each must be a `[user.*]`. |
| `vm` | table | Required. See below. |

### `vm`

Usually written inline: `vm = { anci-version = '0.0.1', vcpus = 2, mem = '2gb', disk = '20gb' }`.

| Key | Type | Notes |
|---|---|---|
| `anci-version` | string | Required here or at the top level. Must be a version anci knows. |
| `vcpus` | int | 1–255. |
| `mem` | `'512mb'`, `'4gb'`, or int MiB | At least 512 MiB. |
| `disk` | `'20gb'` or int GiB | At least 4 GiB. |

`vcpus`, `mem` and `disk` are required.

## `[exe.<name>]`

| Key | Type | Notes |
|---|---|---|
| `source` | path | Required. A compiled binary. |
| `args` | list of strings | |
| `env-vars` | table of strings | Names are letters, digits and `_`, not starting with a digit or `ANCI_`. |
| `schedule` | string | systemd `OnCalendar` (`'daily'`, `'*-*-* 03:00:00'`). Makes it a job, not a service. |
| `static` | path | A directory shipped with every deploy. |
| `host` | string | Public domain. Only allowed here if the file has exactly one env and it isn't local. Otherwise set it per env. |

### `[exe.<name>.env.<env>]`

Overrides for one env. Takes `source`, `args`, `env-vars`, `schedule`, `static` and `host`. `env-vars` is merged
with the base table; the rest replace it. The env has to exist.

- No `host` on a local env.
- Two exes on the same env can't have the same `host`.
- At most 10 services (exes without `schedule`) per env.

## `[db.<name>]`

| Key | Type | Notes |
|---|---|---|
| `migrations` | path | Required. Directory of `.sql` files. |
| `extensions` | list of paths | SQLite `.so` files. |
| `replication` | table | Turns on backups for this db. Can be empty. See below. |

### `[db.<name>.env.<env>]`

| Key | Type | Notes |
|---|---|---|
| `replication` | table or `false` | A table changes settings for this env. `false` turns backups off here. |

## `[replication]`

Shared backup settings. On its own it backs up nothing; a db needs its own `[db.<name>.replication]`.

Settings are layered: `[replication]`, then `[db.<name>.replication]`, then `[db.<name>.env.<env>.replication]`.
The same keys work in all three, and a required key can be set in any of them.

| Key | Type | Default | Notes |
|---|---|---|---|
| `bucket` | string | | Required. No spaces or `/`. |
| `path` | string | `<env>/<db>` | Key prefix in the bucket. No leading or trailing `/`, no `.` or `..`. |
| `endpoint` | URL | | Required. Your store's URL, `https://` or `http://`. |
| `access-key-env` | env var name | | Required. Name of the env var holding the access key ID. The value goes in `anci <env> secrets`. |
| `secret-key-env` | env var name | | Required. Same, for the secret access key. |
| `region` | string | | Signing region, if your store wants one. |
| `sync-interval` | Go duration | `'1s'` | e.g. `'5m'`. |
| `retention` | Go duration | `'24h'` | You probably want longer, e.g. `'720h'` (30 days). |

Two dbs or envs can't back up to the same bucket and path.
