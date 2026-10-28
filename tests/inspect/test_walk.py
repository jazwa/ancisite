"""The walk: steps collected once each, ordered by `after` and RULES, and
every resource able to apply every field it reports."""

import unittest
from unittest import mock

from anci import errors
from anci.inspect import RESOURCES
from anci.inspect import walk as walk_mod
from anci.inspect.core import Step


def _order(steps: list[Step]) -> list[str]:
    """Order bare steps the way `walk` does (no changes, no always-steps)."""
    with mock.patch.object(walk_mod, "always", return_value=steps), \
         mock.patch.object(walk_mod, "_resource"):
        ctx = mock.Mock()
        ctx.uploads.blobs = {}
        return [s.id for s in walk_mod.walk([], ctx)]


class TestOrder(unittest.TestCase):
    def test_rules_order_writes_reload_restarts_and_cleanup(self):
        order = _order([Step("box/gc", ""), Step("exe.web/restart", ""), Step("box/daemon-reload", ""),
                        Step("exe.web/write", ""), Step("box/layout", "")])
        self.assertEqual(order, ["box/layout", "exe.web/write", "box/daemon-reload",
                                 "exe.web/restart", "box/gc"])

    def test_secrets_are_written_before_restarts_and_removed_after(self):
        order = _order([Step("exe.web/restart", ""), Step("secrets/write", "")])
        self.assertLess(order.index("secrets/write"), order.index("exe.web/restart"))
        order = _order([Step("secrets/remove", ""), Step("exe.web/restart", "")])
        self.assertLess(order.index("exe.web/restart"), order.index("secrets/remove"))

    def test_a_replica_is_reset_while_litestream_is_stopped(self):
        order = _order([Step("litestream/start", ""), Step("db.a/reset-replica", ""),
                        Step("litestream/stop", "")])
        self.assertEqual(order, ["litestream/stop", "db.a/reset-replica", "litestream/start"])

    def test_after_names_only_steps_in_the_run(self):
        self.assertEqual(_order([Step("exe.web/restart", "", after=("exe.api/stop",))]),
                         ["exe.web/restart"])


class TestCollect(unittest.TestCase):
    def _walk(self, steps):
        with mock.patch.object(walk_mod, "always", return_value=steps), \
             mock.patch.object(walk_mod, "_resource"):
            ctx = mock.Mock()
            ctx.uploads.blobs = {}
            return walk_mod.walk([], ctx)

    def test_the_same_step_twice_runs_once_after_both(self):
        [s] = [s for s in self._walk([Step("exe.web/restart", "x", after=("a",)),
                                      Step("exe.web/restart", "x", after=("b",))])]
        self.assertEqual(s.after, ("a", "b"))

    def test_two_steps_with_one_name_and_different_scripts_fail(self):
        with self.assertRaisesRegex(errors.InternalError, "different steps named exe.web/restart"):
            self._walk([Step("exe.web/restart", "x"), Step("exe.web/restart", "y")])

    def test_a_cycle_fails(self):
        with self.assertRaisesRegex(errors.InternalError, "circle"):
            self._walk([Step("a/x", "", after=("b/y",)), Step("b/y", "", after=("a/x",))])


class TestRows(unittest.TestCase):
    """Every field a resource can report a change for maps to a row (and so to
    steps): a field added to inspect without one fails here, not on a box."""

    NEVER_COMPARED = {"db": {"exists"}}     # read off the box, left out of every target

    def test_every_field_has_a_row(self):
        for r in RESOURCES:
            fields = set(r.fields) - self.NEVER_COMPARED.get(r.name, set())
            self.assertEqual(set(r.rows) - {"CREATE", "DELETE"}, fields, r.name)

    def test_what_the_box_always_has_is_never_deleted(self):
        for r in RESOURCES:
            if r.name in ("logtrim", "sshd", "resolved", "vendored"):
                self.assertNotIn("DELETE", r.rows, r.name)


class TestScript(unittest.TestCase):
    def test_each_step_is_announced_and_isolated(self):
        out = walk_mod.script([Step("a/x", "echo hi")])
        self.assertIn("echo '::step a/x'; echo '::step a/x' >&2\n(\nset -e\necho hi\n)", out)

    def test_output_is_read_back_per_step(self):
        self.assertEqual(walk_mod.step_output("::step a/x\nhi\n::step b/y\n"),
                         {"a/x": ["hi"], "b/y": []})
        self.assertEqual(walk_mod.failed_step("::step a/x\n::step b/y\nboom\n"), "b/y")


if __name__ == "__main__":
    unittest.main()
