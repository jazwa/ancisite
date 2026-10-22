# Modes: full vs. adopt

An env's `mode` says who owns its box. With **`full`** (the default), anci creates and runs the VM. With
**`adopt`**, anci sets up and deploys to a machine you already have.

```toml
# full, on your machine
[env.dev]
ip = '127.0.0.1'
vm = { vcpus = 2, mem = '2gb', disk = '20gb' }

# full, on a remote libvirt host
[env.staging]
ip = '203.0.113.5'          # the host
vm = { vcpus = 2, mem = '2gb', disk = '20gb' }

# adopt: a VPS you rent
[env.prod]
ip = '198.51.100.7'         # the VPS itself
mode = 'adopt'
```

| | `full` | `adopt` |
|---|---|---|
| Machine | a VM anci creates | yours or your provider's |
| OS | Debian 13, pinned by `anci-version` | whatever the box runs |
| `vcpus` / `mem` / `disk` | set in `anci.toml` | set by the provider |
| `up --recreate`, `down` | yes | no |
| Public `host` | the host forwards 80/443 to the VM | Caddy serves directly |

`anci <env> up` depends on the mode. With `full`, it creates or starts the VM and applies its size. With `adopt`, the first
`up` sets up the existing box, and later ones only bring it in line with `anci.toml`.

Everything else works the same: deploy, migrate, reconcile, status, secrets and replication.

## Diagram

**full**

```
 your machine                    hypervisor machine (homelab/your laptop)
 ┌──────────┐                    ┌────────────────────────────────────────────┐
 │          │── sudo/ssh+sudo ──▶│ hypervisor (create, start, stop, resize)   │
 │          │                    │      │                                     │
 │ anci CLI │                    │      ▼                                     │
 │          │                    │  ┌──────────────────────────────────────┐  │
 │          │── ssh ─────────────┼─▶│ anci VM                              │  │
 └──────────┘                    │  │  ├─ your exes                        │  │
 internet ──▶ app.example.com ───┼─▶│─▶├─ caddy :80/:443 (TLS)             │  │
                                 │  │  └─ litestream                       │  │
                                 │  └──────────────────────────────────────┘  │
                                 │  (one VM per env)                          │
                                 └────────────────────────────────────────────┘
```

**adopt**

```
 your machine                    VPS (glibc Linux, x86_64), no hypervisor
 ┌──────────┐                    ┌────────────────────────────────────────────┐
 │          │                    │ first up: checks, then adds your users,    │
 │ anci CLI │── ssh ────────────▶│ the anci account and dirs (no packages)    │
 │          │                    │                                            │
 └──────────┘                    │  ├─ your exes                              │
 internet ──▶ app.example.com ──▶│─▶├─ caddy :80/:443 (TLS)                   │
                                 │  └─ litestream                             │
                                 │                                            │
                                 │  (sshd made key-only; the rest untouched)  │
                                 └────────────────────────────────────────────┘
```

## Full Mode

The machine running the VM needs Linux with libvirt, QEMU and KVM (`/dev/kvm`). Bare metal is best: a dedicated
server, or a homelab. A cloud VM works only if the provider supports nested virtualization.

On a Mac, run anci there but put the VM on a Linux host, or use `adopt`: macOS can't run the VM itself
(see [macos.md](macos.md)). On Windows, run anci in
WSL2.

## Adopt Mode

Adoption is best effort: anci checks the box, adds what it needs, makes ssh key-only, turns off LLMNR, and leaves
everything else alone. The box needs:

- a glibc Linux with systemd (any distro except musl ones like Alpine)
- x86_64 (not ARM)
- passwordless sudo for the login user, or log in as `root`
- ports 80/443 free and open, if you use a `host`

Most VPS images meet these out of the box. Watch for ARM plans, and cloud firewalls that block 80/443.

Only Debian 13 boxes are tested in adopt mode; other distros are untested.

Once the first `up` has set the box up, it pins the box's host key in `envs/<env>/hostkey`; commit it.

## When to use which

- **`full` on your machine** for local dev.
- **`full` on a remote host** for staging, or several envs on one server you control.
- **`adopt`** for a rented VPS.

