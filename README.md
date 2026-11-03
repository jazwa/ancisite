# ancisite

Ancisite is a simple and opinionated Heroku-lite-like web platform. 

**Docs: [external-docs/](external-docs/)** 

## Installing

```
curl -fsSL https://raw.githubusercontent.com/jazwa/ancisite/main/anci-install.sh | sh
```

To uninstall: `rm ~/.local/bin/anci` and `~/.cache/nce`.


## Dev

Install `anci` from your checkout as a command on your PATH, and run the tests:

```
uv tool install --editable .
uv run python -m unittest discover -s tests -t .
```


## Building

```
make release      # -> dist/release/anci-<os>-<arch> + SHA256SUMS: what anci-install.sh downloads
make build        # -> dist/anci (+ dist/anci.sha256): a zipapp, needs Python 3.12+ where you run it
```

## Shipping a release

1. Set `__version__` in `anci/__init__.py` to the new version, e.g. `"1.2.3"`, and commit it.
2. Tag that commit and push the tag:

   ```
   git tag v1.2.3
   git push origin v1.2.3
   ```

The `release` GitHub Actions workflow (`.github/workflows/release.yml`) then runs. It fails if the tag doesn't
match `__version__`. Otherwise it runs `make release` and creates a GitHub release for the tag, with everything in
`dist/release/` attached. `anci-install.sh` installs the latest release from there.

If the workflow fails, commit the fix, then delete the tag and repeat step 2 to tag the new commit:

```
git tag -d v1.2.3
git push origin :refs/tags/v1.2.3
```

## Using anci

Define your users, envs (each with its machine config), and exes in `anci.toml`
(run `anci init`, or see `examples/guestbook/anci.toml`), then:

```
anci dev up               # provisions + boots the box (idempotent)
anci dev deploy demo      # deploy a new version of your app (uses your ssh keys)
curl http://<guest-ip>:8000/
anci dev status           # this env: versions, state, URLs
```

Handy verb: `anci dev logs demo` (your app's log file, `-f` follows).

The **env scopes every command** (`anci <env> <verb>`); `anci init` is the only
top-level one.

`anci dev down` stops the box (disk kept); `anci dev up --recreate` rebuilds it from
scratch.

**Secrets**: `anci <env> secrets` manages a SOPS-encrypted `envs/<env>/secrets.env` of `KEY=value` lines,
safe to commit. `deploy`, `migrate` and `reconcile` send it to the env, and your app reads the values as env vars.
For a quick local env, a git-ignored `.anci/secrets.<env>.env` works too.
