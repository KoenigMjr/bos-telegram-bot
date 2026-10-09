"""Abgleich von Abos mit Alarmwerten. Kein Telegram, keine Datenbank."""

import re

WILDCARD_CHARS = "*%?_"

# Eine Zahl, direkt gefolgt von '+': "diese Zahl und jede größere" (z.B. 'B 3+').
RANGE_TOKEN = re.compile(r"(\d+)\+")


def has_wildcard_syntax(pattern: str) -> bool:
    """True, wenn die Eingabe Platzhalter-Zeichen oder ein 're:'-Präfix enthält."""
    pattern = pattern.strip()
    return pattern.lower().startswith("re:") or any(ch in pattern for ch in WILDCARD_CHARS)


def has_range_syntax(pattern: str) -> bool:
    """True, wenn die Eingabe eine Zahl mit '+' enthält ('B 3+'). Bei 're:' gilt die
    Eingabe unverändert als Regex, dort ist '+' ein normaler Quantifizierer."""
    pattern = pattern.strip()
    return not pattern.lower().startswith("re:") and bool(RANGE_TOKEN.search(pattern))


def is_pattern_input(pattern: str) -> bool:
    """True, wenn die Eingabe ein Muster ist und nicht als fester Wert gilt."""
    return has_wildcard_syntax(pattern) or has_range_syntax(pattern)


def _digit_class(lowest: int) -> str:
    """Zeichenklasse für alle Ziffern von 'lowest' bis 9."""
    if lowest == 9:
        return "9"
    return f"[{lowest}-9]"


def number_at_least_regex(digits: str) -> str:
    """Regex-Teil für alle Zahlen, die der angegebenen Zahl entsprechen oder größer sind.

    'digits' sind die getippten Ziffern. Ohne führende Null zählt der Zahlenwert:
    '3' trifft 3, 4, ... 9, 10, 11, ... aber nicht 1 oder 2. Mit führender Null
    (z.B. eine RIC wie '0230100') wird stellengenau in gleicher Länge verglichen.

    Der Ausdruck erkennt eine Zahl auch am Anfang einer längeren Ziffernfolge. Das ist
    richtig, denn jede Verlängerung ist größer als die Zahl selbst. Deshalb braucht es
    keine Wortgrenze."""
    if digits == "0":
        return r"\d+"

    length = len(digits)
    padded = length > 1 and digits.startswith("0")

    alternatives = []
    if not padded:
        # Mehr Stellen als die Eingabe: immer größer.
        alternatives.append(rf"[1-9]\d{{{length},}}")

    # Gleich viele Stellen: an der ersten Stelle, die größer ist, darf der Rest beliebig sein.
    for i, ch in enumerate(digits):
        if ch == "9":
            continue
        rest = length - i - 1
        tail = "" if rest == 0 else (r"\d" if rest == 1 else rf"\d{{{rest}}}")
        alternatives.append(digits[:i] + _digit_class(int(ch) + 1) + tail)
    alternatives.append(digits)  # genau diese Zahl
    return "(?:" + "|".join(alternatives) + ")"


def _escape_with_wildcards(text: str) -> str:
    """Escaped Sonderzeichen, lässt aber die Platzhalter *, %, ?, _ als Muster stehen."""
    star_token, dot_token = "WCSTARTOKEN", "WCDOTTOKEN"
    tmp = text.replace("*", star_token).replace("%", star_token)
    tmp = tmp.replace("?", dot_token).replace("_", dot_token)
    return re.escape(tmp).replace(re.escape(star_token), ".*").replace(re.escape(dot_token), ".")


def _translate(pattern: str) -> str:
    """Wandelt Platzhalter und 'Zahl+' in Regex-Bausteine um, alles andere wird wörtlich."""
    parts, pos = [], 0
    for m in RANGE_TOKEN.finditer(pattern):
        parts.append(_escape_with_wildcards(pattern[pos:m.start()]))
        parts.append(number_at_least_regex(m.group(1)))
        pos = m.end()
    parts.append(_escape_with_wildcards(pattern[pos:]))
    return "".join(parts)


def wildcard_to_regex(pattern: str, anchor=None) -> str:
    """Wandelt ein Glob/SQL-Wildcard-Muster in eine Regex um.

    - '*' und '%'  -> beliebig viele Zeichen
    - '?' und '_'  -> genau ein Zeichen
    - 'Zahl+'      -> diese Zahl und jede größere ('B 3+' trifft B 3, B 4, B 10, nicht B 2)
    - mit Platzhalter -> das Muster gilt für den ganzen Wert ('THL*' = beginnt mit THL,
      '301*' = beginnt mit 301, '*301*' = enthält 301)
    - sonst        -> 'enthält'-Suche (auch bei 'B 3+', wie bei 'B 3')
    - Groß-/Kleinschreibung ist bei diesen Mustern egal ('thl*' trifft 'THL ...')
    - 're:'-Präfix -> Rest wird als rohe Regex verwendet, genau wie eingegeben
      (kein Anchoring, Groß-/Kleinschreibung wird beachtet, außer mit '(?i)')

    'anchor' erzwingt (True) oder verbietet (False) das Anchoring auf den ganzen Wert.
    Ohne Angabe gilt: nur Platzhalter-Zeichen verankern.

    Wirft re.error, wenn das Ergebnis keine gültige Regex ist.
    """
    pattern = pattern.strip()
    if pattern.lower().startswith("re:"):
        raw = pattern[3:]
        re.compile(raw)
        return raw

    if anchor is None:
        anchor = has_wildcard_syntax(pattern)
    body = _translate(pattern)
    regex = f"(?i)^{body}$" if anchor else f"(?i).*{body}.*"
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
