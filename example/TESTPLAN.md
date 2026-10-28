# Test plan: anci.toml → plan → reconcile → check the box

Each test changes `anci.toml` (or the box), checks that `plan` predicts the change, applies it, then checks the box
has what `anci.toml` says and nothing else.

## Setup

```sh
cd example
cargo build --release --target x86_64-unknown-linux-musl     # builds web and tick
IP=192.168.122.74                                            # the address `reconcile` prints after "dev →"
box() { ssh zhao@$IP "$@"; }
inv() { ssh zhao@$IP 'sudo bash -s' < boxcheck.sh; }         # everything anci manages on the box
```

`inv` prints users, units, links, exe dirs, ports, dbs, extensions, secrets, replication, vendored binaries,
drop-ins, unreferenced blobs and the VM's size. Most checks below are "look at this part of `inv`".

## The loop for every test

1. Make the edit.
2. `anci dev plan`: the lines should match **Plan** (the order may differ).
3. Run **Apply**.
4. `inv`: it should match **Box**. Running services that weren't meant to restart should keep their start time.
5. `anci dev plan` should print `✓ no changes`, except for lines marked `(anci dev deploy)` before a deploy.
6. Run `anci dev reconcile` again. It should change nothing and restart nothing.

Some removals are left until garbage collection: blobs and static versions nothing uses. They go once they're 5
minutes old, on the next run. Where a test says **wait 5 min**, run `anci dev reconcile` after waiting and check
`inv` → "store" shows `0 unreferenced`.

## Where I expect failures

These come from reading the code. The tests below check each one.

| # | What | Test |
|---|---|---|
| A | A line with both a reconcile change and a deploy change (`unit, new static files`) is marked `(anci dev deploy)` as a whole, though `reconcile` applies the `unit` part | 3a |
| B | Removing `static` reads `new static files` | 3c |
| C | An exe whose binary isn't built shows `+ exe.x` without `(anci dev deploy)`, but `reconcile` won't create it | 5c |
| D | Names that become the same env var aren't rejected: exes `web-1`/`web_1` (`ANCI_PORT_WEB_1`), dbs `x-y`/`x_y` (`ANCI_DB_X_Y_PATH`) | 9 |
| E | A db name with a dot gives an env var with a dot (`ANCI_DB_V.2_PATH`) | 9 |
| F | A stopped service, or a plain file (not a link) in an ext dir, doesn't show in `plan` | 14 |
| G | An exe named after a system unit (`ssh`) isn't rejected | 16 |
| H | `plan` fails with an error on a disk shrink, instead of showing it | 15 |
| I | Two users with the same key are accepted | 1h |
| J | A static version, or its blobs, deleted on the box doesn't show in `plan` | 17 |
| K | A newline in an env-var value breaks the unit file | 17 |
| L | `deploy` says it succeeded for a service that dies at once, or can't get its port | 17 |

---

## 0. Baseline

No edit. **Plan**: `✓ no changes`. If it shows `~ exe.web  new build`, the rebuild with `tick` changed the web
binary: `anci dev deploy web` and continue.

**Box**: user `zhao` only. `web.service` running on port 8000. db `main` with `0001_create_visits`. No secrets,
no litestream, no caddy. `inv` → "disk" shows about 22G.

Also run `virsh -c qemu:///system domblkinfo anci-example-b68b3b-dev vda --human`. Capacity should be 22 GiB,
not TiB.

## 1. Users

First make a second key: `ssh-keygen -t ed25519 -f ~/.ssh/anci-jack -N ''`.

**1a. Add a user.**
```toml
[user.jack]
ssh-pub-keys = '<contents of ~/.ssh/anci-jack.pub>'
```
and `access-users = ['zhao', 'jack']`.
- **Plan**: `+ user.jack`.
- **Apply**: `anci dev reconcile`.
- **Box**: jack is in the anci group and has a sudoers grant with mode 440 and 1 key.
  `ssh -i ~/.ssh/anci-jack -o IdentitiesOnly=yes jack@$IP sudo -n true` works.

