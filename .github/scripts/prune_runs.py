#!/usr/bin/env python3
"""Räumt alte Workflow-Läufe (GitHub Actions) auf.

Gelöscht wird ein Lauf nur, wenn ALLE Bedingungen zutreffen:
  - er ist abgeschlossen (nie ein laufender, wartender oder angeforderter Lauf),
  - er ist älter als --keep-days Tage,
  - er gehört nicht zu den neuesten --keep-per-workflow Läufen seines Workflows
    (so bleibt bei selten laufenden Workflows immer etwas Verlauf),
  - er ist nicht der Lauf, der dieses Skript gerade ausführt.

Mit --dry-run wird nichts gelöscht, es wird nur angezeigt, was gelöscht würde.
Mit --max-deletions wird pro Aufruf begrenzt (Ratenbegrenzung der GitHub-API), der Rest folgt beim nächsten Mal.

Aufgerufen wird die GitHub-CLI (gh), die auf den Runnern von GitHub vorinstalliert ist. Sie liest das Token aus
der Umgebungsvariable GH_TOKEN. Das Löschen eines Laufs entfernt auch sein Log und seine Artefakte."""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

# Alles, was nicht 'completed' ist, läuft noch oder wartet: Das fassen wir nie an.
FINISHED = "completed"

# gh api --paginate liefert je Seite ein Objekt. --jq macht daraus eine Zeile (JSON) pro Lauf.
LIST_JQ = ".workflow_runs[] | {id, workflow_id, name, created_at, status, conclusion}"


