# Migrating

```
anci <env> migrate [<db>]
anci <env> migrate [<db>] --baseline
```

Migrate applies your db's pending migrations, in filename order. Leave out `<db>` to migrate every db in
`anci.toml`, one after another in name order. If one fails, the dbs before it keep their migrations and the rest
aren't touched.

## What changes

- **The db's schema is updated.** Each pending migration runs in its own transaction and is recorded as applied.
  The first migrate on an env creates the db.
- **Extensions are updated.** Files listed in `extensions` are uploaded and loaded for the migrations.
- **The env matches `anci.toml`.** Same as deploy: config, secrets, `host` and replication take effect.
  Replication starts once the db exists.

## What doesn't change

- **Your apps' versions.** Apps keep running while migrations run, on the version they have. They restart only if
  their config or secrets changed (see above).
- **Applied migrations.** Migrate refuses to run if an applied migration was edited or deleted (use `--baseline` to
  accept the change).
- **VM size.** Changes to `vcpus`, `mem` or `disk` take effect on `anci <env> reconcile` or `anci <env> up`, not
  on migrate.

## When a migration fails

That migration is rolled back. Migrations before it in the same run stay applied, and later ones don't run.
The env isn't updated until a full migrate succeeds.

## Baselining

`--baseline` accepts the files as they are now: it updates the record of applied migrations to match them, without
running them again. Edited migrations are recorded with their current contents, and migrations whose files are gone
are forgotten.

## Good to know

- Don't put `BEGIN`/`COMMIT` in migration files; anci wraps each one in a transaction.
- anci puts every db in WAL mode (litestream needs it) before running migrations, so don't set `journal_mode`
  in a migration: inside its transaction it can't change, and the migration fails.
- Write migrations that the running app tolerates; it isn't stopped.
- Only one migrate or deploy runs on an env at a time; a second one waits for the first.

## Sqlite Defaults

- `journal_mode=WAL`
- `auto_vacuum=INCREMENTAL` (new dbs)
- foreign keys checked after migrations are applied

## Next steps

```
anci <env> status                # each db's last migration, size and replication lag
anci <env> info db <db>          # where the db file, extensions and replication config live on the box
anci <env> local-schema <db>     # a schema-only copy of the db on your machine
```
