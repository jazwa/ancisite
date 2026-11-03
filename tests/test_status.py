"""`anci <env> status` — the db table (litestream): probe shape, reading the
facts, the plain-text replication cell, and the drift lines.

Transport-mocked: the whole `anci.ssh` surface is stubbed so nothing here
touches the network (partial mocks fall through to real ssh).
"""

import contextlib
import io
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from anci import auth
from anci import inspect, layout, releases, ssh
from anci.commands import status
from anci.conf import (
    Conf,
    Db,
    Env,
    Exe,
    ExeOverride,
    Replication,
    User,
    allocate_ports,
    load,
)

from .status_support import box_facts, observed

CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './bin/server'

[exe.app.env.dev]

[db.guests]
migrations = './m'

[db.guests.replication]
bucket = 'guests-backups'
endpoint = 'https://s3.example.com'
access-key-env = 'S3_KEY'
secret-key-env = 'S3_SECRET'
"""

UNIT_SHA = "a" * 64
REPLICA_TS = "2026-10-05T19:50:03Z"                     # newest replica write
REPLICA_EPOCH = int(datetime.fromisoformat(REPLICA_TS).timestamp())


def load_in(directory: str, text: str = CONF):
    p = Path(directory) / "anci.toml"
    p.write_text(text)
    return load(str(p))


def make_conf(replication: "Replication | None" =
              Replication(bucket="guests-backups", endpoint="https://s3.example.com", access_key_env="S3_KEY", secret_key_env="S3_SECRET")) -> Conf:
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["k"])},
        envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                         anci_version="0.0.1")},
        exes={"app": Exe(name="app", source="./s",
                         env_overrides={"dev": ExeOverride(env="dev")})},
        dbs={"guests": Db(name="guests", migrations="./m",
                          replication=replication)},
    )


class TestDbProbes(unittest.TestCase):
    """The probes carry the db facts; lag only where replicated."""

    def _probes(self, conf) -> dict:
        return status._probes(conf, sorted(conf.exes), "dev", allocate_ports(conf))

    def _script(self, conf) -> str:
        return "\n".join(self._probes(conf).values())

    def test_one_litestream_probe_reads_the_daemon_not_s3(self):
        probe = self._probes(make_conf(replication=None))["litestream dbs"]   # asked even when off
        self.assertIn("litestream list -socket /run/litestream/litestream.sock -json", probe)
        self.assertNotIn("secrets.env", probe)   # no bucket credentials involved

    def test_litestream_probe_lists_every_db_and_its_last_sync(self):
        awk = self._probes(make_conf())["litestream dbs"].split(" | ", 1)[1]
        listing = (
            "{\n  \"databases\": [\n"
            "    {\n      \"path\": \"/other.db\",\n"
            "      \"last_sync_at\": \"2026-01-01T00:00:00.5-04:00\"\n    },\n"
            "    {\n      \"path\": \"/var/lib/anci/db/guests/data.sql\",\n"
            "      \"status\": \"replicating\",\n"
            "      \"last_sync_at\": \"2026-10-14T13:14:12.259043773-04:00\"\n    }\n  ]\n}\n"
        )
        out = subprocess.run(["bash", "-c", awk], input=listing,
                             text=True, capture_output=True).stdout
        self.assertEqual(out.splitlines(), ["/other.db\t2026-01-01T00:00:00-04:00",
                                            "/var/lib/anci/db/guests/data.sql\t2026-10-14T13:14:12-04:00"])

    def test_carries_db_probes(self):
        with tempfile.TemporaryDirectory() as td:
            probes = self._probes(load_in(td))
        script = "\n".join(probes.values())
        for key in ("guests last", "guests size", "litestream dbs"):
            self.assertIn(key, probes)
        # last migration reads the guest's bookkeeping table as the service user
        self.assertIn("SELECT name FROM _anci_migrations", script)
        self.assertIn("sudo -n -u anci sqlite3", script)
        # size covers the data file and its wal
        self.assertIn("stat -c%s /var/lib/anci/db/guests/data.sql", script)
        self.assertIn("stat -c%s /var/lib/anci/db/guests/data.sql-wal", script)
        # replication lag reads the daemon's last successful sync over its socket
        self.assertIn("litestream list -socket", script)

    def test_no_dbs_no_lit_probes(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            conf.dbs = {}
            probes = self._probes(conf)
        self.assertFalse(any(k.startswith("guests") for k in probes))


class TestReadDbFacts(unittest.TestCase):
    def test_reads_db_facts(self):
        found = {
            "guests last": ["0003_add_guests"],
            "guests size": ["12288"],
            "litestream dbs": [f"/var/lib/anci/db/guests/data.sql\t{REPLICA_TS}"],
        }
        parsed = status._read(observed(found), make_conf(), ["app"], "dev")
        db = parsed.dbs["guests"]
        self.assertEqual(db.last, "0003_add_guests")
        self.assertEqual(db.size, 12288)
        self.assertTrue(db.exists)
        self.assertTrue(db.replicating)
        self.assertEqual(db.replica_ts, REPLICA_TS)

    def test_missing_db_and_empty_bookkeeping(self):
        conf = make_conf()
        conf.dbs["ghosts"] = Db(name="ghosts", migrations="./m")
        # guests: file exists, no bookkeeping rows, no birth, no replica row
        parsed = status._read(observed({"guests size": ["4096"]}),
                              conf, ["app"], "dev")
        self.assertIs(parsed.dbs["ghosts"].exists, False)
        self.assertIsNone(parsed.dbs["guests"].last)
        self.assertEqual(parsed.dbs["guests"].size, 4096)
        self.assertIsNone(parsed.dbs["guests"].replica_ts)


class TestBehind(unittest.TestCase):
    def _at(self, offset):
        return mock.patch.object(status.time, "time", return_value=REPLICA_EPOCH + offset)

    def test_seconds(self):
        with self._at(2):
            self.assertEqual(status._behind(REPLICA_TS), "2s since last sync")

    def test_clock_behind_replica_is_caught_up(self):
        with self._at(-5):
            self.assertEqual(status._behind(REPLICA_TS), "0s since last sync")

    def test_minutes(self):
        with self._at(250):
            self.assertEqual(status._behind(REPLICA_TS), "4m since last sync")

    def test_grows_with_the_clock(self):
        with self._at(8 * 60):
            self.assertEqual(status._behind(REPLICA_TS), "8m since last sync")

    def test_no_replica_timestamp(self):
        self.assertIsNone(status._behind(None))
        self.assertIsNone(status._behind(""))
        self.assertIsNone(status._behind("-"))

    def test_unparseable_timestamp(self):
        self.assertIsNone(status._behind("not-a-date"))

class TestUrlColumn(unittest.TestCase):
    """The exe url is a report, not a conf promise — only when the port answers."""

    def _cell(self, *, port_open: bool) -> str:
        conf = make_conf()
        found = {"app current": [f"{layout.BLOB_DIR}/aaaa"], "app active": ["active"]}
        if port_open:
            found["app port"] = ["open"]
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect",
                               return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts", return_value=box_facts(found)), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev")
        for line in buf.getvalue().splitlines():
            if " app " in line and "│" in line:
                return line
        return ""

    def test_url_shown_when_the_port_answers(self):
        self.assertIn("http://", self._cell(port_open=True))

    def test_url_dash_when_the_port_does_not_answer(self):
        self.assertNotIn("http://", self._cell(port_open=False))
        self.assertIn("—", self._cell(port_open=False))

    def test_url_dash_for_a_hostless_remote_even_when_port_open(self):
        conf = make_conf()
        # swap dev for a remote env with no host: nothing publicly reachable
        conf.envs["dev"].ip = "203.0.113.10"
        conf.envs["dev"].mode = "adopt"
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect",
                               return_value=ssh.Conn(ip="203.0.113.10")), \
             mock.patch.object(ssh, "facts", return_value=box_facts({"app port": ["open"]})), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev")
        self.assertNotIn("http", buf.getvalue())


class TestReplicationCell(unittest.TestCase):
    """The cell says what the box does; a note says when the conf disagrees."""

    def setUp(self):
        p = mock.patch.object(status.time, "time", return_value=REPLICA_EPOCH + 2)
        p.start()
        self.addCleanup(p.stop)

    def cell(self, conf, *, exists=True, replicating=True, ts=REPLICA_TS, up=True):
        st = status.DbState(size=1 if exists else None, replicating=replicating, replica_ts=ts)
        return status._replication_cell(conf, "dev", "guests", st, up)

    def test_cells(self):
        on, off = make_conf(), make_conf(replication=None)
        self.assertEqual(self.cell(on), "2s since last sync")
        self.assertEqual(self.cell(off), "2s since last sync")              # still running there
        self.assertEqual(self.cell(on, ts=None), "litestream error")       # listed, never synced
        self.assertEqual(self.cell(on, replicating=False, up=False), "litestream error")
        self.assertEqual(self.cell(off, replicating=False), "off")
        self.assertEqual(self.cell(on, exists=False), "—")

    def test_notes_when_the_box_and_conf_differ(self):
        def notes(conf, replicating):
            st = status.DbState(size=1, replicating=replicating, replica_ts=REPLICA_TS)
            report = status.Report({}, [], {}, {"guests": st}, litestream_up=True)
            return status._replication_notes(conf, "dev", report)
        self.assertIn("still replicating", notes(make_conf(replication=None), True)[0])
        self.assertIn("isn't replicating yet", notes(make_conf(), False)[0])
        self.assertEqual(notes(make_conf(), True), [])

class TestStatusDbTable(unittest.TestCase):
    def _conf(self) -> Conf:
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(CONF)
        fd.close()
        return load(fd.name)

    def _run(self, conf, found: dict, debug: bool = False,
             facts_raises: Exception | None = None) -> str:
        buf = io.StringIO()
        with mock.patch.object(status.time, "time", return_value=REPLICA_EPOCH + 2), \
             mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect",
                               return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts") as facts_mock, \
             mock.patch.object(status, "_box_changes", return_value=[]), \
             contextlib.redirect_stdout(buf):
            if facts_raises is not None:
                facts_mock.side_effect = facts_raises
            else:
                facts_mock.return_value = box_facts(found)
            status.status(conf, "dev", debug=debug)
        return buf.getvalue()

    def _guest_facts(self, **kw) -> dict:
        found = {
            "app current": ["/var/lib/anci/store/blobs/aaaa"],
            "app active": ["active"],
            "app port": ["open"],
            "litestream.service active": [kw.get("lit_state", "active")],
            "litestream.service enabled": ["enabled"],
            "litestream.service installed": ["yes"],
            "guests last": ["0003_add_guests"],
            "guests size": ["12288"],
            "litestream active": ["active"],
            "litestream dbs": [f"/var/lib/anci/db/guests/data.sql\t{REPLICA_TS}"],
        }
        return found

    def test_renders_the_db_table(self):
        conf = self._conf()
        text = self._run(conf, self._guest_facts())
        self.assertIn("db", text)
        self.assertIn("last migration", text)
        self.assertIn("size", text)
        self.assertIn("replication", text)
        self.assertIn("guests", text)
        self.assertIn("0003_add_guests.sql", text)   # full filename
        self.assertIn("12.0 KB", text)
        self.assertIn("2s since last sync", text)             # plain text, no grading
        self.assertNotIn("destination", text)        # where it goes is in `anci info`
        self.assertIn("ssh with", text)              # login hint when the box is up

    def test_checklist_drops_the_litestream_line(self):
        conf = self._conf()
        text = self._run(conf, self._guest_facts())
        self.assertNotIn("litestream.service", text)
        # the replication number lives in the db table now
        self.assertIn("2s since last sync", text)
        # a local env is reached through its localhost forward: no "not configured" note
        self.assertNotIn("not configured", text)

    def test_box_down_grays_the_db_rows(self):
        conf = self._conf()
        text = self._run(
            conf, {},
            facts_raises=ssh.SSHError("connection refused", unreachable=True))
        self.assertIn("db", text)
        # the row is all dashes, no invented state
        self.assertNotIn("behind", text)
        self.assertNotIn("litestream down", text)
        self.assertNotIn("ssh with", text)           # no login hint for a down box
        self.assertIn("guests", text)                # the db is still listed

    def test_auth_failure_is_not_box_down(self):
        # a reachable box that refuses the login isn't "down" — it errors, and
        # the CLI taxonomy explains it (the guest account is missing/mismatched)
        conf = self._conf()
        with self.assertRaises(ssh.SSHError):
            self._run(
                conf, {},
                facts_raises=ssh.SSHError("permission denied",
                                           unreachable=True, auth=True))

    def test_no_access_degrades_the_db_rows(self):
        conf = self._conf()
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="?"), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev")
        self.assertIn("guests", buf.getvalue())


class TestBoxChanges(unittest.TestCase):
    """--debug drift is what `anci <env> plan` shows."""

    def _changes(self, edit=None):
        with tempfile.TemporaryDirectory() as td:
            conf = load_in(td)
            observed = inspect.ObservedState(
                {i: dict(f) for i, f in inspect.target_state(conf, "dev").items()})
            if edit:
                edit(observed)
            return status._box_changes(conf, "dev", observed)

    def test_clean_box_has_no_drift(self):
        self.assertEqual(self._changes(), [])

    def test_edited_and_missing_files_are_reported(self):
        def edit(observed):
            observed["exe.app"]["unit"] = "0" * 64
            del observed["litestream"]

        changes = self._changes(edit)
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes, ["~ exe.app  unit", "+ litestream"])


class TestResourceStats(unittest.TestCase):
    """cpu/mem: parsing the probe output, and what the exe table shows."""

    def test_cpu_percent_from_two_samples(self):
        # 0.4s sample window; a 0.2s delta of cpu time ≈ 50% of one core
        delta = int(0.2 * 1e9)
        self.assertAlmostEqual(status._cpu_percent(f"1000 {1000 + delta}"), 50.0, places=1)

    def test_cpu_percent_handles_junk_and_inactive(self):
        for bad in (None, "", "[not set]", "123", "a b"):
            self.assertIsNone(status._cpu_percent(bad))

    def test_mem_unset_sentinel_is_none(self):
        self.assertEqual(status._mem("299859968"), 299859968)
        self.assertIsNone(status._mem(str((1 << 64) - 1)))  # systemd "unset"
        self.assertIsNone(status._mem(None))

    def test_service_probes_cpu_and_mem(self):
        probes = status._probes(make_conf(), ["app"], "dev", allocate_ports(make_conf()))
        self.assertIn("app cpu", probes)
        self.assertIn("app mem", probes)
        self.assertIn("CPUUsageNSec", probes["app cpu"])
        self.assertIn("MemoryCurrent", probes["app mem"])
        self.assertNotIn("MemoryPeak", probes["app mem"])   # current only, no peak

    def _row(self, found: dict) -> str:
        conf = make_conf()
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts", return_value=box_facts(found)), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev")
        out = buf.getvalue()
        self.assertNotIn("version", out)   # dropped from the default table
        for line in out.splitlines():
            if " app " in line and "│" in line:
                return line
        return ""

    def test_running_service_shows_cpu_and_mem(self):
        delta = int(0.04 * 1e9)   # ~10% of one core
        row = self._row({
            "app current": [f"{layout.BLOB_DIR}/aaaa"],
            "app active": ["active"],
            "app port": ["open"],
            "app cpu": [f"1000 {1000 + delta}"],
            "app mem": ["299859968"],
        })
        self.assertIn("10.0%", row)
        self.assertIn("286.0 MB", row)
        self.assertNotIn("pk", row)   # no peak

    def test_stopped_service_shows_dashes(self):
        row = self._row({"app current": [f"{layout.BLOB_DIR}/aaaa"], "app active": ["inactive"]})
        # no cpu/mem for a service that isn't running
        self.assertNotIn("%", row)
        self.assertNotIn("MB", row)
        self.assertIn("stopped", row)

    def test_a_crash_looping_service_is_failing_not_stopped(self):
        current = [f"{layout.BLOB_DIR}/aaaa"]
        row = self._row({"app current": current, "app active": ["activating"], "app restarts": ["3"]})
        self.assertIn("failing · restarted 3×", row)
        row = self._row({"app current": current, "app active": ["failed"], "app restarts": ["5"]})
        self.assertIn("failed", row)
        self.assertNotIn("stopped", row)


class TestBoxStats(unittest.TestCase):
    """Box-level disk + mem footer lines: plain numbers, no grading."""

    def test_probes_whole_disk_and_mem(self):
        probes = status._probes(make_conf(), ["app"], "dev", allocate_ports(make_conf()))
        self.assertIn("df -P -B1 /", probes["box disk"])
        self.assertIn("MemAvailable", probes["box mem"])

    def test_pair_parses_and_rejects_junk(self):
        self.assertEqual(status._pair("1904271360 79048540160"), (1904271360, 79048540160))
        for bad in (None, "", "123", "a b", "1 2 3"):
            self.assertIsNone(status._pair(bad))

    def _footer(self, found: dict) -> str:
        conf = make_conf()
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts", return_value=box_facts(found)), \
             contextlib.redirect_stdout(buf):
            status.status(conf, "dev")
        return buf.getvalue()

    def test_shows_disk_and_mem_numbers(self):
        out = self._footer({
            "app current": [f"{layout.BLOB_DIR}/aaaa"], "app active": ["active"],
            "box disk": ["1904271360 79048540160"],
            "box mem": ["663998464 8134729728"],
        })
        self.assertIn("disk", out)
        self.assertIn("1.8 GB / 73.6 GB (2%)", out)
        self.assertIn("mem", out)
        self.assertIn("633.2 MB / 7.6 GB (8%)", out)
        # just a number — none of the health grading vocabulary
        self.assertNotIn("✗ disk", out)
        self.assertNotIn("⚠", out)

    def test_missing_probe_drops_only_that_line(self):
        out = self._footer({
            "app current": [f"{layout.BLOB_DIR}/aaaa"], "app active": ["active"],
            "box mem": ["663998464 8134729728"],   # disk probe absent
        })
        self.assertIn("mem", out)
        # no disk line, and no crash
        self.assertNotIn("/ 73.6 GB", out)

    def test_down_box_has_no_box_stats(self):
        buf = io.StringIO()
        with mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "facts",
                               side_effect=ssh.SSHError("x", unreachable=True)), \
             contextlib.redirect_stdout(buf):
            status.status(make_conf(), "dev")
        self.assertNotIn("GB /", buf.getvalue())   # no numbers when the box is down


if __name__ == "__main__":
    unittest.main()