**1b. Give jack a second key.** Make it a list holding the anci-jack key plus your zhao key.
- **Plan**: `~ user.jack  keys`.
- **Box**: jack has 2 keys.

**1c. Rename jack to `j_4ck-`.** Rename the table and the `access-users` entry.
- **Plan**: `- user.jack`, `+ user.j_4ck-`.
- **Box**: `j_4ck-` has the group, sudoers and keys. jack has none of them, but the account remains
  (`box id jack`). Logging in as jack fails.

**1d. Rejected names.** Try each of `[user.Jack]` and `[user.9jack]` in turn.
- **Plan**: a conf error naming the user. Nothing reaches the box.

**1e. Remove access.** Take `j_4ck-` out of `access-users`, but keep the `[user.j_4ck-]` table.
- **Plan**: `- user.j_4ck-`.
- **Box**: only zhao is in the group, has a sudoers grant and has keys.

**1f. Several at once.** Add `[user.amy]`, `[user.bo]` and `[user.cy]` (make keys the same way) and put all of
them in `access-users`. In the same edit, give `j_4ck-` access again with a different key.
- **Plan**: `+ user.amy`, `+ user.bo`, `+ user.cy`, `+ user.j_4ck-`.
- **Box**: 5 users with grants, each holding only their own key.

**1g. Swap keys between two users.** Give amy bo's key and bo amy's.
- **Plan**: `~ user.amy  keys`, `~ user.bo  keys`.
- **Box**: `ssh -i ~/.ssh/anci-amy amy@$IP` now fails, and `ssh -i ~/.ssh/anci-amy bo@$IP` works.

**1h. Two users share one key.** Give cy amy's key too. Expected: a conf error, because a key should identify
one user. I think it's accepted today: note what happens, and who anci thinks you are when you log in with that
key.

**1i. Your own key.**
- Add a second key to `zhao`. **Plan**: `~ user.zhao  keys`.
- Remove zhao's original key, the one in your agent. Expected: anci refuses before touching the box, because
  you'd lock yourself out. Note the message.
- Remove `zhao` from `access-users`. Same expectation.

**1j. Mismatches.**
- Put a name in `access-users` that has no `[user.x]` table. **Plan**: a conf error.
- Add a `[user.dee]` table that isn't in `access-users`. **Plan**: `✓ no changes`.

**1k. Put them all back.** Leave only zhao in `access-users`.
- **Plan**: one `-` line per user.
- **Box**: only zhao is in the group, has a grant and has keys. The other accounts still exist with no keys.

## 2. Exe settings

**2a.** Add `env-vars = { GREETING = 'hi' }` to `[exe.web]`.
- **Plan**: `~ exe.web  unit`.
- **Box**: web restarted (new start time).
  `box sudo systemctl show web -p Environment | tr ' ' '\n' | grep GREETING` prints the variable.

**2b.** Add `args = ['--loud', 'two words']`.
- **Plan**: `~ exe.web  unit`.
- **Box**: `box systemctl show web -p ExecStart` lists both args, with `two words` kept as one.

**2c.** Remove both settings.
- **Plan**: `~ exe.web  unit`.
- **Box**: neither setting is in the unit.

## 3. Static files

**3a. Add.** Add `static = './static'` to `[exe.web]`.
- **Plan**: `~ exe.web  unit, new static files  (anci dev deploy)`. Failure A: `reconcile` would apply the
  unit part, but the line says only deploy does.
- **Apply**: `anci dev reconcile`, then `anci dev deploy web`.
- **Box**: web shows `static=static-<id>  versions=1`.
  `box ls -lR /var/lib/anci/exe/web/static/` shows `index.html` and `css/site.css` as links into the blob store.
  `ANCI_STATIC_DIR` is in web's Environment.

