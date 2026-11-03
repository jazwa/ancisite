# Status

```
anci <env> status
anci <env> status --debug
```

Status shows what's running in the env.

## What it shows

- **Exes.** Each exe's deployed version, its state, its URL, and who deployed it last and when.
- **Databases.** Each db's last applied migration, its size, how far its replica is behind, and where the
  replica goes (`bucket/path`, or — when replication is off).
- **Caddy.** Whether the HTTPS front is running. On a remote env with no `host` it shows "not configured": your
  exes have no public HTTPS URL.
- **How to ssh in.** The ssh command for the box.

## Exe states

| State | Meaning |
|---|---|
| serving | running and answering on its port |
| starting | running, not answering yet |
| failing | it crashed, and systemd restarts it again shortly (with how many times it has) |
| failed | it kept crashing, and systemd stopped restarting it |
| stopped | deployed, but not running |
| scheduled · next … | a job that hasn't run yet, and when it next runs |
| running · next … | a job running now |
| succeeded · next … | a job whose last run succeeded |
| failed · next … | a job whose last run failed (see `anci <env> logs <exe>`) |
| timer stopped | a deployed job whose timer isn't armed |
| not deployed | never deployed to this env |
| box down | the box didn't answer |

The URL is shown only when the exe is answering.

## Replication

| Value | Meaning |
|---|---|
| 3s since last sync | the replica is that far behind |
| off | the db isn't replicated on this env |
| — | the db doesn't exist yet; run `anci <env> migrate <db>` |
| litestream error | replication isn't working |

## --debug

Also lists anything in the env that differs from `anci.toml`: what `anci <env> plan` shows (including a caddy,
litestream or sqlite3 binary other than the pinned one), and a release log that disagrees with what's deployed. A
clean env says "no drift".

## Next steps

```
anci <env> logs <exe> -f         # follow an exe's output
anci <env> reconcile             # fix drift: bring the env back in line with anci.toml
anci <env> info exe <exe>        # where an exe's files live on the box
```
