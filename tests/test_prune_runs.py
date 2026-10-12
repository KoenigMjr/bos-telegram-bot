"""Das Aufräumen alter Workflow-Läufe (.github/scripts/prune_runs.py).

Das Skript löscht, deshalb sind hier vor allem die Schutzregeln festgenagelt: Es wird nie ein laufender oder
wartender Lauf gelöscht, nie der aktuelle, nie etwas ohne lesbares Datum, und die neuesten Läufe jedes Workflows
bleiben immer."""
import contextlib
import datetime as dt
import importlib.util
import io
import os
import random
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "scripts", "prune_runs.py")
spec = importlib.util.spec_from_file_location("prune_runs", SCRIPT)
prune_runs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune_runs)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def make_run(run_id, days_old, workflow=1, status="completed", name=None):
    """Ein Lauf, der vor days_old Tagen gestartet wurde."""
    created = NOW - dt.timedelta(days=days_old)
    return {"id": run_id, "workflow_id": workflow, "name": name or f"Workflow {workflow}",
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status, "conclusion": "success"}


def many(count, first_age, workflow=1, first_id=1, **kwargs):
    """count Läufe mit den Altern first_age, first_age + 1, ... (aufsteigend alt) und fortlaufenden IDs."""
    return [make_run(first_id + i, first_age + i, workflow, **kwargs) for i in range(count)]


def ids(runs):
    return [run["id"] for run in runs]


def select(runs, keep_days=30, keep_per_workflow=10, **kwargs):
    return prune_runs.select_runs_to_delete(runs, NOW, keep_days, keep_per_workflow, **kwargs)


