# Environment variables

**Global** values are the same for every exe on the env; **by exe** values
differ per exe, and only that exe sees them.

| Variable | Scope | Who gets it | Value |
|---|---|---|---|
| `ANCI_ENV` | global | every exe | the env name |
| `ANCI_BIND` | global | services | `0.0.0.0` on a local env, else `127.0.0.1` |
| `ANCI_PORT` | by exe | services | its own port |
| `ANCI_URL` | by exe | services | `http://<project>.localhost:<port>` on a local env, `https://<host>`, or `http://127.0.0.1:<port>` |
| `ANCI_PORT_<EXE>` | global | every exe | each service's port on the box (`my-api` → `ANCI_PORT_MY_API`) |
| `ANCI_DB_<DB>_PATH` | global | every exe | each db's file (`main-db` → `ANCI_DB_MAIN_DB_PATH`) |
| `ANCI_DB_<DB>_EXT_DIR` | global | every exe | each db's extensions directory, when `extensions` is set |
| `ANCI_STATIC_DIR` | by exe | exes with `static` | the exe's static files, read-only |
| `env-vars` | by exe | that exe | your values; per-env keys override the exe's base ones |
| secrets | global | every exe | each `KEY=value` line from `anci <env> secrets` |

## Overrides

Your `env-vars` override every `ANCI_*` variable, and secrets override both.
