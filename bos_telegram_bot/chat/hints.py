"""Hinweise nach dem Anlegen eines Abos (Schutz vor Tippfehlern)."""

import html
import re

from bos_telegram_bot.chat.common import find_entry, get_db_path
from bos_telegram_bot.core.matching import candidate_values, match_sub
from bos_telegram_bot.storage import knowledge


def _known_hint(db_path: str, entry: dict, side: str, target: str, is_regex: bool):
    """Prüft das Abo gegen alle bekannten Werte (Liste + gelernte Paare)."""
    rows = knowledge.known_rows(db_path, entry)
    plain = [r for r in rows if not r["isRegex"]]
    if not plain:
        return None  # nichts bekannt, also auch nichts zu vergleichen

    if is_regex:
        rx = re.compile(target)
        hits = [r for r in plain if rx.match(r[side])]
        if hits:
            n = len(hits)
            names = ", ".join(html.escape(r["add"]) for r in hits[:3])
            more = f" (+{n - 3} weitere)" if n > 3 else ""
            count = "1 bekannten Eintrag" if n == 1 else f"{n} bekannte Einträge"
            return f"✅ Trifft {count}: {names}{more}"
        note = " Wache-Muster der Liste werden dabei nicht geprüft." if any(r["isRegex"] for r in rows) else ""
        return "ℹ️ Dazu ist kein passender Eintrag bekannt (weder in der Liste noch aus bisherigen Alarmen)." + note

    if side == "for":
        if knowledge.resolve_name(entry, db_path, target):
            return None
    elif any(r["add"] == target for r in plain):
        return None
    return ("ℹ️ Diesen Wert kenne ich noch nicht (weder aus der Liste noch aus bisherigen Alarmen). "
            "Prüfe die Schreibweise.")


def _last_alarm_hint(context, field_key: str, target: str, is_regex: bool):
    payload = context.bot_data.get("last_payload")
    if not payload:
        return None
    values, raw_list = candidate_values(payload, field_key)
    if not values:
        return "ℹ️ Der letzte Alarm enthält dieses Feld nicht."
    ok = match_sub({"target": target, "is_regex": is_regex, "alias": ""}, values, raw_list) is not None
    if ok:
        return "✅ Passt auf den letzten Alarm."
    return "ℹ️ Passt nicht auf den letzten Alarm (normal, wenn er etwas anderes betraf)."


def check_hints(context, field_key: str, target: str, is_regex: bool) -> list:
    """Hinweise nach dem Anlegen eines Abos. Das Abo wird in jedem Fall angelegt,
    die Hinweise sind nur dazu da, Tippfehler aufzudecken."""
    hints = []
    entry, side = find_entry(context, field_key)
    if entry and entry.get("add"):
        hints.append(_known_hint(get_db_path(context), entry, side, target, is_regex))
    hints.append(_last_alarm_hint(context, field_key, target, is_regex))
    return [h for h in hints if h]