def parse_time(value):
    """ISO-Zeitstempel von GitHub ('2026-10-12T09:23:51Z') als UTC-Zeit, None wenn nicht lesbar."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def select_runs_to_delete(runs, now, keep_days, keep_per_workflow, current_run_id=None, limit=None):
    """Wählt die Läufe aus, die gelöscht werden dürfen. Älteste zuerst, höchstens limit Stück.

    Ein Lauf ohne lesbares Datum wird nie ausgewählt: Im Zweifel bleibt er."""
    cutoff = now - dt.timedelta(days=keep_days)
    by_workflow = {}
    for run in runs:
        created = parse_time(run.get("created_at"))
        if created is not None:
            by_workflow.setdefault(run.get("workflow_id"), []).append((created, run["id"], run))

    candidates = []
    for items in by_workflow.values():
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)        # neueste zuerst
        for position, (created, run_id, run) in enumerate(items):
            if position < keep_per_workflow:
                continue                                                      # gehört zu den neuesten: bleibt
            if run.get("status") != FINISHED or created >= cutoff or run_id == current_run_id:
                continue
            candidates.append((created, run_id, run))

    candidates.sort(key=lambda item: (item[0], item[1]))                      # älteste zuerst
    selected = [run for _, _, run in candidates]
    return selected if limit is None else selected[:max(limit, 0)]


def fetch_runs(repo, runner=subprocess.run):
    """Alle Läufe des Repositories über die GitHub-CLI. Wirft RuntimeError, wenn das nicht klappt."""
    try:
        result = runner(["gh", "api", "--paginate", f"repos/{repo}/actions/runs?per_page=100", "--jq", LIST_JQ],
                        capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("Die GitHub-CLI (gh) wurde nicht gefunden") from None
    if result.returncode != 0:
        raise RuntimeError(f"Die Läufe konnten nicht gelesen werden: {result.stderr.strip() or result.stdout.strip()}")
    runs = []
    for number, line in enumerate(result.stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError(f"Unerwartete Antwort von GitHub in Zeile {number}: {line[:80]!r}") from None
        if not isinstance(run, dict) or "id" not in run:
            raise RuntimeError(f"Unerwartete Antwort von GitHub in Zeile {number}: {line[:80]!r}")
        runs.append(run)
    return runs


def delete_run(repo, run_id, runner=subprocess.run):
    """Löscht einen Lauf. Gibt (True, '') oder (False, Fehlertext) zurück."""
    result = runner(["gh", "api", "-X", "DELETE", f"repos/{repo}/actions/runs/{run_id}"], capture_output=True, text=True)
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr.strip() or result.stdout.strip() or f"Rückgabecode {result.returncode}")


def describe(run):
    created = (run.get("created_at") or "")[:10]
    return f"{run.get('name') or 'Workflow ' + str(run.get('workflow_id'))} (Lauf {run['id']}, {created})"


def write_summary(lines):
    """Hängt die Zusammenfassung an die Seite des Laufs an, wenn GitHub dafür eine Datei bereitstellt."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv=None, now=None, runner=subprocess.run, sleep=time.sleep):
    parser = argparse.ArgumentParser(description="Räumt alte Workflow-Läufe auf.")
    parser.add_argument("--repo", required=True, help="Besitzer/Repository")
    parser.add_argument("--keep-days", type=float, default=30, help="Läufe jünger als so viele Tage bleiben (30)")
    parser.add_argument("--keep-per-workflow", type=int, default=10, help="so viele neueste Läufe je Workflow bleiben (10)")
    parser.add_argument("--max-deletions", type=int, default=400, help="höchstens so viele Löschungen je Aufruf (400)")
    parser.add_argument("--current-run-id", type=int, default=None, help="der laufende Lauf (Standard: GITHUB_RUN_ID)")
    parser.add_argument("--dry-run", action="store_true", help="nichts löschen, nur anzeigen")
    args = parser.parse_args(argv)
    if args.keep_days < 0 or args.keep_per_workflow < 0 or args.max_deletions < 0:
        parser.error("Die Werte dürfen nicht negativ sein")

    now = now or dt.datetime.now(dt.timezone.utc)
    current = args.current_run_id
    if current is None and os.environ.get("GITHUB_RUN_ID", "").isdigit():
        current = int(os.environ["GITHUB_RUN_ID"])

    try:
        runs = fetch_runs(args.repo, runner)
    except RuntimeError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 2

    # Die Auswahl ohne Begrenzung: So lässt sich sagen, wie viel noch übrig bleibt.
    everything = select_runs_to_delete(runs, now, args.keep_days, args.keep_per_workflow, current)
    chosen = everything[:args.max_deletions]
    mode = "Testlauf (es wird nichts gelöscht)" if args.dry_run else "Löschen"
    print(f"{mode}: {len(runs)} Läufe gefunden, {len(everything)} davon dürfen weg "
          f"(älter als {args.keep_days:g} Tage, nicht unter den neuesten {args.keep_per_workflow} je Workflow).")

    deleted, failed = [], []
    for run in chosen:
        if args.dry_run:
            print(f"  würde löschen: {describe(run)}")
            continue
        ok, reason = delete_run(args.repo, run["id"], runner)
        if ok:
            deleted.append(run)
            print(f"  gelöscht: {describe(run)}")
        else:
            failed.append(run)
            print(f"  FEHLER bei {describe(run)}: {reason}", file=sys.stderr)
        sleep(0.1)                                                           # schont die Ratenbegrenzung

    remaining = len(everything) - len(chosen)
    if remaining:
        print(f"Noch {remaining} Lauf/Läufe warten auf den nächsten Aufruf (Grenze: {args.max_deletions} je Aufruf).")
    summary = ["### Aufräumen der Workflow-Läufe", f"- Gefunden: {len(runs)}"]
    if args.dry_run:
        summary.append(f"- Testlauf: {len(chosen)} würden gelöscht, es wurde nichts gelöscht")
    else:
        summary.append(f"- Gelöscht: {len(deleted)}")
        summary.append(f"- Nicht gelöscht (Fehler): {len(failed)}")
    summary.append(f"- Noch offen für den nächsten Aufruf: {remaining}")
    write_summary(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
