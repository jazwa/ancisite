# Secrets

```
anci <env> secrets
```

Opens the env's secrets in `$EDITOR`: an env file with one `KEY=value` per line (`#` for comments). Every exe on
the env gets each line as an environment variable.

```
# envs/prod/secrets.env, as you see it in the editor
STRIPE_KEY=sk_live_…
S3_ACCESS_KEY_ID=…
```

## What changes

- **`envs/<env>/secrets.env` in your repo** is created or updated. Safe to commit.

## What doesn't change

- **The env.** Exes keep the old values until the next `deploy`, `migrate` or `reconcile`, which restarts every exe
  and replication with the new ones.
- **Other envs' secrets.** Each env has its own file.

## Encryption

The file is encrypted with [sops](https://github.com/getsops/sops) for the ssh keys of everyone in the env's
`access-users`. When `access-users` changes, the next `secrets` or `reconcile` re-encrypts it for the new list.
Don't forget to commit again!

## Good to know

- Replication credentials go here, under the names set in `access-key-env` and `secret-key-env`.
- Only `ssh-ed25519` and `ssh-rsa` keys work.
- For a quick local env, a git-ignored `.anci/secrets.<env>.env` with the same lines is used when there's no
  encrypted file.

## Next steps

```
anci <env> reconcile     # send the new values to the env
```