**3b. Change.** Run `echo '<h1>hello v2</h1>' > static/index.html`.
- **Plan**: `~ exe.web  new static files  (anci dev deploy)`.
- **Apply**: `anci dev deploy web`.
- **Box**: `versions=2`, and 1 unreferenced blob (the old index.html). The unchanged css blob is shared.
  **Wait 5 min**: `versions=1`, `0 unreferenced`.

**3c. Remove.** Remove `static`.
- **Plan**: `~ exe.web  unit, …`. Failure B: it says `new static files` for a removal.
- **Box**: `static=-`, and `ANCI_STATIC_DIR` is gone from the unit. **Wait 5 min**: `versions=0`,
  `0 unreferenced`.

## 4. A new build

In `src/main.rs`, change `hello from anci` to `hello again from anci`, then build.
- **Plan**: `~ exe.web  new build  (anci dev deploy)`.
- **Apply**: `anci dev reconcile` first. It should change nothing for web. Then `anci dev deploy web`.
- **Box**: web's `current=` is a new blob, and `releases/web` has one more line. `curl -s $IP:8000` says
  "hello again". 1 unreferenced blob; **wait 5 min**: 0.

## 5. A new db, a new migration, a scheduled job

**5a. New db.**
```toml
[db.jobs]
migrations = './migrations-jobs/'
```
- **Plan**: `+ db.jobs`, `~ exe.web  unit`.
- **Apply**: `anci dev reconcile`.
- **Box**: `jobs anci:anci 750 data=no`.
- **Apply**: `anci dev migrate`.
- **Box**: `jobs data=yes`, tables include `ticks`, migrations `0001_create_ticks`.

**5b. New migration on an existing db.**
`printf 'ALTER TABLE visits ADD COLUMN agent TEXT;\n' > migrations/0002_add_agent.sql`
- **Plan**: `✓ no changes`, because plan doesn't cover migrations.
- **Apply**: `anci dev migrate`.
- **Box**: main's migrations list `0001_create_visits 0002_add_agent`.
  `box sudo sqlite3 /var/lib/anci/db/main/data.sql .schema` shows `agent`. Visit rows from before are still there.

**5c. A scheduled job.**
```toml
[exe.tick]
source = './target/x86_64-unknown-linux-musl/release/tick'
schedule = '*:0/2'
```
- **Plan**: `+ exe.tick  (anci dev deploy)`. Web is unchanged: a job takes no port.
- **Apply**: `anci dev reconcile`. It should not create tick. Then `anci dev deploy tick`.
- **Box**: `tick.service` and `tick.timer` are in the units, both linked. `timers.target.wants/tick.timer` is
  present. tick has no port. web was not restarted. After 2 minutes, `box sudo tail /var/log/anci/tick.log`
  shows `ticks: 1`, then 2, and so on.
- **Failure C**: run `cargo clean -p web --release --target x86_64-unknown-linux-musl` before planning 5c
  (this deletes both binaries). Plan should then still say tick needs a deploy. Rebuild afterwards.

## 6. A second service: ports move

Use the web binary under a name that sorts first:
```toml
[exe.api]
source = './target/x86_64-unknown-linux-musl/release/web'
```
- **Plan**: `+ exe.api  (anci dev deploy)`, `~ exe.web  unit, port: 8000 → 8010`, `~ exe.tick  unit`.
- **Apply**: `anci dev reconcile`.
- **Box**: web listens on 8010 (`curl -s $IP:8010`). api isn't on the box yet.
- **Apply**: `anci dev deploy api`.
- **Box**: api is on 8000. api's `current=` is the same blob as web's, so nothing new was uploaded. Both pages
  count visits in the same db.

## 7. Job ↔ service

**7a.** Remove `schedule` from tick. As a service it answers HTTP with the tick count.
- **Plan**: `~ exe.tick  kind: job → service, …, port: none → 8010`, `~ exe.web  unit, port: 8010 → 8020`,
  `~ exe.api  unit`.
