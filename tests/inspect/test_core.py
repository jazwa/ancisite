"""diff and the one-line format `plan` prints."""

import tempfile
import unittest
from unittest import mock

from anci import errors, inspect
from anci.inspect import Action, Change, ObservedState, TargetState, diff, format_change
from anci.inspect.core import FileSet, check

from .support import CONF, load_conf


class TestDiff(unittest.TestCase):
    def test_only_in_the_target_is_a_create(self):
        self.assertEqual(diff(ObservedState(), TargetState({"exe.web": {"kind": "service", "timer": None}})),
                         [Change("exe.web", Action.CREATE, {"kind": (None, "service")})])

    def test_only_on_the_box_is_a_delete(self):
        self.assertEqual(diff(ObservedState({"user.old": {"group": False, "sudoers": "s", "keys": None}}),
                              TargetState()),
                         [Change("user.old", Action.DELETE, {"group": (False, None), "sudoers": ("s", None)})])

    def test_update_lists_only_the_fields_that_differ(self):
        observed = ObservedState({"exe.report": {"kind": "service", "unit": "a"}})
        target = TargetState({"exe.report": {"kind": "job", "unit": "a"}})
        self.assertEqual(diff(observed, target),
                         [Change("exe.report", Action.UPDATE, {"kind": ("service", "job")})])

    def test_a_field_missing_from_the_target_is_not_compared(self):
        observed = ObservedState({"exe.web": {"kind": "service", "blob": "9f2c"}})
        self.assertEqual(diff(observed, TargetState({"exe.web": {"kind": "service"}})), [])

    def test_a_none_target_field_must_be_absent(self):
        observed = ObservedState({"exe.web": {"static": "4be0"}})
        self.assertEqual(diff(observed, TargetState({"exe.web": {"static": None}})),
                         [Change("exe.web", Action.UPDATE, {"static": ("4be0", None)})])


class TestFormat(unittest.TestCase):
    def test_short_by_default(self):
        self.assertEqual(format_change(Change("exe.web", Action.CREATE, {"kind": (None, "service")})),
                         "+ exe.web")
        c = Change("exe.web", Action.UPDATE, {"kind": ("service", "job"), "unit": ("a" * 64, "b" * 64),
                                              "blob": ("0123456789abcdef", "fedcba9876543210"),
                                              "bin": (None, "/var/lib/anci/exe/web/current")})
        self.assertEqual(format_change(c), "~ exe.web  kind: service → job, unit, blob, bin")

    def test_a_missed_restart_reads_as_one(self):
        c = Change("exe.web", Action.UPDATE, {"pending": (True, False)})
        self.assertEqual(format_change(c), "~ exe.web  not restarted since its last change")

    def test_none_and_no_fields(self):
        self.assertEqual(format_change(Change("db.x", Action.UPDATE, {"replica": ("b p", None)})),
                         "~ db.x  replica: b p → none")
        self.assertEqual(format_change(Change("caddy", Action.DELETE, {})), "- caddy")


class _Thing(FileSet):
    name = "thing"
    files = {"conf": "/etc/thing.conf"}
    links = {"link": ("/etc/thing", "/etc/thing.conf")}

    def __init__(self, wanted):
        self.wanted = wanted

    def contents(self, conf, env_name):
        return {"conf": "x"} if self.wanted else None


class TestFileSet(unittest.TestCase):
    def test_absent_on_both_sides_is_no_change(self):
        t = _Thing(wanted=False)
        self.assertEqual(t.target(None, "dev"), {})
        self.assertEqual(t.observed({"conf": [], "link": []}), {})

    def test_present_when_any_file_or_link_is(self):
        self.assertEqual(_Thing(wanted=False).observed({"conf": [], "link": ["/etc/thing.conf"]}),
                         {"thing": {"conf": None, "link": "/etc/thing.conf"}})


class TestObservedState(unittest.TestCase):
    def test_one_ssh_call_and_each_resource_gets_its_own_keys(self):
        seen = {}

        class A(FileSet):
            name = "a"
            files = {"f": "/a"}

            def observed(self, found):
                seen["a"] = found
                return {}

        class B(A):
            name = "b"
            files = {"f": "/b"}

            def observed(self, found):
                seen["b"] = found
                return {}

        facts = mock.MagicMock(return_value={"a/f": ["1" * 64]})
        with mock.patch.object(inspect, "RESOURCES", (A(), B())), \
             mock.patch.object(inspect.ssh, "facts", facts):
            inspect.observed_state(None)
        facts.assert_called_once()
        self.assertEqual(seen, {"a": {"f": ["1" * 64]}, "b": {"f": []}})


class TestCheck(unittest.TestCase):
    """Anything a resource reports but doesn't declare fails at once."""

    def _check(self, part, state=None):
        check(_Thing(wanted=True), part, state if state is not None else ObservedState())

    def test_declared_fields_pass(self):
        self._check({"thing": {"conf": "a", "link": "b"}})
        check(_Thing(wanted=True), {"thing": {"conf": "a"}}, TargetState())   # a target may leave some out

    def test_an_unknown_field_fails(self):
        with self.assertRaisesRegex(errors.InternalError, "doesn't match"):
            self._check({"thing": {"conf": "a", "link": "b", "mode": "0644"}})

    def test_an_observed_field_left_out_fails(self):
        with self.assertRaisesRegex(errors.InternalError, "doesn't match"):
            self._check({"thing": {"conf": "a"}})

    def test_another_resources_identifier_fails(self):
        with self.assertRaisesRegex(errors.InternalError, "isn't one of its own"):
            self._check({"things.x": {"conf": "a", "link": "b"}})

    def test_an_identifier_reported_twice_fails(self):
        with self.assertRaisesRegex(errors.InternalError, "two resources"):
            self._check({"thing": {"conf": "a", "link": "b"}}, ObservedState({"thing": {}}))

    def test_every_resource_reports_only_what_it_declares(self):
        # an empty box, and a conf that turns every resource on
        conf_text = (CONF + "\n[db.main-db.replication]\nbucket = 'backups'\n")
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, conf_text)
            with mock.patch.object(inspect.ssh, "facts", return_value={}):
                inspect.observed_state(None)
            inspect.target_state(conf, "dev")


if __name__ == "__main__":
    unittest.main()
