# Regen

```
anci <env> regen [<db>]
anci <env> regen [<db>] --timestamp 2026-10-14T15:00:00Z
anci <env> regen [<db>] --force
```

Regen replaces an env's db with a copy restored from its replica: the latest one, or the db as of
`--timestamp`. Use it to recover from bad data or a lost box. Leave out `<db>` to restore every db the env
replicates, in name order. Only dbs with replication configured can be regenerated!

## What changes

- **The db is replaced.** Everything written after the restored point is gone for good.
- **Your apps restart.** They're stopped during the restore and started again afterwards, even if it fails.

## What doesn't change

- **Other dbs.** Only the db you name is restored (or, with no `<db>`, the replicated ones). If one fails, the
  ones before it stay restored.
- **Your exes' versions, and the env's config.** Regen doesn't deploy or reconcile.

## Regen is one-way

After a restore, replication continues from the restored db; the changes you rolled back are pruned once
`retention` runs out. If you might want the current data, copy the db file off the box first
(`anci <env> info db <db>` shows where it lives).

## Good to know

- For a lost box: `anci <env> up`, then `regen`, then `migrate` (for unreplicated dbs), then `deploy`.
- Regen checks the replica and asks you to confirm before touching anything. If the restore fails, the live db is
  left as it was.
- By default you can restore up to 24 hours back, and a restore can miss the last second of writes. Change these
  with `retention` and `sync-interval` under `[db.<name>.replication]`.
- `--force` skips the confirmation; without a terminal, regen needs it.

## Next steps

```
anci <env> status       # the db's size and replication lag after the restore
anci <env> migrate <db> # apply any migrations newer than the restored copy
```