- **Box**: `tick.timer` is gone from the units, links and `timers.target.wants`.
  `multi-user.target.wants/tick.service` is present. Ports: api 8000, tick 8010, web 8020.
  `curl -s $IP:8010` → `ticks: N`.

**7b.** Put `schedule` back.
- **Plan**: the reverse.
- **Box**: the same as after 5c and 6. No `multi-user.target.wants/tick.service` is left behind.

## 8. Renames

**8a. Rename exe `web` to `web.v2`.**
- **Plan**: `- exe.web`, `+ exe.web.v2  (anci dev deploy)`, plus unit and port lines for api and tick
  (`ANCI_PORT_WEB` becomes `ANCI_PORT_WEB_V2`).
- **Apply**: `anci dev reconcile`. It prints "removed web; kept its log and releases". Then
  `anci dev deploy web.v2`.
- **Box**: nothing named `web.service` is in the units, systemd links or wants. `/var/lib/anci/exe/web` is gone.
  `web.log` and `releases/web` remain. `web.v2.service` is running.

**8b. Rename db `main` to `main-db`.**
- **Plan**: `+ db.main-db`, unit lines for each exe. No line for `main`: its data stays.
- **Apply**: `anci dev reconcile`, then `anci dev migrate`.
- **Box**: `main` still has `data=yes`. `main-db` is new, with an empty `visits` table. `curl` shows "no db": the
  app reads `ANCI_DB_MAIN_PATH`, which is now `ANCI_DB_MAIN_DB_PATH`.

**8c. Rename it back to `main`.**
- **Plan**: unit lines only.
- **Box**: `curl` shows the old visit count, continuing. `main-db` stays on the box and isn't listed in plan.

## 9. Weird names

Add each group, plan, apply (deploy any new exe), check `inv`, then remove it again. Removing should leave only
the log and release history of an exe, and the data of a db.

| Add | What to look for |
|---|---|
| exes `web-1` and `web_1`, both with the web binary | Failure D. Expected: a conf error. Otherwise, check which port `ANCI_PORT_WEB_1` holds in `api.service`: `box grep ANCI_PORT_WEB_1 /etc/anci/units/api.service` |
| dbs `x-y` and `x_y` | Failure D, with `ANCI_DB_X_Y_PATH` |
| db `v.2` | Failure E. `box sudo systemctl show api -p Environment`, and `box sudo journalctl -u api -n 20` for "Invalid environment assignment" |
| exe `Web` next to `web.v2` | both run. `ANCI_PORT_WEB` belongs to `Web` |
| exe `9lives`, db `0` | accepted. The env vars are `ANCI_PORT_9LIVES` and `ANCI_DB_0_PATH` |
| exe `caddy`, exe `anci-logtrim` | a conf error (reserved names) |
| exe `.hidden`, exe `a/b` | a conf error |
| exe with 11 serving exes in total | a conf error (`too many exes`) |

## 10. Extensions

**10a.** Add `extensions = ['./ext/hello.so']` to `[db.main]`.
- **Plan**: `~ db.main  exts`, plus a unit line for each exe (`ANCI_DB_MAIN_EXT_DIR`).
- **Box**: `main/hello.so -> /var/lib/anci/store/blobs/<id>`.

**10b.** `echo v2 >> ext/hello.so`.
- **Plan**: `~ db.main  exts`.
- **Box**: hello.so points to a new blob, and 1 unreferenced blob is left. **Wait 5 min**: 0.

**10c.** `mv ext/hello.so ext/hello2.so` and update the conf.
- **Box**: only `main/hello2.so` is listed.

**10d.** Remove `extensions`.
- **Plan**: `~ db.main  exts`, plus the unit lines.
- **Box**: nothing is under `dbs: extensions`, and the `EXT_DIR` variable is gone from the units.

## 11. Secrets

