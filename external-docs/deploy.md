# Deploying

```
anci <env> deploy [<exe>]
```

Deploy puts your latest build of `<exe>` live on the env.

## What changes

- **The specified exe is updated.** The service restarts when its binary, static files, `env-vars` or secrets
  changed. A scheduled job picks up the new binary and files on its next run.
- **The exe's static files are updated.** If you set `static`, the new files are in `$ANCI_STATIC_DIR` once the
  deploy finishes.
- **The env matches `anci.toml`.** Edits to config, secrets, `host` or replication take effect. Exes you removed
  from the file are stopped. Other exes keep running the version they already had.
- **The release is logged.** `anci <env> status` shows who deployed last and when.

## What doesn't change

- **Your data.** Databases aren't touched; run `anci <env> migrate <db>` first if the new code needs a new schema.
- **Other exes' versions.** Only the exe you name gets new bytes (or every exe, when you name none).
- **VM size.** Changes to `vcpus`, `mem` or `disk` take effect on `anci <env> reconcile` or `anci <env> up`, not
  on deploy.

## Rolling back

Deploy the previous build.

## Good to know

- Only one deploy runs on an env at a time.
- Deploy returns once the service has restarted, not once it's serving. Check status with the commands below.

## Next steps

```
anci <env> status             # each exe's version, state and URL, plus who deployed last
anci <env> logs <exe> -f      # follow the app's output
anci <env> info exe <exe>     # where its binary, static files, unit and logs live on the box
```
