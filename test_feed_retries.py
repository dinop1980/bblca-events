import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from make_feed import (
    FETCH_ATTEMPTS,
    FETCH_RETRY_SECONDS,
    SupersededRunError,
    ensure_main_current,
    fetch_calendar_page,
    main,
)


def unavailable(code=503):
    return HTTPError("https://calendar.invalid/", code, "Unavailable", {}, None)


def calendar_response():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b"calendar page"
    return response


class CalendarRetryTests(unittest.TestCase):
    def test_unguarded_fetch_keeps_all_six_attempts_and_ten_minute_delays(self):
        opener = Mock(side_effect=unavailable())
        sleeper = Mock()

        with self.assertRaises(HTTPError):
            fetch_calendar_page(opener=opener, sleeper=sleeper)

        self.assertEqual(opener.call_count, FETCH_ATTEMPTS)
        self.assertEqual(
            [call.args[0] for call in sleeper.call_args_list],
            [FETCH_RETRY_SECONDS] * (FETCH_ATTEMPTS - 1),
        )

    def test_guarded_fetch_can_recover_on_the_sixth_attempt(self):
        opener = Mock(side_effect=[unavailable()] * 5 + [calendar_response()])
        sleeper = Mock()

        page = fetch_calendar_page(
            opener=opener, sleeper=sleeper, safety_check=Mock(),
        )

        self.assertEqual(page, "calendar page")
        self.assertEqual(opener.call_count, 6)
        waits = [call.args[0] for call in sleeper.call_args_list]
        self.assertEqual(sum(waits), 5 * 600)
        self.assertLessEqual(max(waits), 30)

    def test_superseded_build_never_starts_a_calendar_request(self):
        opener, sleeper = Mock(), Mock()
        check = Mock(side_effect=SupersededRunError("main advanced"))

        with self.assertRaises(SupersededRunError):
            fetch_calendar_page(opener=opener, sleeper=sleeper, safety_check=check)

        opener.assert_not_called()
        sleeper.assert_not_called()

    def test_main_advancing_during_a_successful_fetch_discards_the_response(self):
        opener = Mock(return_value=calendar_response())
        sleeper = Mock()
        check = Mock(side_effect=[None, SupersededRunError("main advanced")])

        with self.assertRaises(SupersededRunError):
            fetch_calendar_page(opener=opener, sleeper=sleeper, safety_check=check)

        self.assertEqual(opener.call_count, 1)
        sleeper.assert_not_called()

    def test_permanent_calendar_errors_do_not_retry(self):
        opener = Mock(side_effect=unavailable(403))
        sleeper = Mock()

        with self.assertRaises(HTTPError):
            fetch_calendar_page(opener=opener, sleeper=sleeper, safety_check=Mock())

        self.assertEqual(opener.call_count, 1)
        sleeper.assert_not_called()


class MainHeadTests(unittest.TestCase):
    def test_unreachable_or_missing_remote_stops_work(self):
        failures = [
            subprocess.CalledProcessError(2, ["git", "ls-remote"]),
            subprocess.TimeoutExpired(["git", "ls-remote"], 20),
            OSError("git unavailable"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch("make_feed.subprocess.run", side_effect=failure):
                    with self.assertRaisesRegex(RuntimeError, "Cannot confirm origin/main"):
                        ensure_main_current("a" * 40)

    def test_ambiguous_remote_output_stops_work(self):
        for stdout in ("", "a refs/heads/other", "a refs/heads/main b refs/heads/main"):
            with self.subTest(stdout=stdout):
                result = subprocess.CompletedProcess([], 0, stdout=stdout)
                with patch("make_feed.subprocess.run", return_value=result):
                    with self.assertRaisesRegex(RuntimeError, "Cannot confirm origin/main"):
                        ensure_main_current("a" * 40)

    def test_real_remote_advance_stops_the_first_retry_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, work = root / "remote.git", root / "work"
            work.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=work, check=True,
                    capture_output=True, text=True,
                ).stdout.strip()

            git("init", "--bare", str(remote))
            git("init", "-b", "main")
            git("config", "user.name", "Retry test")
            git("config", "user.email", "retry-test@example.invalid")
            git("remote", "add", "origin", str(remote))
            git("commit", "--allow-empty", "-m", "initial")
            git("push", "origin", "main")
            expected_sha = git("rev-parse", "HEAD")
            waits = []

            def advance_remote(seconds):
                waits.append(seconds)
                git("commit", "--allow-empty", "-m", "main advanced")
                git("push", "origin", "main")

            opener = Mock(side_effect=unavailable())
            with contextlib.chdir(work):
                ensure_main_current(expected_sha)
                with self.assertRaises(SupersededRunError):
                    fetch_calendar_page(
                        opener=opener, sleeper=advance_remote,
                        safety_check=lambda: ensure_main_current(expected_sha),
                    )

            self.assertEqual(opener.call_count, 1)
            self.assertEqual(waits, [30])

    def test_superseded_generation_preserves_existing_feed_and_history_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "calendar.html"
            source.write_text(
                'this.AddAppointment("1", new Date(2026,9,14,9), '
                '3600000, [null], "Board Meeting");', encoding="utf-8",
            )
            state = root / "state.json"
            state.write_text(json.dumps({}) + "\n", encoding="utf-8")
            outputs = [root / name for name in ("feed.xml", "events.ics", "meetings.ics")]
            for output in outputs:
                output.write_bytes(b"previous valid feed")
            originals = {path: path.read_bytes() for path in [state, *outputs]}
            argv = [
                "make_feed.py", "--input", str(source), "--expected-main-sha", "a" * 40,
                "--state", str(state), "--output", str(outputs[0]),
                "--ics-output", str(outputs[1]), "--meetings-ics-output", str(outputs[2]),
            ]
            with patch("sys.argv", argv):
                with patch("make_feed.ensure_main_current", side_effect=[
                    None, SupersededRunError("main advanced before saving"),
                ]):
                    with self.assertRaises(SupersededRunError):
                        main()

            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