**11a.** `mkdir -p .anci && printf 'FOO=bar\n' > .anci/secrets.dev.env`
- **Plan**: `+ secrets`, plus a unit line for each exe (`EnvironmentFile`).
- **Box**: `root:root 600 1 vars`.
  `box 'sudo cat /proc/$(systemctl show -p MainPID --value api)/environ' | tr '\0' '\n' | grep FOO`
  prints `FOO=bar`.

**11b.** Change the value to `FOO=baz`.
- **Plan**: `~ secrets`.
- **Box**: every service restarted and has `FOO=baz`. tick's timer wasn't touched.

**11c.** Delete the file.
- **Plan**: `- secrets`, plus the unit lines.
- **Box**: secrets show `none`, and `EnvironmentFile` is gone from the units.

## 12. Replication

There's no bucket, so litestream can't upload. This test checks the files anci writes. I haven't checked whether
litestream stays running when it can't reach the bucket: note what you see.
```toml
[db.main.replication]
bucket = 'anci-test'
path = 'dev/main'
endpoint = 'http://127.0.0.1:9000'
```
- **12a. Plan**: `+ litestream`, `~ db.main  replica: none → http://127.0.0.1:9000 anci-test dev/main`.
  **Box**: `/etc/anci/litestream.yaml` exists, `litestream.service` is linked and in
  `multi-user.target.wants`, and `main replica=[http://127.0.0.1:9000 anci-test dev/main]`.
- **12b.** Change `path` to `dev/main2`. **Plan**: `~ db.main  replica: …`. **Box**: the record shows `dev/main2`.
  The old `litestream-state` dir was deleted (it may come back if litestream started again).
- **12c.** Remove the table. **Plan**: `- litestream`, `~ db.main  replica: … → none`. **Box**: no yaml, no
  litestream links or wants, `replica=[-]`, no state dir, litestream inactive. `/usr/local/bin/litestream`
  stays: it's on every box.

## 13. Removals

**13a.** Remove `[exe.api]`.
- **Plan**: `- exe.api`, plus unit and port lines for the rest.
- **Box**: `api` is in no unit, link, wants or exe dir. `api.log` and `releases/api` remain. Ports moved back.

**13b.** Remove `[exe.tick]` and `[db.jobs]` together.
- **Plan**: `- exe.tick`, a unit line for each exe. No line for `jobs`.
- **Box**: no `tick.service`, `tick.timer` or `timers.target.wants/tick.timer`. `jobs` still has `data=yes`.

## 14. Edits made on the box by hand

For each row: make the edit, run plan, reconcile, check `inv`, and confirm plan is back to `✓ no changes`.
`$E` is the current web exe (`web.v2`).

| Edit on the box | Plan |
|---|---|
| `box sudo rm /etc/systemd/system/multi-user.target.wants/$E.service` | `~ exe.$E  boot` |
| `box "echo '# x' \| sudo tee -a /etc/anci/units/$E.service"` | `~ exe.$E  unit` |
| `box sudo touch /var/lib/anci/exe/$E/.restart-pending` | `~ exe.$E  not restarted since its last change` |
| `box sudo cp /etc/anci/units/$E.service /etc/anci/units/ghost.service` | `- exe.ghost` |
| `box sudo mkdir /var/lib/anci/exe/ghost2` | `- exe.ghost2` |
| `box 'sudo useradd -m eve && sudo usermod -aG anci eve'` | `- user.eve`. The account stays, not in the group |
| `box "echo 'bob ALL=(ALL) NOPASSWD:ALL' \| sudo tee /etc/sudoers.d/anci-bob"` | `- user.bob` |
| `box "echo '# x' \| sudo tee -a /etc/ssh/sshd_config.d/00-anci.conf"` | `~ sshd  dropin` |
| `box sudo rm /etc/systemd/resolved.conf.d/anci.conf` | `+ resolved` |
| `box "echo '# x' \| sudo tee -a /etc/anci/logtrim.sh"` | `~ logtrim  script` |
| `box sudo systemctl disable anci-logtrim.timer` | `~ logtrim  boot` (maybe also `timer_link`) |
| `box sudo rm /usr/local/bin/sqlite3` | `~ vendored  sqlite3` |
| `box sudo ln -s /tmp /var/lib/anci/ext/main/stray` | `~ db.main  exts`. Reconcile removes the link |
| `box 'sudo rm $(readlink -e /var/lib/anci/exe/'$E'/current)'` | `~ exe.$E  new build  (anci dev deploy)`. Reconcile doesn't fix it; `anci dev deploy $E` does |
| `box sudo chmod 644 /etc/anci/secrets.env` (with secrets on) | unknown: tell me what it shows |
| `box sudo touch /var/lib/anci/ext/main/plain.so` | Failure F: nothing. It stays |
| `box sudo systemctl stop $E` | Failure F: nothing. It stays stopped |

