# Where your VMs run

Every env is a VM that anci creates and runs. Its `ip` says which machine the VM runs on:

```toml
# on your machine
[env.dev]
ip = '127.0.0.1'
vm = { vcpus = 2, mem = '2gb', disk = '20gb' }

# on a remote libvirt host
[env.prod]
ip = '203.0.113.5'          # the host
vm = { vcpus = 2, mem = '2gb', disk = '20gb' }
```

Either way the VM runs Debian 13, pinned by `anci-version`, and is sized by `vcpus`, `mem` and `disk`.
`anci <env> up` creates or starts it and applies its size.

## Diagram

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

## What the host needs

The machine running the VM needs Linux with libvirt, QEMU and KVM (`/dev/kvm`). Bare metal is best: a dedicated
server, or a homelab. A cloud VM works only if the provider supports nested virtualization.

On a Mac, run anci there but put the VM on a Linux host: macOS can't run the VM itself (see
[macos.md](macos.md)). On Windows, run anci in WSL2.

## When to use which

- **Your machine** for local dev.
- **A remote host** for staging and production, or several envs on one server you control.
