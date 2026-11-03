# Hardening run: findings

The results of running TESTPLAN.md against the dev VM. Findings are grouped by type, most severe first within
each group. Each gives what was done, what happened, and what should happen.

**Severity**
- **Critical**: root for something that shouldn't have it, or a box anci can't recover.
- **High**: a box that needs repair by hand, or apps silently given the wrong data or config.
- **Medium**: anci reports the wrong thing, misses a change, or catches a mistake only on the box.
- **Low**: wording, speed, small inconsistencies.

## Index

| Severity | # | Finding |
|---|---|---|
| Critical, fixed | S1 | A newline in env-vars or schedule adds lines to the unit file: the app ran as root |
| Critical, fixed | S2 | The user name `anci` gives the apps' account passwordless sudo |
| Critical, partly fixed | B1 | An exe named `ssh` takes over the box's ssh, and anci can't remove it |
| Critical | B2 | A sudoers grant with the wrong mode locks anci out, with an unclear error |
| High, fixed | S3 | Keys aren't validated: a newline in a key adds a line, with options, to authorized_keys |
| High | S4 | The secrets file made world-readable or given another owner isn't noticed |
| High, partly fixed | B3 | A long exe name passes the check, then leaves the box stuck |
| High, fixed | W1 | Db names that differ in case or `-`/`_` share an env var: apps switched to an empty db |
| High, fixed | W2 | A deleted static version or static blob is never noticed or repaired |
| High | V1 | Plan reads only the running VM: a bad saved size stays hidden until the VM won't boot |
| High, partly fixed | V2 | Memory can be lowered while the VM runs, with no lower limit: 256mb made the box unreachable |
| High | V3 | A renamed env finds no secrets, and nothing says so |
| High, partly fixed | V4 | A disk smaller than the 3 GiB base image passes the check; `up --recreate` deletes the old VM, then fails |
| High, fixed | V5 | Memory below what the box needs passes the check; a new VM never boots and `up` waits 7 minutes |
| High, fixed | R1 | Deploy says "restarted" for a service that fails at once; status says "stopped" |
| Medium, fixed | S5 | Two users can share a key, and the first one listed wins |
| Medium | W3 | `$`, `\` and tabs in args and env-vars don't reach the app as written |
| Medium, fixed | W4 | Setting `ANCI_PORT` in env-vars never settles |
| Medium | W5 | A changed extension doesn't restart the apps |
| Medium | V6 | Raising vcpus or memory past the max, or lowering vcpus, waits for a VM restart; plan and reconcile repeat it |
| Medium | V7 | A vcpu added while the VM runs stays offline in the guest; plan says no changes |
| Medium | V8 | Renaming an env leaves the old VM running, and no anci command can reach it |
| Medium | V9 | `up` on a VM that's shutting down treats it as running, then waits 7 minutes for a VM that's off |
| Medium, partly fixed | V10 | VM sizes aren't sanity-checked: 999 vcpus and 64 GiB went into the saved config |
| Medium | V11 | Plan fails with an error on a disk shrink |
| Medium | V12 | Memory above the host's passes the check when a VM is created |
| Medium | V13 | A failed filesystem grow is silent |
| Medium | R2 | Plan misses a stopped service and a plain file in an ext dir |
| Medium | R3 | Plan hides an error that stops deploy |
| Medium | R4 | Plan shows `+ exe.x` without the deploy note when x isn't built |
| Medium | C1 | A schedule systemd rejects passes the conf check |
| Medium, fixed | O1 | `reconcile` needed sudo even for a member of the libvirt group |
| Low | B4 | A directory where a unit file should be stops every run |
| Low | W6 | Secrets are parsed with systemd's rules |
| Low | W7 | A renamed db is empty, and plan gives no hint |
| Low | V14 | Plan shows memory partway through a balloon change |
| Low | V15 | Sizes that aren't whole GiB read wrongly |
| Low | V16 | `reconcile` on a stopped VM says "migrate converges a running box" |
| Low | R5 | A line with both reconcile and deploy changes is marked deploy-only |
| Low | R6 | Removing `static` reads "new static files" |
| Low | R7 | A migration error hides sqlite's message |
| Low | R8 | Ctrl-C after the upload says "interrupted", but the box finishes the deploy |
| Low | R9 | Removed timers and services stay in `systemctl --failed` |
| Low | R10 | Unsafe names on the box are ignored with no warning |
| Low | R11 | Labels and values missing from some plan and status lines |
| Low | R12 | "can't reach your box" suggests `anci <env> up`, with `<env>` left in |
| Low | O2 | Many static files are slow |
| Low | O3 | `deploy` takes one exe or none |
| Low | O4 | Db dirs get mode 2750, not 0750 |
| Low | O5 | Empty directories in `static/` aren't shipped |

---

## Security

### S1. A newline in env-vars or schedule adds lines to the unit file: the app ran as root (Critical, fixed)

anci writes these values into systemd unit files without rejecting newlines.
- `env-vars = { INJ = "x\nUser=root\nNoNewPrivileges=false\nProtectSystem=false" }` deployed cleanly, and
  `ps` showed the app running as **root**. A later `User=` line wins.
- `schedule = "*:0/2\nUnit=web.service"` deployed; the job's timer now starts `web.service`.
- An arg with a newline, or an env-var value with a newline and a space, made the unit invalid, so the run
  failed at the restart.

Whoever edits the conf already has sudo on the box, so this isn't a privilege gain for them. But a reviewer reading
`env-vars` doesn't expect a change to the app's user or sandbox, and a pasted value with a newline breaks the app.
**Should**: reject control characters (newline, tab, NUL) in every conf value that goes into a unit file.
Fixed: No control characters are allowed in any conf key or value (`validate.check_text`).

### S2. The user name `anci` gives the apps' account passwordless sudo (Critical, fixed)

`[user.anci]` in `access-users` was applied: the box got `/etc/sudoers.d/anci-anci` with
`anci ALL=(ALL) NOPASSWD:ALL`, and `sudo -u anci sudo -n id` printed `uid=0(root)`. `anci` is the account every
app runs as. The app units set `NoNewPrivileges=true`, so an app process can't use it, but anything else running
as `anci` gets root. Keys went to `/home/anci/.ssh` (its home is `/var/lib/anci`, shell nologin, so no ssh login).
`[user.root]` passes the conf check too (not applied). Taking the user out again removed the grant cleanly.
**Should**: reject `anci`, `root`, and the names of existing system accounts (`caddy`, `daemon`, `nobody`, …) in
the conf check.
Fixed: `root`, `anci`, `caddy` and anci's other names are reserved for every kind of name. On the box, the grant step refuses an existing account below uid 1000.

### S3. Keys aren't validated: a newline in a key adds a line, with options, to authorized_keys (High, fixed)

`ssh-pub-keys = ['not a key']` is accepted and written as-is. A key string containing a newline,
`"<amy key>\ncommand=\"echo injected\" <cy key>"`, became two lines in bo's `authorized_keys`, and
`ssh -i cy bo@box hostname` printed `injected`. Anyone who edits the conf can already add keys, so this is about
mistakes and review: a reviewer sees one key, and the box gets another, with options.
**Should**: accept only one well-formed public key per entry (type, base64, optional comment), with no options
and no newlines.
Fixed: Each entry must be one public key whose base64 decodes to a key of the type it names, so options and extra lines are refused.

### S4. The secrets file made world-readable or given another owner isn't noticed (High)

`/etc/anci/secrets.env` set to mode 644, then owned by zhao, by hand: plan said `✓ no changes` and runs left it.
Every account on the box could read the secrets, and zhao could change them. Only the file's contents are
compared. The same holds for units: `chmod 666` on `web.service` went unnoticed, and systemd logged "marked
world-writable … Proceeding anyway". For sudoers grants, see B2.
**Should**: compare the mode and owner of the files anci writes: at least secrets, sudoers grants, units.

### S5. Two users can share a key, and the first one listed wins (Medium, fixed)

`[user.cy]` holding zhao's key is accepted. anci takes the first user in the file whose key matches yours
(`auth.identify_user`). With `[user.cy]` above `[user.zhao]` and cy not in `access-users`, plan failed with
"you (cy) don't have access to env 'dev'", although zhao has access. With cy in `access-users`, runs would log in
and record releases as cy (inferred from the code, not checked).
**Should**: reject a key held by two users.
Fixed: A key held by two users is refused.

## Box left broken

### B1. An exe named `ssh` takes over the box's ssh, and anci can't remove it (Critical, partly fixed)

`[exe.ssh]` passes the conf check. Its link `/etc/systemd/system/ssh.service` replaces Debian's unit of the same
name. Deploying it printed `✓ applying  … ssh restarted`. Starting it stopped `ssh.socket` (Debian marks the two
as conflicting), so port 22 refused new connections. anci kept working only through its already-open shared
connection, which lasts 10 minutes.

Removing the exe never finished. The delete step runs `systemctl disable --now ssh.service`. Once anci's link is
gone, that name is Debian's real sshd unit again, so the step stopped and disabled the box's ssh and cut anci's own
connection mid-run ("can't reach your box"). Every retry did the same. Recovered by hand, over anci's open
connection: removed the unit and exe dir, then re-enabled `ssh.socket`, which was left disabled along with
`ssh.service` (the box would have had no ssh after a reboot).
**Should**: reject exe names that match a unit already on the box (check `systemctl list-unit-files` before
creating one), or put exes under a prefix (`anci-<exe>.service`). Stop and disable only units whose file is
anci's own.
Partly fixed: `ssh`, `sshd`, `networking` and `systemd-networkd` are reserved names: the units whose loss cuts the
box off. Still open: an exe named like any other unit on the box replaces it, and removing the exe leaves that unit
disabled (`disable` acts on units that aren't anci's). Recoverable by hand over ssh.

### B2. A sudoers grant with the wrong mode locks anci out, with an unclear error (Critical)

`chmod 666 /etc/sudoers.d/anci-zhao` on the box. sudo then refuses everything ("sudo: … is world writable",
"sudo: a password is required"), so every anci verb fails with only:
`error: a remote command failed unexpectedly — try: re-run with --debug`. The real cause is in `--debug`'s
stderr. anci can't repair the file, because the repair needs sudo. The VM has no other way in (no guest agent, no
root password), so only `anci dev up --recreate` recovered it.
**Should**: say "sudo on the box refused: <its message>" when the remote stderr comes from sudo. The mode check
from S4 would catch a wrong mode while sudo still works. A second way in for a VM anci runs (the guest agent)
would allow repair.

### B3. A long exe name passes the check, then leaves the box stuck (High, partly fixed)

An exe with a 250-character name passes the conf check. Its unit name, `<name>.service`, is longer than the
255-byte limit for a file name. `anci dev deploy <name>` failed halfway with `install: cannot stat
'/etc/anci/units/aaa…'`. By then it had rewritten the units of the other exes: api's unit said port 8010 while
api still listened on 8000. Every exe was left owing a restart.

Taking the exe out of the conf didn't recover. The delete step runs `rm -f` on the same too-long paths, which
fails ("cannot remove"), and every run stops there, so the owed restarts never happen. The error's advice, "what
already ran is kept, the rest is picked up", isn't true here. Fixed by hand with
`rm -rf /var/lib/anci/exe/aaa…`; the next deploy then restarted everything correctly. A 250-character db name
also passes the check (not applied).
**Should**: cap exe and db names in the conf check, so that every path built from them fits (e.g. 64
characters). A step that fails on one resource shouldn't stop the restarts that other resources owe.
Partly fixed: Names are capped at 32 characters. Still open: a failed step stops the restarts other resources owe.

### B4. A directory where a unit file should be stops every run (Low)

`/etc/anci/units/web.service` replaced by a directory, by hand: every run failed with "Is a directory". anci
doesn't replace it. Only happens by hand.

## Apps silently get the wrong thing

### W1. Db names that differ in case or `-`/`_` share an env var: apps switched to an empty db (High, fixed)

Exe names are checked for this ("names may not differ only in case or in '.', '_', '-'"); db names aren't.
- Adding `[db.Main]` next to `[db.main]` changed every app's `ANCI_DB_MAIN_PATH` from `main` to `Main`.
  The app silently switched to the new, empty db: `visits: sqlite3 failed: … no such table: visits`. Its sqlite3
  call then created `Main/data.sql`. Plan showed only `+ db.Main` and `~ exe.* unit`.
- `x-y` and `x_y` both give `ANCI_DB_X_Y_PATH`. `x_y` won.
- `v.2` gives `ANCI_DB_V.2_PATH`. systemd logs "Invalid environment assignment, ignoring", so apps get no path for
  that db.
**Should**: apply the exe rule to db names, and build db env var names the way port names are built (anything
not a letter or digit becomes `_`).
Fixed: One name rule for every kind: lowercase letters, digits and `-`, so no two db names share an env var.

### W2. A deleted static version or static blob is never noticed or repaired (High, fixed)

After `rm -rf` of the static version web serves: plan said `✓ no changes`, and deploy did nothing. After
deleting every blob, the build and vendored binaries were restored, but every static file stayed a broken link.
A redeploy can't fix either, because the static id hasn't changed. Only changing a static file does.
**Should**: check that the served static version exists and that its links resolve (the build blob is checked
this way, with `readlink -e`).
Fixed: a static version counts only when its dir exists and every link in it resolves; otherwise the exe's
`static` reads `broken`, plan shows it (with the local build), and the next deploy rebuilds it and ships the
missing blobs.

### W3. `$`, `\` and tabs in args and env-vars don't reach the app as written (Medium)

Checked in `/proc/<pid>/cmdline` and `environ`:
- arg `$HOME` → `/var/lib/anci`, `${HOME}` → `/var/lib/anci`, `$$` → `$`: systemd expands `$` in `ExecStart`
  even inside the quotes anci adds.
- arg `back\slash` → `back lash`: systemd reads `\s` as an escape for a space.
- env-var `"a\tb"` (a tab) → `TAB=a`: values are quoted only when they contain a space, so systemd splits on
  the tab.
- `100%`, `%h`, quotes, `it's`, unicode and empty values all came through correctly.
**Should**: escape `$` as `$$` and `\` as `\\` in args, and quote env values that contain any whitespace.

### W4. Setting `ANCI_PORT` in env-vars never settles (Medium, fixed)

`env-vars = { ANCI_PORT = '9999' }`: the user's value wins, so the app listens on 9999. After the deploy, plan
still shows `~ exe.web  port: 9999 → 8000`, every run restarts web, and status shows `starting` with no URL.
**Should**: reject env-vars named `ANCI_*`, or at least `ANCI_PORT`.
Fixed: env-vars named `ANCI_*` are refused.

### W5. A changed extension doesn't restart the apps (Medium)

Changing `ext/hello.so`'s contents relinked it and restarted nothing. An app that loaded the old file keeps it
until its next restart.
**Should**: restart the services, as a secrets change does, when a db's extensions change.

### W6. Secrets are parsed with systemd's rules (Low)

Quotes are stripped (`C="quoted value"` → `quoted value`), `x\ny` → `xny`, and a line without `=` or starting
with a space is dropped silently. The secrets doc says only "one KEY=value per line".
**Should**: document the rules, or check the file and say which lines systemd will drop or change.

### W7. A renamed db is empty, and plan gives no hint (Low)

Renaming `main` to `main-db`: plan said only `+ db.main-db`. The apps got an empty db; the old data stays under
the old name (renaming back picked it up again).
**Should**: when a db is added in the same edit that one disappears, say the old one's data stays and isn't
carried over.

## VMs and envs

Resizing was run with `reconcile` working without sudo (O1's fix); the `up` and env-rename tests were run by you,
with sudo. The disk grew live, with the guest filesystem, every time.

### V1. Plan reads only the running VM: a bad saved size stays hidden until the VM won't boot (High)

`vcpus = 999`: `reconcile` saved 999 to the VM's config ("takes effect on next restart"). Setting the conf back to
3: plan said `✓ no changes` and `reconcile` did nothing, because both read the running VM, which still had 3.
The saved config kept 999. On the next start, libvirt refused: "more than 255 vCPUs require extended interrupt
mode enabled on the iommu device". anci's own advice (`anci dev up`) starts the same config. Fixed by hand with
`virsh setvcpus … --config`.

The same with memory: after `mem = '64gb'` and back to `2gb`, the saved maximum stayed 64 GiB. anci never lowers
a maximum.
**Should**: read the saved config too (`dumpxml --inactive`), compare both with the conf, and set both.

### V2. Memory can be lowered while the VM runs, with no lower limit: 256mb made the box unreachable (High, partly fixed)

`mem = '256mb'` (the box uses about 300 MiB): `reconcile` shrank the balloon, ssh to the box timed out, and the
run's converge failed with "can't reach your box". The balloon stuck at about 310 MiB. Raising memory with
`virsh setmem 1G` brought it back; nothing was killed. `mem = '1mb'` passes the conf check too (not run).
**Should**: a minimum (e.g. 512 MiB), and refuse a live decrease below what the guest is using.
Partly fixed: the conf check refuses memory below 512 MiB. Still open: a live decrease below what the guest uses.

### V3. A renamed env finds no secrets, and nothing says so (High)

`[env.dev]` renamed to `[env.dev2]`, with `.anci/secrets.dev.env` holding `FOO=bar`. `anci dev2 up`, `migrate`
and `deploy` all succeeded, and plan said `✓ no changes`. The new box has no `/etc/anci/secrets.env`, web's unit
has no `EnvironmentFile`, and web runs without `FOO`. Secrets are found by env name (`envs/<env>/secrets.env`,
`.anci/secrets.<env>.env`), so the file for `dev` was silently ignored.
**Should**: when an env has no secrets but there's a secrets file for an env the conf no longer has, say so in
plan and `up`.

### V4. A disk smaller than the 3 GiB base image passes the check; `up --recreate` deletes the old VM, then fails (High, partly fixed)

`disk = '2gb'`, then `anci dev up --recreate`, confirmed with `y`. anci destroyed and undefined the VM and deleted
its disk, then failed: "creating the box's disk failed on your dev machine — try: re-run with --debug". The base
image (`debian-13-generic-amd64-20261001-2618.qcow2`) is 3 GiB (from its qcow2 header), and `_ensure_disk` runs
`qemu-img resize "$d" 2G`, which won't shrink without `--shrink`. The env was left with no box.

From the code (not checked on the box, since its images folder needs root): `_ensure_disk` runs `cp`, then the
resize, under `set -e`, so the copied 3 GiB disk stays. The next `up` sees the file and skips both
(`[ -f "$d" ] && exit 0`): the VM starts with 3 GiB, and with `disk = '2gb'` still set, the resize step then
fails with "can't shrink", after the VM is created.
**Should**: reject a disk below the base image's size before touching anything (the size is in the image's
header, or the versions map can carry it). In `--recreate`, check everything that can be checked before deleting
the old VM. If creating the disk fails, remove the half-made file. Show the `qemu-img` error, not only "re-run
with --debug".
Partly fixed: The conf check refuses a disk below 4 GiB. Still open: `--recreate` deletes before every check has run, a half-made disk is left, and the `qemu-img` error is hidden.

### V5. Memory below what the box needs passes the check; a new VM never boots and `up` waits 7 minutes (High, fixed)

From the code; not run. The conf check has no minimum, and `up` passes `mem` straight to
`virt-install --memory <MiB>`. A VM created with too little memory can't boot, or fails during its first boot,
and `up` waits the full 420s for ssh before "your box isn't answering after 420s". With `--recreate`, the old VM
is already deleted by then (as in V4). For scale: the box used about 300 MiB idle, and 256mb on a running VM made
it unreachable (V2).
**Should**: a minimum (around 512 MiB) in the conf check, checked before `--recreate` deletes anything.
Fixed: The conf check refuses memory below 512 MiB, before `--recreate` deletes anything.

### V6. Raising vcpus or memory past the max, or lowering vcpus, waits for a VM restart; plan and reconcile repeat it (Medium)

`virt-install` sets the maximum to the starting size. Raising vcpus or memory past it is saved for the next
start only. Lowering vcpus isn't supported live here either ("live change isn't supported here"); raising them
within the maximum is (V7). Until a restart, plan shows `~ vm  vcpus: 2 → 3`, and every `reconcile` prints
"resized … takes effect on next restart" again. anci never restarts the VM. After a restart (by hand, `virsh shutdown` and `start`), plan settled.

`up` doesn't help either. With the VM stopped and `vcpus = 5` (saved maximum 4), `anci dev up` started it with 4,
then raised the saved config to 5. Straight after `up`, plan showed `~ vm  vcpus: 4 → 5`. `up` always starts the
VM first and resizes after: an increase within the saved maximum is added live (and stays offline, V7), and one
past it waits for another restart. The stopped-VM disk branch (`qemu-img resize`) never runs either: `reconcile`
refuses a stopped VM, and `up` starts it first.
**Should**: in `up`, apply the new size to a stopped VM's saved config before starting it. Say once, in plan,
"needs a VM restart (`anci dev down && anci dev up`)", and don't repeat the resize. Or create VMs with room above
the starting size.

### V7. A vcpu added while the VM runs stays offline in the guest; plan says no changes (Medium)

Test 1 of the sudo tests: the VM stopped, `vcpus = 3` in the conf, then `anci dev up`. The VM's saved maximum was
already 3 (from an earlier test), so anci added the third vcpu live. libvirt reports 3, and the kernel logged
"ACPI: CPU2 has been hot-added", but the guest left it offline: `/sys/devices/system/cpu/online` is `0-1` and
`nproc` says 2. plan and `reconcile` compare libvirt's count, so both say `✓ no changes`.
**Should**: bring new vcpus online in the guest (write `1` to `/sys/devices/system/cpu/cpuN/online`, or a udev
rule that does it), and compare the guest's online count.

### V8. Renaming an env leaves the old VM running, and no anci command can reach it (Medium)

After the rename, `anci dev2 up` made a second VM (`anci-example-b68b3b-dev2`, 192.168.122.65, forward group 2)
and said nothing about the first. The first VM kept running with its data (24 visits; the new one started
empty), and its forward ruleset `/etc/anci/fwd/example-b68b3b-dev.nft` stayed loaded. `anci dev plan` and
`anci dev down` fail with "'dev' isn't an env in anci.toml". Even for a current env, `down` only stops the VM:
no anci command deletes a VM, its disk, its cloud-init files or its forward rules.
**Should**: when `up` creates a box, list this project's other anci VMs that no env in the conf names. Add a
verb that removes an env's VM and everything anci made for it on the dev machine.

### V9. `up` on a VM that's shutting down treats it as running, then waits 7 minutes for a VM that's off (Medium)

Test 1, second run: `virsh shutdown`, then `anci dev up` (with `vcpus = 4`). `up` hung on "waiting for the box to
answer ssh" while `virsh domstate --reason` said `shut off (shutdown)`. The VM's log was last written the minute
`up` started. Most likely the guest was still shutting down when `up` looked: it read "running", skipped starting
it, resized the saved config, and then the guest finished shutting down. The wait (`_wait_for_ssh_port`,
`anci/providers/libvirt.py:198`) probes port 22 for up to 420s without checking the VM is still running.
**Should**: in the wait, stop as soon as the VM isn't running, and say so. In `up`, wait for a VM that's shutting
down to finish, then start it.

### V10. VM sizes aren't sanity-checked: 999 vcpus and 64 GiB went into the saved config (Medium, partly fixed)

`vcpus = 999` and `mem = '64gb'` (on an 8-core, 15 GB host) both pass the conf check and were saved by
`reconcile`. See V1 for the result.
**Should**: a warning above the host's cores or memory, and a hard limit at what libvirt can start (255
vcpus without extra setup).
Partly fixed: The conf check refuses more than 255 vcpus. Still open: a warning above the host's cores or memory.

### V11. Plan fails with an error on a disk shrink (Medium)

`disk = '20gb'` on a 22 GiB VM: plan stops with "can't shrink the disk to 20gb" instead of showing it.
`vm_differences` (`anci/providers/libvirt.py:417`) catches errors from reading the VM, not this one.
**Should**: show it as a line, e.g. `~ vm  disk: 22 GiB → 20 GiB (can't shrink: use --recreate)`.

### V12. Memory above the host's passes the check when a VM is created (Medium)

From the code; not run. `_virt_install` turns a "cannot allocate memory" failure into a clear error ("couldn't give
the box its … of memory"). But Linux usually lets qemu reserve more than the host has, so the VM starts, and the
trouble comes when the guest uses the memory: the host swaps or kills processes. `--recreate` deletes the old VM
first here too.
**Should**: for a VM on this machine, warn above the host's memory, before `--recreate` deletes anything.

### V13. A failed filesystem grow is silent (Medium)

From the code; not run (the grow worked every time it ran). The grow step hides its errors
(`growpart … >/dev/null 2>&1 || true`, `anci/inspect/walk.py:39-45`). On a failure, libvirt reports the new size,
plan says nothing, and the guest keeps its old size. It also takes the disk to be the root device minus its last
digit, so `/dev/vda12` gives `/dev/vda1`.
**Should**: let the grow fail loudly, and compare the guest's filesystem size with the disk's.

### V14. Plan shows memory partway through a balloon change (Low)

Just after lowering memory, plan read `~ vm  mem: 2621 MiB → 1024 MiB`, and later `1025 MiB → 2048 MiB`, while
the balloon was still moving. It settled within seconds.

### V15. Sizes that aren't whole GiB read wrongly (Low)

`disk = '25000mb'` on a 24 GiB disk: plan said `disk: 24 GiB → 24 GiB`. Setting it back to `24gb`: "can't shrink
the disk to 24gb — the box's disk is already 24 GiB". The display rounds down.

### V16. `reconcile` on a stopped VM says "migrate converges a running box" (Low)

`_require_running` names `migrate` whatever the verb.

## Reporting and detection

### R1. Deploy says "restarted" for a service that fails at once; status says "stopped" (High, fixed)

- A port already taken: deploy said `✓ applying  web restarted`, while web's log said
  `bind: … Address in use`. systemd kept retrying (`Restart=on-failure`), and web came up once the port was
  free.
- `/bin/false` as an exe: `✓ applying  … crash restarted`, while systemd was restarting it in a loop.
- In both cases `status` says `stopped`, which reads as deliberate.
**Should**: after a restart, wait a moment and check the unit is active; report "web failed to start: <last log
lines>". Status should say "failing" (or "restarting") for a unit that keeps restarting.
Fixed: a run that restarted services waits 3s once, then fails if one isn't active or systemd restarted it on
its own (`NRestarts`, zeroed by `reset-failed` before each restart), with the end of its log. Status says
"failing · restarted N×" or "failed". A service that fails after 3s is only caught by status.

### R2. Plan misses a stopped service and a plain file in an ext dir (Medium)

Made by hand; plan said `✓ no changes` and runs left them.
- `systemctl stop web.v2`: the app stays down. `status` shows it; plan and runs don't start it.
- A plain file (`plain.so`) in `/var/lib/anci/ext/main`: it stays, and apps see it in `ANCI_DB_MAIN_EXT_DIR`.
  The probe lists only links.
**Should**: start a service the conf wants that isn't running; remove anything in an ext dir that isn't a listed
extension.

### R3. Plan hides an error that stops deploy (Medium)

A static file named `new\nline.txt`, or a symlink in `static/`: deploy refuses with a clear error ("it contains a
newline", "it is a symlink"). Plan instead shows `~ exe.web  unit` and leaves out the static change, with no
error.
**Should**: show the same error in plan.

### R4. Plan shows `+ exe.x` without the deploy note when x isn't built (Medium)

With tick's binary missing, plan showed `+ exe.tick` with no `(anci dev deploy)`. `reconcile` won't create it:
only a deploy does, and deploy needs the build.
**Should**: mark it `(anci dev deploy, not built yet)`.

### R5. A line with both reconcile and deploy changes is marked deploy-only (Low)

Adding `static`: plan said `~ exe.web  unit, new static files  (anci dev deploy)`, though `reconcile` applies the
`unit` part. The whole line is compared, not each field.
**Should**: split it into two lines, or mark only the fields that need a deploy.

### R6. Removing `static` reads "new static files" (Low)

Plan said `~ exe.web  unit, new static files` for a removal. The field's wording (`field_words`) is used whatever
the change.
**Should**: say `static files removed` when the target has none.

### R7. A migration error hides sqlite's message (Low)

A migration with `CREATE TABL` failed with "migration 0003_broken failed — its transaction was rolled back". The
reason, `near "TABL": syntax error`, appears only with `--debug`. The rollback itself worked.
**Should**: print sqlite's error line.

### R8. Ctrl-C after the upload says "interrupted", but the box finishes the deploy (Low)

SIGINT at 0.55–0.75s printed `interrupted`, yet the new build went live and the release was recorded. The box
was always consistent, and plan was right afterwards.
**Should**: say "the box finishes this run" once the run has started there.

### R9. Removed timers and services stay in `systemctl --failed` (Low)

After removing the job `tick` (and after switching it to a service), `systemctl --failed` lists
`● tick.timer not-found failed failed`. The journal says "tick.timer: Unit to trigger vanished … Failed with
result 'resources'": the service unit went before the timer was stopped. A removed crash-looping service
(`crash`) stays there too. Anything that watches failed units reports them.
**Should**: stop the timer before its service goes, and `systemctl reset-failed` the units of a removed exe.

### R10. Unsafe names on the box are ignored with no warning (Low)

`bad name.service` in the units dir and `a;touch ${IFS}pwned2` in the exe dir were ignored safely (nothing ran),
but stay there and nothing mentions them.
**Should**: list them in plan as "ignored: not a name anci makes".

### R11. Labels and values missing from some plan and status lines (Low)

Plan says `~ secrets  file` and `~ db.main  replica` with no values, where a label or the values would read
better. `status` shows a crash-looping exe's URL as `—`.

### R12. "can't reach your box" suggests `anci <env> up`, with `<env>` left in (Low)

The hint prints the placeholder instead of the env's name.

## Conf check gaps (caught only on the box)

### C1. A schedule systemd rejects passes the conf check (Medium)

`schedule = 'every tuesday'` failed at `exe.tick/arm` ("bad unit file setting"). The error was clear, and fixing
the schedule recovered.
**Should**: check it in the conf (`systemd-analyze calendar` rules), before anything changes on the box.

## Operations

### O1. `reconcile` needed sudo even for a member of the libvirt group (Medium, fixed)

`anci dev reconcile` ran `sudo -v` up front and failed without a password, although `plan` reads the same VM
without sudo. Fixed: `vm_reconcile` now uses `libvirt_hypervisor` (`anci/hypervisor.py`), which runs virsh as you
when libvirt lets you, and asks for sudo otherwise. A running VM is resized with virsh alone. Tests added in
`tests/test_libvirt.py`; all 609 pass.

### O2. Many static files are slow (Low)

With 5000 static files: the first deploy took 37s (35.6s applying), every plan about 10s afterwards, and a deploy
changing one file 18.6s. These look like one process per file, both when reading the box and when rebuilding
the link tree.

### O3. `deploy` takes one exe or none (Low)

`anci dev deploy api tick` fails with "Got unexpected extra arguments".

### O4. Db dirs get mode 2750, not 0750 (Low)

The setgid bit is inherited from `/var/lib/anci/db`. The docs say 0750.

### O5. Empty directories in `static/` aren't shipped (Low)

---

## Not run yet

Everything has been run or recorded. Findings marked "from the code; not run" were taken as true without
running them. `mem = '1mb'` on a running VM was skipped: V2 shows the same failure at 256mb.

## Predictions that were wrong

- Two envs on the same address: expected `up` to refuse late, with "already claimed". The conf check refuses it
  first: `[env.e0] and [env.dev] are the same box (192.168.122.74) — one env is one box, so they can't share an
  address`. It doesn't say what to do (rename one). A clash with another project's box goes through the later
  check, not tested.
- `vcpus = 999`: expected libvirt to refuse it. It saved it, and the VM then wouldn't start (V1).
- D, for exes: `web-1` next to `web_1` is rejected ("its port variable ANCI_PORT_WEB_1 is also [exe.web-1]'s").
  It's true for dbs (W1).
- Exe names that look like unit names work: `tick.timer`, `x.service` and `Web` all deployed and were removed
  cleanly.

## Checked and working

With sudo: a stopped VM restarted by `up` comes up with its saved size; a vcpu or memory increase takes effect
after a restart and plan then settles; a new env (`dev2`) came up, migrated and deployed cleanly; two envs on one
address are refused by the conf check.

Phase 17 (on the recreated VM): every change in one run and back again, both correct; interrupts at 7
moments (always consistent); two deploys and a migrate at once (the lock serialized them); a full disk (clear
error, clean recovery); odd static file names (`-rf`, `.static-tmp`, `$(touch pwned).txt`, quotes, unicode:
shipped safely, nothing executed); unsafe names and symlink traps planted on the box (ignored, or only the
link removed; the canary survived); zhao taken out of the anci group (detected, restored).

Phases 1–14: users (add, second key, rename, rejected names, several at once, swap keys, own key, access
mismatches); env-vars and args; static add, change, remove and gc; a new build (migrate doesn't ship it, deploy
does); a new db, new migrations; a scheduled job (runs every 2 min); a second service shifting ports; job ↔
service both ways; renaming an exe and a db and back; extensions add, change, rename, remove; secrets add,
change, remove (services restart, the job's timer doesn't); replication on, change, off (litestream stays up and
status shows "litestream error" for the missing bucket); removing exes; and hand edits: boot link, unit edit,
restart marker, stray unit, stray exe dir, stray group member, stray sudoers grant, sshd and resolved drop-ins,
logtrim script and timer, a deleted vendored binary, a stray ext link, a deleted build blob. Garbage collection
removed every unreferenced blob after 5 minutes.