## 15. VM size

**15a.** Set `vm = { …, vcpus = 3, mem = '3gb', disk = '24gb' }`.
- **Plan**: `~ vm  vcpus: 2 → 3, mem: 2048 MiB → 3072 MiB, disk: 22 GiB → 24 GiB`.
- **Apply**: `anci dev reconcile`. It may say a max was raised and takes effect on the next restart.
- **Box**: `inv` → "disk": `/` is about 24G. `domblkinfo` says 24 GiB. If vcpus or memory didn't change live,
  run `virsh -c qemu:///system reboot anci-example-b68b3b-dev`, then check again.

**15b.** Set `disk = '20gb'`.
- **Plan**: Failure H, an error ("can't shrink the disk"). The better result is a line saying it can't shrink.
- **Apply**: `anci dev reconcile` stops with the same error. Nothing changes.

## 16. Risky: last, and only on this throwaway VM

**16a. An exe named `ssh`** (web binary). Failure G: its `ssh.service` link replaces Debian's ssh service, which
can lock you out. If it does, use `virsh -c qemu:///system console anci-example-b68b3b-dev`, or
`anci dev up --recreate`.

**16b. Rename the env `dev` to `dev2`.** Expected: `anci dev2 up` makes a second VM. The old one stays:
`virsh -c qemu:///system list --all`. Note whether anything tells you about it.

## 17. Trying to break it

Each test should end with `plan` predicting what `reconcile` does, and with the box matching `inv` afterwards.
Anything else is a bug: note the command and output.

**Everything at once**
- From the baseline, make every edit from 1–13 in one go: users, settings, static, both dbs, tick, api,
  extensions, secrets, replication. Run one plan, then one `anci dev reconcile`, then
  `anci dev deploy`, then `anci dev migrate`. `inv` should match the end of 13a with all of them on.
- Then go back to the baseline in one edit and one reconcile. The ordering of steps across resources gets tested
  here: a removed exe's units go before the daemon reload, secrets are removed only after restarts, and
  litestream stops before its replica record resets.

**Interrupted and concurrent runs**
- Start `anci dev deploy web` after a code change, and Ctrl-C as soon as "applying" appears. Then run plan. It
  should show exactly what's left, for example `not restarted since its last change`. Run reconcile and check.
  Repeat with Ctrl-C at different moments.
- Run `anci dev reconcile` in two terminals at the same moment. Expected: the second waits for the box's lock, or
  says it's busy. It must not run both at once.
- Run `anci dev plan`, make a hand edit from 14, then run `anci dev reconcile`. It should act on the box as it is
  now, not as it was during plan.

**Steps that fail halfway**
- `schedule = 'every tuesday'` on tick. Systemd will reject the timer. Note where the run stops, what it says,
  and whether plan and reconcile afterwards fix it once you put a valid schedule back.
