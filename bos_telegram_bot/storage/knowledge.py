"""Bekannte Wertepaare (z.B. RIC -> Beschreibung).

Quellen: eine optionale CSV im BOSWatch3-Descriptor-Format (nur gelesen, nie
verändert) und die Paare, die der Bot aus ankommenden Alarmen lernt (in der
eigenen SQLite-Datenbank). Die CSV hat bei gleichem Wert Vorrang."""

import csv
import logging
import os
import re

from bos_telegram_bot.core.matching import split_list_aligned
from bos_telegram_bot.storage import database as db

log = logging.getLogger(__name__)


def load_csv(csv_path: str, field: str = None) -> list:
    """Lädt eine CSV im BOSWatch3-Descriptor-Schema (for,add,isRegex).
    Parsing bewusst identisch zu module/descriptor.py in BW3-Core gehalten,
    damit dieselbe Datei ohne Anpassung von beiden Systemen gelesen werden
    kann (inkl. Toleranz für fehlende isRegex-Spalte).

    field ist nur für das Log: Es nennt, zu welchem Feld die CSV gehört."""
    label = f" (Feld {field})" if field else ""
    if not csv_path:
        return []
    if not os.path.isfile(csv_path):
        log.warning("CSV nicht gefunden%s, es werden nur gelernte Werte genutzt: %s", label, csv_path)
        return []

    rows = []
    skipped = 0
    with open(csv_path, "r", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row_num, row in enumerate(reader, start=2):  # Zeile 1 = Header
            raw_for = str(row.get("for", "")).strip()
            clean_for = raw_for.strip().strip('"').strip("'")
            if not clean_for:
                continue

            is_regex = (row.get("isRegex") or "false").strip().lower() == "true"
            if is_regex:
                try:
                    re.compile(clean_for)
                except re.error as e:
                    skipped += 1
                    log.warning("CSV %s, Zeile %d übersprungen (ungültiger Regex '%s'): %s", csv_path, row_num, clean_for, e)
                    continue

            rows.append({"for": clean_for, "add": (row.get("add") or "").strip(), "isRegex": is_regex})
    details = []
    patterns = sum(1 for row in rows if row["isRegex"])
    if patterns:
        details.append(f"{patterns} Muster")
    if skipped:
        details.append(f"{skipped} übersprungen")
    count = f"{len(rows)} Datensatz" if len(rows) == 1 else f"{len(rows)} Datensätze"
    log.info("CSV eingelesen%s: %s%s aus %s", label, count,
             f" ({', '.join(details)})" if details else "", os.path.abspath(csv_path))
    return rows


def _csv_hint(name: str, entries: list) -> str:
    """Der wahrscheinlichste Grund, warum eine CSV im Datenordner nicht zugeordnet wurde, und was zu tun ist."""
    fields = [e["for"] for e in entries if e.get("add")]
    fields_text = ("vorhanden: " + ", ".join(fields)) if fields else "kein Feld hat eine Namenssuche ('add:')"
    match = re.fullmatch(r"descriptions_(.+)\.csv", name, flags=re.IGNORECASE)
    if match:
        wanted = match.group(1)
        entry = next((e for e in entries if e.get("add") and e["for"].lower() == wanted.lower()), None)
        if entry is None:
            return f"Es gibt kein Feld '{wanted}' mit Namenssuche ({fields_text})."
        expected = f"descriptions_{entry['for']}.csv"
        if name != expected:
            return f"Der Name muss genau '{expected}' lauten (Kleinschreibung, Endung .csv), dann wird sie automatisch erkannt."
        if entry.get("csv"):
            return (f"Für Feld '{entry['for']}' ist stattdessen {entry['csv']} eingestellt "
                    f"(CSV_PATH_{entry['for'].upper()} oder 'csv:' in der Konfiguration).")
    return (f"Zuordnen: in 'descriptions_<Feld>.csv' umbenennen ({fields_text}), in data/config.yaml "
            f"bei dem Feld 'csv:' eintragen oder CSV_PATH_<FELD> setzen.")


def find_unassigned_csvs(data_dir: str, entries: list) -> list:
    """Sucht im Datenordner nach CSV-Dateien, die kein Feld verwendet, und warnt für jede.

    Eine CSV liegt dort meist, weil jemand sie einbinden wollte, aber die Zuordnung fehlt (falscher Name,
    Variable vergessen, eine andere CSV hat Vorrang). Der Bot würde sie sonst stillschweigend ignorieren.
    Gesucht wird nur direkt im Ordner. Liefert die absoluten Pfade der nicht zugeordneten Dateien."""
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return []
    used = {os.path.realpath(e["csv"]) for e in entries if e.get("csv")}
    unassigned = []
    for name in names:
        path = os.path.join(data_dir, name)
        if not name.lower().endswith(".csv") or not os.path.isfile(path) or os.path.realpath(path) in used:
            continue
        unassigned.append(os.path.abspath(path))
        log.warning("CSV %s liegt im Datenordner, wird aber von keinem Feld verwendet und deshalb nicht gelesen. %s",
                    os.path.abspath(path), _csv_hint(name, entries))
    if not unassigned:
        log.debug("Datenordner %s: keine nicht zugeordnete CSV", os.path.abspath(data_dir))
    return unassigned


def csv_covers(csv_rows: list, value: str):
    """Liefert die CSV-Zeile, die den Wert abdeckt (exakt vor Regex), sonst None."""
    for row in csv_rows:
        if not row["isRegex"] and row["for"] == value:
            return row
    for row in csv_rows:
        if row["isRegex"] and re.match(row["for"], value):
            return row
    return None


def resolve_name(entry: dict, db_path: str, value: str):
    """Name zu einem Wert (z.B. zu einer RIC) aus CSV oder gelernten Paaren."""
    row = csv_covers(entry.get("csv_rows", []), value)
    if row:
        if row["isRegex"]:
            m = re.match(row["for"], value)
            try:
                return m.expand(row["add"])
            except (re.error, IndexError):
                return row["add"]
        return row["add"]
    return db.get_learned(db_path, entry["for"], value)


def known_rows(db_path: str, entry: dict) -> list:
    """Alle bekannten Zeilen: CSV plus gelernte Paare, die nicht schon die CSV abdeckt."""
    csv_rows = entry.get("csv_rows", [])
    rows = [dict(r, source="csv") for r in csv_rows]
    for learned in db.list_learned(db_path, entry["for"]):
        if csv_covers(csv_rows, learned["for_value"]):
            continue
        rows.append({"for": learned["for_value"], "add": learned["add_value"], "isRegex": False, "source": "learned"})
    return rows


def extract_pairs(payload: dict, for_key: str, add_key: str) -> list:
    """Wertepaare (for, add) eines Alarms: das Hauptpaar und bei Multicast die
    Paare aus den *_list-Feldern.

    Die Listenpaare zählen nur, wenn beide Listen gleich lang sind. Enthält ein
    Name selbst ', ', passen die Längen nicht mehr und die Zuordnung wäre
    verschoben, dann wird die Liste bewusst nicht verwendet."""
    pairs = []

    def add_pair(f, a):
        f, a = ("" if f is None else str(f)).strip(), ("" if a is None else str(a)).strip()
        if f and a and (f, a) not in pairs:
            pairs.append((f, a))

    add_pair(payload.get(for_key), payload.get(add_key))

    for_list = split_list_aligned(payload.get(f"{for_key}_list"))
    add_list = split_list_aligned(payload.get(f"{add_key}_list"))
    if for_list and len(for_list) == len(add_list):
        for f, a in zip(for_list, add_list):
            add_pair(f, a)
    return pairs


def learn_from_payload(db_path: str, entries: list, payload: dict) -> int:
    """Trägt neue Paare aus einem Alarm ein bzw. aktualisiert den Namen.
    Übersprungen wird alles, was eine CSV-Zeile bereits abdeckt, und Einträge
    mit 'learn: false'. Liefert die Zahl der geschriebenen Paare."""
    count = 0
    for entry in entries:
        if not entry.get("add") or not entry.get("learn", True):
            continue
        csv_rows = entry.get("csv_rows", [])
        for for_value, add_value in extract_pairs(payload, entry["for"], entry["add"]):
            if csv_covers(csv_rows, for_value):
                continue
            db.upsert_learned(db_path, entry["for"], for_value, add_value)
            log.debug("Gelernt: %s %s = %s", entry["for"], for_value, add_value)
            count += 1
    return count
