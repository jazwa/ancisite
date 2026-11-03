# anci on a Mac

The anci CLI runs on macOS, but your apps run on x86_64 Linux.

Your best bet for Mac development is to run the dev env's VM on a Linux machine you own (a homelab, a spare PC,
or a server that supports KVM): `ip = '<that machine>'`. See [hosts.md](hosts.md).

You'll also need to explicitly build for x86_64 Linux. Example for Go: `GOOS=linux GOARCH=amd64 go build`.

## Opening your app in the browser

A VM on a remote machine is on that machine's private network, so its URL in `anci <env> status` won't
open from your Mac. Either give the dev env a `host` (a domain pointing at the machine's public IP), or tunnel the app over ssh.