- A migration with a syntax error, in a run that also has other changes. Check what applied and what didn't.
- Fill the disk: `box sudo fallocate -l <free space minus 100M> /big`, then deploy a new build. Note the error,
  remove `/big`, then plan. It should show the deploy still to do and nothing half-done.
- A port taken by something else: `box 'sudo nohup python3 -m http.server 8000 >/dev/null 2>&1 &'` before api
  takes 8000. What do deploy and `anci dev status` say? anci doesn't check a service after starting it, so I
  expect deploy to say it succeeded.
- A binary that exits at once: `cp /bin/false ./false` and use it as an exe's `source`. Same question.

**Values that need escaping.** Each should reach the app exactly as written. Check with
`box 'sudo cat /proc/$(systemctl show -p MainPID --value web)/environ' | tr '\0' '\n'` and
`box systemctl show web -p ExecStart`.
- env-vars values: `'100%'`, `'%h'`, `'$HOME'`, `'${HOME}'`, `'a "quoted" b'`, `"it's"`, `'back\slash'`,
  `'ünïcødé'`, `''`, and a newline (`"line1\nline2"` in TOML). I expect the newline to break the unit file.
- args: the same list, plus `'--x=$HOME'` and `';'`. systemd may expand `$HOME` in args even inside quotes.
- An env-var named `ANCI_PORT = '9999'`. The user's value wins over anci's. Which port does plan think web is on,
  and which does it listen on?
- Secrets with the same tricky values, plus `KEY=` (empty) and a line with no `=`.

**Static file trees**
- File names with spaces, unicode, a leading dash (`-rf`), and a file named `.static-tmp`; an empty subdir; a
  symlink pointing outside `static/`; 5000 small files.
- Delete the static version web serves, on the box: `box sudo rm -rf /var/lib/anci/exe/web/static-<id>`.
  The static probe reads the link without checking that its target exists, so I expect plan to show nothing
  while the app serves no files.

**The box's own state**
- Delete every blob: `box sudo rm -rf /var/lib/anci/store/blobs/*`. Expected plan: `~ vendored  caddy,
  litestream, sqlite3`-style lines, `~ db.main  exts` with extensions on, and `new build  (anci dev deploy)` for
  each exe. Static files (if on) are broken too; I expect that part to go unnoticed.
- Remove the anci group: `box sudo groupdel anci`. Expected: plan shows `~ user.zhao  group: …`, and reconcile
  recreates the group.
- Names that aren't safe: `box "sudo touch '/etc/anci/units/bad name.service'"` and
  `box "sudo mkdir '/var/lib/anci/exe/\$(touch /tmp/pwned)'"`. Plan and reconcile must ignore both, without
  failing, and `/tmp/pwned` must not appear.
- Links meant to fool deletion. Make a canary with `box 'sudo mkdir /srv/canary && sudo touch /srv/canary/keep'`, then:
  `box sudo ln -s /srv/canary /var/lib/anci/exe/evil` and
  `box sudo ln -s /srv/canary/keep /etc/anci/units/evil2.service`. Plan shows `- exe.evil` and `- exe.evil2`.
  After reconcile and a 5-minute gc, `/srv/canary/keep` must still exist.
- A directory where a file should be: `box 'sudo rm /etc/anci/units/web.service && sudo mkdir
  /etc/anci/units/web.service'`. Note what plan and reconcile do.

## Final: back to the start

Restore the original `anci.toml` (zhao, `web`, `main`, 2 vcpus, 2gb, 24gb disk). Reconcile, **wait 5 min**,
reconcile again.

**Box**: only zhao is in the group, has a grant and has keys. Only `web`'s units, links and exe dir exist, plus
logtrim's. No secrets, litestream or caddy files. `0 unreferenced` blobs, and web has 0 static versions.

These remain by design: the accounts of removed users (with no keys or grants), the logs and release histories of
removed exes, and the dbs `jobs`, `main-db`, `x-y`, `x_y`, `v.2` and `0` with their data.
