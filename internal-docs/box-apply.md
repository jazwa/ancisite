# Box apply

Status: built (`anci/inspect/walk.py`, `anci/inspect/run.py`; each resource's `steps()`).

Apply carries out the changes that [inspect](box-inspection.md) finds. It replaces converge.

## How it works

1. Inspect lists the changes.
2. Each change maps to a few **steps**. For example, a new binary for `web` becomes: link it, record the
   release, restart `web`.
3. The steps are put in order and joined into **one shell script**.
4. anci uploads everything the script needs in one go, then runs the script.

That's four ssh calls per run, however much changed: lock, inspect, upload, script. Converge today makes one or
more calls per file, which is slow on a remote box.

```python
@dataclass(frozen=True)
class Step:
    id: str                       # "exe.web/restart"
    script: str                   # shell, run as root
    after: tuple[str, ...] = ()   # steps that must run first
```

Each resource provides everything the engine needs about it, so the engine and the commands never name a
resource or read its fields:

| Hook | What it gives |
|---|---|
| `steps(change, ctx)` | the steps for one of its changes |
| `rules` | how its steps order against others' (`("secrets/write", "*/restart")`) |
| `always(ctx)` | steps every run has (sshd's check, litestream's "running once a db exists") |
| `with_inputs(target, inputs)` | what this run's inputs set on the target (a deploy's binary is the exe's `blob`) |
| `blobs(target, inputs)` | the blobs it may ship, so one probe can tell which the box lacks |

A run's inputs travel by resource name (`exe.deploy_inputs`); work on another machine, like the hypervisor's edge,
is something a resource's steps add to `ctx.elsewhere`, run before the script. A step's `says` is what the run's
summary prints.

Each resource turns its own changes into steps. Two changes that need the same step (say, a new binary and new
secrets both restart `web`) produce the same id, and it runs once.

## What each change does

| Change | Steps |
|---|---|
| user added or changed | create the account, add to `anci`, write sudoers and ssh keys |
| user removed | remove from `anci`, delete sudoers and ssh keys (the account stays) |
| exe added | write units, link the binary, start it |
| exe binary or static files | link them, record the release, restart (a job just uses them on its next run) |
| exe unit changed | write it, restart (a job's timer is re-armed) |
| exe service ↔ job | stop the old kind, write the new units, start the new kind |
| exe removed | stop, disable, delete its units and files (logs and releases stay) |
| db added | make its dirs |
| db extensions | link the new ones, remove the old ones |
| db replica moved | stop litestream, wipe that db's local litestream state, start litestream |
| db removed | nothing (the data stays) |
| secrets changed | write the file, restart every service and litestream |
| caddy added or changed | write its files, update the edge, restart or reload |
| caddy removed | stop, disable, delete its files (certificates stay) |
| litestream added or changed | write its files, restart it (once a db exists) |
| litestream removed | stop, disable, delete its files |
| sshd, resolved, logtrim | write the file, reload what reads it |
| caddy or litestream binary | restart that service |

Every run also checks that password logins are off, starts litestream if it's stopped, and cleans up unused files.

## Order

Steps run in an order that satisfies:

- Files are written, then `systemctl daemon-reload`, then services restart.
- Secrets are written before the restarts that need them.
- Litestream stops before a db's replica state is wiped, and starts after.
- A db's dirs exist before any app starts.
- If ports shift, the app that held a port stops before another app takes it.
- Cleanup runs last.

## Unknown changes fail

If a change has no row in the table above, apply stops before touching the box. Each resource lists its fields
and the step for each one, so a new field without a step fails the tests.

## Interrupted runs

The script stops at the first failure. A restart it still owed shows up as a change on the next run, so nothing
is lost.

## What inspect reads for apply

- each exe's port (for the port-shift order)
- whether a restart is still owed
- boot links for caddy, litestream and logtrim
- the caddy, litestream and sqlite3 binaries
- whether each db file exists (litestream only starts once one does)

## Tests

`tests/inspect/fakebox.py` runs apply's real scripts in a temp dir, with stubs for systemctl, the account tools and
sshd. `tests/inspect/test_apply.py` converges it through each kind of change, and after each run checks that
inspecting the box again finds nothing left to change.
