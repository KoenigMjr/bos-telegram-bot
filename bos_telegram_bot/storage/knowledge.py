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


def load_csv(csv_path: str) -> list:
    """Lädt eine CSV im BOSWatch3-Descriptor-Schema (for,add,isRegex).
    Parsing bewusst identisch zu module/descriptor.py in BW3-Core gehalten,
    damit dieselbe Datei ohne Anpassung von beiden Systemen gelesen werden
    kann (inkl. Toleranz für fehlende isRegex-Spalte)."""
    if not csv_path:
        return []
    if not os.path.isfile(csv_path):
        log.warning("CSV nicht gefunden, es werden nur gelernte Werte genutzt: %s", csv_path)
        return []

    rows = []
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
                    log.warning("CSV %s, Zeile %d übersprungen (ungültiger Regex '%s'): %s", csv_path, row_num, clean_for, e)
                    continue

            rows.append({"for": clean_for, "add": (row.get("add") or "").strip(), "isRegex": is_regex})
    log.info("CSV geladen: %d Einträge (%s)", len(rows), csv_path)
    return rows


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
