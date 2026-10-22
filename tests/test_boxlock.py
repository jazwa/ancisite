"""The box lock, run for real: the lock script under local `bash` + `flock`
(no ssh, no sudo), on a temp lock file."""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from anci import boxlock, scratch, ssh


class LocalConn(ssh.Conn):
    """A Conn whose remote commands run here, minus the `sudo -n`."""

    def argv(self, args):
        if args[:2] == ["sudo", "-n"]:
            args = args[2:]
        return args


def is_locked(path) -> bool:
    return subprocess.run(["flock", "-n", path, "true"]).returncode != 0


@unittest.skipUnless(shutil.which("flock"), "needs flock")
class TestHold(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.lock = os.path.join(self.td, "anci.lock")
        for name, value in (("LOCK", self.lock), ("WHO", os.path.join(self.td, "anci.who"))):
            p = mock.patch.object(boxlock, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td)

    def test_held_inside_released_after(self):
        with boxlock.hold(LocalConn(ip="a"), "jack", "deploy"):
            self.assertTrue(is_locked(self.lock))
        self.assertFalse(is_locked(self.lock))

    def test_released_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with boxlock.hold(LocalConn(ip="a"), "jack", "deploy"):
                raise RuntimeError
        self.assertFalse(is_locked(self.lock))

    def test_reentrant_in_one_process(self):
        conn = LocalConn(ip="a")
        with mock.patch.object(boxlock, "_acquire", wraps=boxlock._acquire) as acquire:
            with boxlock.hold(conn, "jack", "migrate"):
                with boxlock.hold(conn, "jack", "converge"):
                    self.assertTrue(is_locked(self.lock))
                self.assertTrue(is_locked(self.lock))   # the inner exit doesn't release
        self.assertEqual(acquire.call_count, 1)
        self.assertFalse(is_locked(self.lock))

    def test_a_second_run_waits_and_names_the_holder(self):
        said, got = [], threading.Event()

        def second():
            # another "process": a different box key, the same lock file
            with boxlock.hold(LocalConn(ip="b"), "alice", "deploy"):
                got.set()

        with mock.patch.object(boxlock, "print", create=True, side_effect=said.append):
            with boxlock.hold(LocalConn(ip="a"), "jack", "deploy"):
                t = threading.Thread(target=second)
                t.start()
                for _ in range(100):
                    if said:
                        break
                    time.sleep(0.02)
                self.assertFalse(got.is_set())
            t.join(5)
        self.assertTrue(got.is_set())
        self.assertIn("waiting for jack (deploy, since", said[0])


class TestScratchNames(unittest.TestCase):
    def test_in_dir_carries_tag_and_run_id(self):
        self.assertEqual(scratch.in_dir("/d", "blob"), f"/d/.tmp-blob-{scratch.RUN_ID}")
        self.assertRegex(scratch.RUN_ID, rf"^{os.getpid()}-[0-9a-f]{{6}}$")


if __name__ == "__main__":
    unittest.main()