class SelectionTests(unittest.TestCase):
    def test_old_finished_runs_beyond_the_newest_ones_are_selected(self):
        runs = many(15, 31)                                  # Alter 31 bis 45 Tage
        selected = select(runs)
        self.assertEqual(sorted(ids(selected)), [11, 12, 13, 14, 15])       # die fünf ältesten, die zehn neuesten bleiben

    def test_runs_younger_than_the_cut_off_are_never_selected(self):
        self.assertEqual(select(many(20, 1)), [])

    def test_the_newest_n_are_kept_even_when_all_of_them_are_old(self):
        selected = select(many(12, 100))
        self.assertEqual(sorted(ids(selected)), [11, 12])                  # nur die zwei ältesten
        self.assertEqual(select(many(10, 100)), [])                         # genau zehn: nichts wird gelöscht

    def test_the_newest_are_counted_per_workflow(self):
        runs = many(12, 100, workflow=1, first_id=1) + many(3, 100, workflow=2, first_id=100)
        self.assertEqual(sorted(ids(select(runs))), [11, 12])               # Workflow 2 hat nur drei, davon bleiben alle

    def test_a_workflow_with_fewer_runs_than_n_is_untouched(self):
        self.assertEqual(select(many(4, 200)), [])

    def test_runs_that_are_not_finished_are_never_selected(self):
        for status in ("in_progress", "queued", "waiting", "requested", "pending", "unbekannt", None):
            old = [make_run(100 + i, 40 + i, status="completed") for i in range(10)]    # die zehn neuesten, geschützt
            runs = old + [make_run(1, 300, status=status)]                              # uralt und nicht abgeschlossen
            self.assertEqual(select(runs), [], f"Status {status!r} darf nicht gelöscht werden")

    def test_an_unfinished_run_still_occupies_one_of_the_protected_slots(self):
        runs = [make_run(1, 40, status="in_progress")] + many(10, 41, first_id=100)     # der laufende ist der neueste
        self.assertEqual(ids(select(runs)), [109])                           # nur der älteste der zehn fertigen fällt heraus

    def test_the_current_run_is_never_selected(self):
        runs = many(15, 31)
        self.assertNotIn(15, ids(select(runs, current_run_id=15)))
        self.assertEqual(sorted(ids(select(runs, current_run_id=15))), [11, 12, 13, 14])

    def test_the_result_is_oldest_first(self):
        selected = select(many(15, 31))
        ages = [run["created_at"] for run in selected]
        self.assertEqual(ages, sorted(ages))
        self.assertEqual(ids(selected), [15, 14, 13, 12, 11])

    def test_the_limit_keeps_the_oldest_ones_first_and_the_rest_waits(self):
        self.assertEqual(ids(select(many(15, 31), limit=2)), [15, 14])
        self.assertEqual(ids(select(many(15, 31), limit=0)), [])
        self.assertEqual(ids(select(many(15, 31), limit=-3)), [])
        self.assertEqual(len(select(many(15, 31), limit=100)), 5)

    def test_exactly_at_the_cut_off_stays_one_second_older_goes(self):
        at_cutoff = make_run(1, 30)
        older = dict(make_run(2, 30), created_at=(NOW - dt.timedelta(days=30, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        filler = many(10, 1, first_id=100)                                   # die zehn neuesten, damit beide zur Wahl stehen
        self.assertEqual(ids(select(filler + [at_cutoff, older])), [2])

    def test_zero_days_and_zero_to_keep_removes_every_finished_run_in_the_past(self):
        runs = many(5, 1) + [make_run(99, 0, status="in_progress")]
        self.assertEqual(sorted(ids(select(runs, keep_days=0, keep_per_workflow=0))), [1, 2, 3, 4, 5])

    def test_runs_without_a_readable_date_always_stay(self):
        broken = [dict(make_run(1, 500), created_at=None), dict(make_run(2, 500), created_at="kaputt"),
                  {k: v for k, v in make_run(3, 500).items() if k != "created_at"}, dict(make_run(4, 500), created_at=12345)]
        self.assertEqual(select(broken, keep_per_workflow=0), [])

    def test_a_run_with_a_broken_date_does_not_use_up_a_protected_slot(self):
        runs = [dict(make_run(1, 1), created_at="kaputt")] + many(11, 40, first_id=100)
        self.assertEqual(ids(select(runs)), [110])

    def test_all_common_timestamp_formats_are_understood(self):
        base = NOW - dt.timedelta(days=60)
        for text in (base.strftime("%Y-%m-%dT%H:%M:%SZ"), base.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                     base.strftime("%Y-%m-%dT%H:%M:%S")):
            run = dict(make_run(1, 60), created_at=text)
            self.assertEqual(ids(select([run], keep_per_workflow=0)), [1], text)

    def test_times_with_another_offset_are_compared_correctly(self):
        local = (NOW - dt.timedelta(days=29, hours=23)).astimezone(dt.timezone(dt.timedelta(hours=2)))   # jünger als 30 Tage
        run = dict(make_run(1, 0), created_at=local.isoformat())
        self.assertEqual(select([run], keep_per_workflow=0), [])

    def test_ties_in_time_are_resolved_by_id_so_the_result_is_stable(self):
        same = [dict(make_run(i, 50), created_at="2026-08-01T10:00:00Z") for i in range(1, 13)]
        first = ids(select(same))
        self.assertEqual(first, ids(select(list(reversed(same)))))
        self.assertEqual(sorted(first), [1, 2])                              # bei Gleichstand gilt die größere ID als neuer

    def test_the_order_of_the_input_does_not_matter(self):
        runs = many(15, 31) + many(12, 100, workflow=2, first_id=100)
        expected = ids(select(runs))
        for seed in range(5):
            shuffled = list(runs)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(ids(select(shuffled)), expected)

    def test_runs_of_a_workflow_that_no_longer_exists_are_treated_like_any_other(self):
        runs = [dict(r, name=None, workflow_id=None) for r in many(12, 100)]
        self.assertEqual(len(select(runs)), 2)

    def test_the_input_is_not_modified(self):
        runs = many(15, 31)
        snapshot = [dict(r) for r in runs]
        select(runs)
        self.assertEqual(runs, snapshot)

    def test_empty_input(self):
        self.assertEqual(select([]), [])


class FetchTests(unittest.TestCase):
    @staticmethod
    def result(stdout="", returncode=0, stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_the_command_asks_for_all_pages_of_the_repository(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return self.result()

        prune_runs.fetch_runs("besitzer/repo", runner)
        self.assertEqual(calls, [["gh", "api", "--paginate", "repos/besitzer/repo/actions/runs?per_page=100",
                                  "--jq", prune_runs.LIST_JQ]])

    def test_one_json_object_per_line_becomes_one_run(self):
        out = '{"id": 1, "workflow_id": 7}\n\n{"id": 2, "workflow_id": 7}\n'
        self.assertEqual([r["id"] for r in prune_runs.fetch_runs("o/r", lambda *a, **k: self.result(out))], [1, 2])

    def test_no_runs_is_fine(self):
        self.assertEqual(prune_runs.fetch_runs("o/r", lambda *a, **k: self.result("")), [])

    def test_a_failing_cli_is_reported_with_its_message(self):
        runner = lambda *a, **k: self.result("", 1, "HTTP 403: Resource not accessible by integration")
        with self.assertRaisesRegex(RuntimeError, "konnten nicht gelesen werden: HTTP 403"):
            prune_runs.fetch_runs("o/r", runner)

    def test_garbage_from_github_is_refused_and_names_the_line(self):
        for bad in ("keine json\n", '{"id": 1}\nnoch ein fehler\n', "[1, 2]\n", '{"kein_id": 5}\n'):
            with self.assertRaisesRegex(RuntimeError, "Unerwartete Antwort von GitHub in Zeile"):
                prune_runs.fetch_runs("o/r", lambda *a, **k: self.result(bad))
        with self.assertRaisesRegex(RuntimeError, "Zeile 2"):
            prune_runs.fetch_runs("o/r", lambda *a, **k: self.result('{"id": 1}\nkaputt\n'))

    def test_a_missing_cli_is_reported_clearly(self):
        def runner(*a, **k):
            raise FileNotFoundError("gh")

        with self.assertRaisesRegex(RuntimeError, "GitHub-CLI \\(gh\\) wurde nicht gefunden"):
            prune_runs.fetch_runs("o/r", runner)


class DeleteTests(unittest.TestCase):
    def test_the_command_deletes_exactly_this_run(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.assertEqual(prune_runs.delete_run("besitzer/repo", 4711, runner), (True, ""))
        self.assertEqual(calls, [["gh", "api", "-X", "DELETE", "repos/besitzer/repo/actions/runs/4711"]])

    def test_a_failure_returns_the_reason(self):
        failing = lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "HTTP 409: Conflict\n")
        self.assertEqual(prune_runs.delete_run("o/r", 1, failing), (False, "HTTP 409: Conflict"))

    def test_the_reason_falls_back_to_stdout_and_then_to_the_return_code(self):
        self.assertEqual(prune_runs.delete_run("o/r", 1, lambda c, **k: subprocess.CompletedProcess(c, 1, "nur stdout", "")),
                         (False, "nur stdout"))
        self.assertEqual(prune_runs.delete_run("o/r", 1, lambda c, **k: subprocess.CompletedProcess(c, 7, "", "")),
                         (False, "Rückgabecode 7"))


class FakeGh:
    """Ersetzt subprocess.run: liefert die Läufe und merkt sich, welche gelöscht wurden."""

    def __init__(self, runs, fail_ids=(), list_failure=None):
        self.runs = runs
        self.fail_ids = set(fail_ids)
        self.list_failure = list_failure
        self.deleted = []
        self.calls = []

    def __call__(self, cmd, **kwargs):
        import json
        self.calls.append(cmd)
        if "--paginate" in cmd:
            if self.list_failure:
                return subprocess.CompletedProcess(cmd, 1, "", self.list_failure)
            return subprocess.CompletedProcess(cmd, 0, "\n".join(json.dumps(r) for r in self.runs) + "\n", "")
        run_id = int(cmd[-1].rsplit("/", 1)[1])
        if run_id in self.fail_ids:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 403: Forbidden")
        self.deleted.append(run_id)
        return subprocess.CompletedProcess(cmd, 0, "", "")


class MainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.summary = os.path.join(tmp.name, "summary.md")
        env = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": self.summary}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GITHUB_RUN_ID", None)
        self.sleeps = []

    def run_main(self, gh, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prune_runs.main(["--repo", "o/r", *args], now=NOW, runner=gh, sleep=self.sleeps.append)
        return code, out.getvalue(), err.getvalue()

    def summary_text(self):
        with open(self.summary, encoding="utf-8") as f:
            return f.read()

    def test_a_dry_run_deletes_nothing_but_shows_what_would_go(self):
        gh = FakeGh(many(15, 31))
        code, out, err = self.run_main(gh, "--dry-run")
        self.assertEqual((code, gh.deleted), (0, []))
        self.assertTrue(all("DELETE" not in call for call in gh.calls))
        self.assertIn("Testlauf (es wird nichts gelöscht): 15 Läufe gefunden, 5 davon dürfen weg", out)
        self.assertEqual(out.count("würde löschen:"), 5)
        self.assertIn("Testlauf: 5 würden gelöscht, es wurde nichts gelöscht", self.summary_text())

    def test_a_real_run_deletes_the_selected_runs_oldest_first(self):
        gh = FakeGh(many(15, 31))
        code, out, _ = self.run_main(gh)
        self.assertEqual((code, gh.deleted), (0, [15, 14, 13, 12, 11]))
        self.assertIn("Löschen: 15 Läufe gefunden, 5 davon dürfen weg", out)
        self.assertEqual(out.count("gelöscht:"), 5)
        self.assertIn("- Gelöscht: 5", self.summary_text())

    def test_nothing_that_is_young_or_unfinished_is_ever_deleted(self):
        runs = many(12, 1) + [make_run(500, 400, status="in_progress"), make_run(501, 400, status="queued")]
        gh = FakeGh(runs)
        code, out, _ = self.run_main(gh)
        self.assertEqual((code, gh.deleted), (0, []))
        self.assertIn("0 davon dürfen weg", out)

    def test_one_failure_does_not_stop_the_others_and_makes_the_job_fail(self):
        gh = FakeGh(many(15, 31), fail_ids=[13])
        code, out, err = self.run_main(gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.deleted, [15, 14, 12, 11])
        self.assertIn("FEHLER bei", err)
        self.assertIn("HTTP 403: Forbidden", err)
        summary = self.summary_text()
        self.assertIn("- Gelöscht: 4", summary)
        self.assertIn("- Nicht gelöscht (Fehler): 1", summary)

    def test_the_limit_is_respected_and_the_rest_is_announced(self):
        gh = FakeGh(many(15, 31))
        code, out, _ = self.run_main(gh, "--max-deletions", "2")
        self.assertEqual((code, gh.deleted), (0, [15, 14]))
        self.assertIn("Noch 3 Lauf/Läufe warten auf den nächsten Aufruf (Grenze: 2 je Aufruf)", out)
        self.assertIn("- Noch offen für den nächsten Aufruf: 3", self.summary_text())

    def test_a_limit_of_zero_deletes_nothing(self):
        gh = FakeGh(many(15, 31))
        self.assertEqual(self.run_main(gh, "--max-deletions", "0")[0], 0)
        self.assertEqual(gh.deleted, [])

    def test_the_default_thresholds_are_30_days_and_the_newest_10(self):
        gh = FakeGh(many(11, 31) + many(5, 1, first_id=100))                # 11 alte (31 bis 41 Tage), 5 junge (1 bis 5 Tage)
        self.run_main(gh)
        # Die zehn neuesten bleiben: die fünf jungen und die fünf neuesten alten. Die sechs übrigen alten gehen.
        self.assertEqual(gh.deleted, [11, 10, 9, 8, 7, 6])

    def test_the_30_day_limit_is_what_separates_the_two_runs_beyond_the_newest_10(self):
        gh = FakeGh(many(10, 1) + [make_run(100, 29), make_run(101, 31)])      # beide liegen außerhalb der zehn neuesten
        self.run_main(gh)
        self.assertEqual(gh.deleted, [101])                                  # 31 Tage alt geht, 29 Tage alt bleibt

    def test_the_thresholds_can_be_changed(self):
        gh = FakeGh(many(8, 5))
        self.run_main(gh, "--keep-days", "3", "--keep-per-workflow", "5")
        self.assertEqual(gh.deleted, [8, 7, 6])

    def test_the_running_job_is_excluded_via_the_environment(self):
        gh = FakeGh(many(15, 31))
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "15"}):
            self.run_main(gh)
        self.assertEqual(gh.deleted, [14, 13, 12, 11])

    def test_the_argument_beats_the_environment(self):
        gh = FakeGh(many(15, 31))
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "15"}):
            self.run_main(gh, "--current-run-id", "14")
        self.assertEqual(gh.deleted, [15, 13, 12, 11])

    def test_a_run_id_in_the_environment_that_is_not_a_number_is_ignored(self):
        gh = FakeGh(many(15, 31))
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "abc"}):
            self.assertEqual(self.run_main(gh)[0], 0)
        self.assertEqual(len(gh.deleted), 5)

    def test_a_read_error_stops_everything_without_deleting_anything(self):
        gh = FakeGh(many(15, 31), list_failure="HTTP 401: Bad credentials")
        code, out, err = self.run_main(gh)
        self.assertEqual((code, gh.deleted), (2, []))
        self.assertIn("Fehler: Die Läufe konnten nicht gelesen werden: HTTP 401", err)

    def test_negative_values_are_refused(self):
        for args in (["--keep-days", "-1"], ["--keep-per-workflow", "-1"], ["--max-deletions", "-5"]):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                prune_runs.main(["--repo", "o/r", *args], now=NOW, runner=FakeGh([]), sleep=self.sleeps.append)
            self.assertEqual(caught.exception.code, 2)

    def test_the_repository_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            prune_runs.main([], now=NOW, runner=FakeGh([]))

    def test_it_pauses_briefly_after_every_real_deletion_but_not_in_a_dry_run(self):
        gh = FakeGh(many(15, 31))
        self.run_main(gh, "--dry-run")
        self.assertEqual(self.sleeps, [])
        self.run_main(gh)
        self.assertEqual(self.sleeps, [0.1] * 5)

    def test_the_summary_is_appended_and_not_replaced(self):
        with open(self.summary, "w", encoding="utf-8") as f:
            f.write("### Vorher\n")
        self.run_main(FakeGh(many(15, 31)), "--dry-run")
        text = self.summary_text()
        self.assertTrue(text.startswith("### Vorher\n"))
        self.assertIn("### Aufräumen der Workflow-Läufe", text)

    def test_without_a_summary_file_nothing_breaks(self):
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        self.assertEqual(self.run_main(FakeGh(many(15, 31)), "--dry-run")[0], 0)

    def test_several_workflows_are_handled_independently(self):
        runs = many(12, 100, workflow=1, first_id=1) + many(12, 200, workflow=2, first_id=100) + many(3, 100, workflow=3, first_id=200)
        gh = FakeGh(runs)
        self.run_main(gh)
        self.assertEqual(sorted(gh.deleted), [11, 12, 110, 111])


