import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from anci import errors, ui


class TestErrorTaxonomy(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(errors.ConfError("x").kind, "conf")
        self.assertEqual(errors.AppError("x").kind, "app")
        self.assertEqual(errors.EnvError("x").kind, "env")
        self.assertEqual(errors.AuthError("x").kind, "auth")
        self.assertEqual(errors.InternalError("x").kind, "internal")

    def test_internal_default_message(self):
        self.assertIn("report", errors.InternalError().what)


class TestErrorRendering(unittest.TestCase):
    def test_what_why_try(self):
        err = errors.ConfError(
            "exe 'demo' isn't declared on env 'prod'",
            why="defined exes: demo",
            try_="add [exe.demo.env.prod] to anci.toml",
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            ui.error(err)
        out = buf.getvalue()
        self.assertIn("error: exe 'demo' isn't declared on env 'prod'", out)
        self.assertIn("defined exes: demo", out)
        self.assertIn("try: add [exe.demo.env.prod] to anci.toml", out)


class TestHumanSize(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(ui.human_size(512), "512 B")
        self.assertEqual(ui.human_size(2 << 20), "2.0 MB")
        self.assertEqual(ui.human_size(3 << 30), "3.0 GB")


class TestStepsPlainMode(unittest.TestCase):
    """With ANSI off, output is plain and only final lines appear."""

    def setUp(self):
        ui.set_ansi(False)

    def tearDown(self):
        ui.set_ansi(False)

    def test_step_done_plain(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pipe = ui.Pipeline()
            step = pipe.step("uploading app")
            step.done("2.1 MB")
            pipe.done()
        out = buf.getvalue()
        self.assertIn("✓ uploading app", out)
        self.assertIn("2.1 MB", out)
        self.assertIn("done in", out)
        self.assertNotIn("\x1b[", out)  # no ANSI escapes in plain mode

    def test_step_fail_plain(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pipe = ui.Pipeline()
            step = pipe.step("restarting")
            step.fail()
        self.assertIn("✗ restarting", buf.getvalue())

    def test_skip(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pipe = ui.Pipeline()
            pipe.step("uploading app").skip("unchanged")
        self.assertIn("unchanged", buf.getvalue())


class TestStepTimers(unittest.TestCase):
    """Every completed step stamps its duration on the right."""

    def setUp(self):
        ui.set_ansi(False)

    def tearDown(self):
        ui.set_ansi(False)

    def test_done_skip_and_fail_show_elapsed(self):
        for label, end, elapsed, line in (
            ("uploading app", lambda s: s.done("2.1 MB"), 2.3, "✓ uploading app  2.1 MB  2.3s"),
            ("reconciling", lambda s: s.skip("clean"), 0.4, "– reconciling  clean  0.4s"),
            ("restarting", lambda s: s.fail(), 0.2, "✗ restarting  0.2s"),
        ):
            buf = io.StringIO()
            with self.subTest(label=label), redirect_stdout(buf), \
                 mock.patch.object(ui.time, "monotonic", side_effect=[0.0, elapsed]):
                end(ui.Step(label))
            self.assertIn(line, buf.getvalue())


class TestTable(unittest.TestCase):
    def test_alignment(self):
        out = ui.table(["exe", "url"], [["demo", "http://x:8000"]])
        self.assertIn("exe", out)
        self.assertIn("http://x:8000", out)
        lines = out.splitlines()
        self.assertEqual(len(set(len(l) for l in lines)), 1)  # box edges align

    def test_color_codes_dont_break_the_alignment(self):
        ui.set_ansi(True)
        try:
            table = ui.table(
                ["exe", "state"], [["app", ui.green("serving")], ["web", "down"]]
            )
        finally:
            ui.set_ansi(False)
        widths = {len(ui._ANSI_RE.sub("", line)) for line in table.splitlines()}
        self.assertEqual(len(widths), 1, f"ragged table:\n{table}")


class TestConfirm(unittest.TestCase):
    def setUp(self):
        ui.set_ansi(True)
        self.addCleanup(ui.set_ansi, False)

    def _confirm(self, answer=None, **kw):
        out = io.StringIO()
        stdin = io.StringIO(answer) if answer is not None else None
        with redirect_stdout(out):
            ok = ui.confirm("it replaces things:", ["line one", "line two"], "go?",
                            stdin=stdin, **kw)
        return ok, out.getvalue()

    def test_prints_a_red_warning_block(self):
        ok, out = self._confirm("y\n")
        red_open = "\x1b[31m"
        self.assertIn("WARNING", out)
        self.assertIn(red_open + " — it replaces things:", out)
        self.assertIn("  " + red_open + "line one", out)
        self.assertIn("  " + red_open + "line two", out)
        self.assertLess(out.index("it replaces things:"), out.index("line one"))

    def test_plain_without_color(self):
        ui.set_ansi(False)
        _, out = self._confirm("y\n")
        self.assertNotIn("\x1b", out)
        self.assertIn("  WARNING — it replaces things:", out)

    def test_yes_variants_and_everything_else(self):
        for yes in ("y\n", "yes\n", "YES\n"):
            self.assertTrue(self._confirm(yes)[0], yes)
        for no in ("n\n", "\n", "maybe\n", ""):
            self.assertFalse(self._confirm(no)[0], no)

    def test_blank_line_separates_the_warning_from_the_question(self):
        _, out = self._confirm("y\n")
        self.assertTrue(out.endswith("line two\x1b[0m\n\n"))

    def test_force_skips_the_question_but_still_warns(self):
        with mock.patch("builtins.input") as ask:
            ok, out = self._confirm(force=True)
        self.assertTrue(ok)
        ask.assert_not_called()
        self.assertIn("line one", out)

    def test_question_is_asked_in_red_on_a_terminal(self):
        with mock.patch.object(ui.sys, "stdin") as stdin, \
             mock.patch("builtins.input", return_value="y") as ask:
            stdin.isatty.return_value = True
            ok, _ = self._confirm()
        self.assertTrue(ok)
        self.assertEqual(ask.call_args.args[0], "  \x1b[31mgo? [y/N] \x1b[0m")

    def test_no_terminal_raises_the_callers_error(self):
        err = errors.EnvError("needs a terminal")
        with mock.patch.object(ui.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaises(errors.EnvError) as cm:
                self._confirm(no_tty_error=err)
        self.assertIs(cm.exception, err)


if __name__ == "__main__":
    unittest.main()
