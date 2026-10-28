# Box inspection

Reconciling a box is two steps: **inspect** (list what differs between the conf and the box) and **apply** (make the
changes). This doc covers inspect, which changes nothing on the box. Code: `anci/inspect/`.

## Design

```python
class Action(Enum):
    CREATE = "+"; UPDATE = "~"; DELETE = "-"

@dataclass(frozen=True)
class Change:
    identifier: str     # "exe.web", "litestream"
    action: Action
    fields: dict        # name -> (observed, target), only the ones that differ

class State(dict[str, dict]):
    """identifier -> fields, for the whole box."""

class TargetState(State): ...      # the box as the conf wants it
class ObservedState(State): ...    # the box as it is now

class Resource:
    name: str                                                                 # "exe"
    def target(self, conf: Conf, env_name: str) -> dict[str, dict]: ...      # identifier -> fields
    def probes(self) -> dict[str, str]: ...                                   # key -> shell command
    def observed(self, found: dict[str, list[str]]) -> dict[str, dict]: ...   # identifier -> fields

RESOURCES = (User(), Exe(), Db(), Caddy(), Litestream(), Logtrim(), Sshd(), Resolved(), Secrets(), Vendored())

def target_state(conf: Conf, env_name: str) -> TargetState: ...   # no ssh
def observed_state(conn: ssh.Conn, extra=None) -> ObservedState: ...   # one ssh call: every resource's probes
def diff(observed: ObservedState, target: TargetState) -> list[Change]: ...   # pure
```

`target` reads only the conf; `observed` reads only its probes' output, never the conf, so whatever the conf no
longer mentions is still seen and diffs as a delete.

## Resources

| Resource | Fields | Target from |
|---|---|---|
| `user.<n>` | `group` (in `anci`), `sudoers` (sha), `keys` (sha of `authorized_keys`) | `conf.users_for_env` |
| `exe.<n>` | `kind`, `unit`, `timer` (shas), `unit_link`, `timer_link`, `bin`, `boot` (its `*.wants` links), `port`, `blob`, `static`, `pending` (a restart still owed) | `conf.exes[n].resolve_for`; units rendered by `inspect.exe.render_units` |
| `db.<n>` | `exts` (name → blob id), `replica` (the `.anci-replica` record), `exists` (the db file; read, never compared) | `conf.dbs[n].resolve_for` |
| `caddy`, `litestream`, `logtrim`, `sshd`, `resolved`, `secrets` | a sha per file, a target per link; `boot` and `pending` for those with a unit or a restart | their renders in `inspect/singletons.py` |
| `vendored` | the blob id behind `/usr/local/bin/{caddy,litestream,sqlite3}` | the env's pinned versions |

The single-file resources share `FileSet` (`inspect/core.py`): they list their files and links, and return their
contents, or None when the box shouldn't have them (no `host` means no caddy, no replication means no litestream).

A user is on the box if they're in the `anci` group or have a `sudoers.d/anci-<n>` grant.

An exe's target has no `blob`, and has `static` only when the conf has no `static` (then it must be `None`). A
deploy sets them before diffing:

```python
target = target_state(conf, env_name)
target["exe.web"]["blob"] = layout.blob_id(sha)
changes = diff(observed_state(conn), target)
```

Not a resource: disk growth (nothing to read back).

## Diff rules

- **Create:** the identifier is only in the target. Fields are `(None, value)`.
- **Delete:** the identifier is only observed. Fields are `(value, None)`.
- **Update:** the identifier is in both. Only the fields that differ are listed.
- A field missing from the target isn't compared. A field set to `None` must be absent.

## Rules for resources

- Each file on the box belongs to exactly one resource. `exe` lists `/etc/anci/units` but skips the mechanism
  units (`units.MECHANISM_UNITS`).
- Names read from the box go through `layout.split_safe`.
- Resources never call each other. Related changes show up separately, and apply orders them:
  ```
  - db.audit  …
  ~ litestream  config: 1a2b3c4d5e6f → 9f8e7d6c5b4a
  ```

## Callers

- `anci <env> plan` (`commands/plan.py`) prints the diff.
- `status` reads the box with `observed_state`, its health probes riding along as `extra`; `--debug` prints the
  diff under "drift".
- `info` lists each resource's paths (`exe.paths`, `db.paths`, `FileSet.paths`).
- `regen` stops and starts the exes it sees on the box with exe steps (`run.run_steps`).
- [Apply](box-apply.md) carries out the diff.