FAKE_GH = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if "--paginate" in args:
    sys.stdout.write(open(os.environ["FAKE_GH_RUNS"], encoding="utf-8").read())
    sys.exit(0)
if args[:3] == ["api", "-X", "DELETE"]:
    run_id = args[3].rsplit("/", 1)[1]
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
        log.write(run_id + "\\n")
    if run_id in os.environ.get("FAKE_GH_FAIL", "").split(","):
        sys.stderr.write("HTTP 403: Forbidden\\n")
        sys.exit(1)
    sys.exit(0)
sys.stderr.write("unerwarteter Aufruf: %r\\n" % (args,))
sys.exit(9)
"""


class RealProcessTests(unittest.TestCase):
    """Mit einer nachgebauten 'gh' als echtem Programm: Hier läuft der Aufruf über subprocess, wie auf dem Runner."""

    def setUp(self):
        import json
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        gh = os.path.join(tmp.name, "gh")
        with open(gh, "w", encoding="utf-8") as f:
            f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        self.log = os.path.join(tmp.name, "geloescht.txt")
        self.runs = os.path.join(tmp.name, "laeufe.jsonl")
        with open(self.runs, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in many(15, 31)) + "\n")
        env = patch.dict(os.environ, {"PATH": tmp.name + os.pathsep + os.environ["PATH"], "FAKE_GH_RUNS": self.runs,
                                      "FAKE_GH_LOG": self.log, "FAKE_GH_FAIL": ""})
        env.start()
        self.addCleanup(env.stop)

    def deleted(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [int(line) for line in f.read().split()]

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prune_runs.main(["--repo", "o/r", *args], now=NOW, sleep=lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def test_the_whole_flow_through_a_real_process(self):
        code, out, _ = self.run_main()
        self.assertEqual((code, self.deleted()), (0, [15, 14, 13, 12, 11]))

    def test_a_dry_run_through_a_real_process_deletes_nothing(self):
        code, out, _ = self.run_main("--dry-run")
        self.assertEqual((code, self.deleted()), (0, []))
        self.assertIn("würde löschen:", out)

    def test_a_failing_deletion_through_a_real_process(self):
        os.environ["FAKE_GH_FAIL"] = "13"
        code, _, err = self.run_main()
        self.assertEqual((code, self.deleted()), (1, [15, 14, 13, 12, 11]))        # alle versucht, 13 abgelehnt
        self.assertIn("HTTP 403: Forbidden", err)

    def test_a_missing_gh_is_reported_and_nothing_happens(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, empty)
        os.environ["PATH"] = empty
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("GitHub-CLI (gh) wurde nicht gefunden", err)

    def test_the_script_can_be_started_directly_like_the_workflow_does(self):
        result = subprocess.run(["python3", SCRIPT, "--repo", "o/r", "--dry-run"], capture_output=True, text=True,
                                env=dict(os.environ, GITHUB_STEP_SUMMARY=""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Testlauf (es wird nichts gelöscht)", result.stdout)
        self.assertEqual(self.deleted(), [])


if __name__ == "__main__":
    unittest.main()
