"""Abgleich von Abos mit Alarmwerten. Kein Telegram, keine Datenbank."""

import re

WILDCARD_CHARS = "*%?_"


def has_wildcard_syntax(pattern: str) -> bool:
    """True, wenn die Eingabe ein Muster ist (Wildcard-Zeichen oder 're:'-Präfix)
    und nicht als fester Wert verstanden werden soll."""
    pattern = pattern.strip()
    return pattern.lower().startswith("re:") or any(ch in pattern for ch in WILDCARD_CHARS)


def wildcard_to_regex(pattern: str) -> str:
    """Wandelt ein Glob/SQL-Wildcard-Muster in eine Regex um.

    - '*' und '%'  -> beliebig viele Zeichen
    - '?' und '_'  -> genau ein Zeichen
    - mit Wildcard -> das Muster gilt für den ganzen Wert ('THL*' = beginnt mit THL,
      '301*' = beginnt mit 301, '*301*' = enthält 301)
    - ohne Wildcard -> 'enthält'-Suche
    - Groß-/Kleinschreibung ist bei Platzhalter-Mustern egal ('thl*' trifft 'THL ...')
    - 're:'-Präfix -> Rest wird als rohe Regex verwendet, genau wie eingegeben
      (kein Anchoring, Groß-/Kleinschreibung wird beachtet, außer mit '(?i)')

    Wirft re.error, wenn das Ergebnis keine gültige Regex ist.
    """
    pattern = pattern.strip()
    if pattern.lower().startswith("re:"):
        raw = pattern[3:]
        re.compile(raw)
        return raw

    star_token, dot_token = "WCSTARTOKEN", "WCDOTTOKEN"
    tmp = pattern.replace("*", star_token).replace("%", star_token)
    tmp = tmp.replace("?", dot_token).replace("_", dot_token)
    escaped = re.escape(tmp).replace(re.escape(star_token), ".*").replace(re.escape(dot_token), ".")
    body = f"^{escaped}$" if has_wildcard_syntax(pattern) else f".*{escaped}.*"
    regex = f"(?i){body}"
    re.compile(regex)
    return regex


def split_list(raw) -> list:
    """BOSWatch3 verbindet die Einzelwerte eines Multicast-Alarms in den
    *_list-Feldern mit ', ' (z.B. ric_list = '1000001, 1000002'). Leere
    Einträge werden verworfen."""
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(", ") if part.strip()]


def split_list_aligned(raw) -> list:
    """Wie split_list, behält aber leere Einträge, damit zwei Listen
    (z.B. ric_list und description_list) Position für Position zusammenpassen."""
    if raw is None or str(raw) == "":
        return []
    return [part.strip() for part in str(raw).split(", ")]


def candidate_values(payload: dict, key: str):
    """Alle Werte eines Feldes, gegen die ein Abo geprüft wird: der eigene Wert
    plus die Werte aus '<key>_list'.

    Bei Multicast-Alarmen fasst BOSWatch3 mehrere Empfänger zu einem Paket
    zusammen. 'ric' und 'description' enthalten dann nur einen davon, alle
    stehen in 'ric_list' bzw. 'description_list'. Ohne diese Liste würde ein
    Abo auf einen der anderen Empfänger nie auslösen.

    Rückgabe: (Werteliste, roher Listenstring)."""
    values = []
    primary = payload.get(key)
    if primary is not None:
        values.append(str(primary))
    raw_list = payload.get(f"{key}_list")
    raw_list = "" if raw_list is None else str(raw_list)
    for part in split_list(raw_list):
        if part not in values:
            values.append(part)
    return values, raw_list


def match_sub(sub, values: list, raw_list: str):
    """Liefert den (ggf. aufgelösten) Anzeigenamen, wenn das Abo auf einen der
    Werte passt, sonst None. 'sub' braucht die Schlüssel target, is_regex, alias."""
    target = sub["target"]

    if sub["is_regex"]:
        for value in values:
            m = re.match(target, value)
            if m:
                try:
                    # Kein zusätzliches Backslash-Escaping: m.expand() erwartet
                    # \1 / \g<name> genau so, wie es in der CSV steht.
                    return m.expand(sub["alias"])
                except (re.error, IndexError):
                    return sub["alias"]
        return None

    if target in values:
        return sub["alias"]
    # Ein Wert, der selbst ', ' enthält, zerfällt beim Aufteilen der Liste.
    # Deshalb zusätzlich gegen den Rohstring prüfen (mit Trennern umrahmt).
    if raw_list and f", {target}, " in f", {raw_list}, ":
        return sub["alias"]
    return None
