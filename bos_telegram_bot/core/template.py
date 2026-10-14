"""Vorlage für die Alarm-Nachricht. Kein Telegram, keine Datenbank.

Platzhalter stehen in geschweiften Klammern und werden wie bei BOSWatch3 in
Großbuchstaben geschrieben. Beim Suchen des Feldes im Alarm ist die Schreibweise egal
('{SUBRICTEXT}' findet 'subricText').

  {FELD}           Wert eines Feldes aus dem Alarm, z.B. {MESSAGE}, {RIC}
  {FELD_LIST}      alle Werte eines Multicast-Alarms aus dem Feld 'feld_list', ein Wert pro
                   Zeile. Leere und doppelte Einträge entfallen. Gibt es keine Liste, gilt
                   der einzelne Wert.
  {MATCHED}        Name des passenden Abos
  {A|B}            Rückfall: der erste Platzhalter, der nicht leer ist,
                   z.B. {DESCRIPTION_LIST|RIC_LIST}
  {{ und }}        wörtliche geschweifte Klammern

Zeilen:
  - Sind alle Platzhalter einer Zeile leer, entfällt die Zeile. Mehrere leere Zeilen in
    Folge werden zu einer, am Anfang und Ende bleibt keine stehen.
  - Enthält eine Zeile genau eine {..._LIST}, wird die Zeile für jeden Eintrag wiederholt
    ('• {DESCRIPTION_LIST}' ergibt je Eintrag eine Zeile mit Aufzählungspunkt). Stehen mehrere
    Listen in einer Zeile, werden ihre Einträge mit ', ' verbunden.

Werte aus dem Alarm werden automatisch HTML-sicher gemacht. Der Text der Vorlage selbst gilt
als Telegram-HTML und wird beim Start geprüft."""

import html
import re

from bos_telegram_bot.core.matching import split_list

DEFAULT_TEMPLATE = """🚨 <b>BOS-ALARM</b> 🚨

{DESCRIPTION_LIST|RIC_LIST}
{MESSAGE}

<i>abonniert über: {MATCHED}</i>"""

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
PLACEHOLDER = re.compile(r"\{(" + _NAME + r"(?:\|" + _NAME + r")*)\}")
FIELD_NAME = re.compile(r"^" + _NAME + r"$")

# Von Telegram im HTML-Modus unterstützte Tags.
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
                "blockquote", "tg-spoiler", "span", "tg-emoji"}
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)([^<>]*)>")
_BAD_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")

# Eigene Templates pro Chat (Befehl /template): so lang darf der Text höchstens sein. Telegram erlaubt 4096 Zeichen
# je Nachricht, der Alarm selbst braucht auch Platz.
MAX_TEMPLATE_LENGTH = 1500

# Ein erfundener Beispiel-Alarm für die Vorschau. Er enthält die üblichen Felder von BOSWatch3 (auch die
# Multicast-Listen), damit sich fast jede Vorlage darauf ausprobieren lässt. Echte Alarme gehören nicht in
# eine Vorschau: Sie könnten eine Wache betreffen, die der Chat gar nicht abonniert hat.
SAMPLE_PAYLOAD = {
    "timestamp": "1700000000.5", "mode": "pocsag", "bitrate": "1200",
    "ric": "1000011", "subric": "1", "subricText": "a",
    "message": "THL 1 - Beispieltext, bitte nicht ernst nehmen",
    "description": "Wache Nord (Beispiel)",
    "clientName": "Beispiel-Client", "clientVersion": "3.0.0", "clientBranch": "develop", "inputSource": "sdr",
    "frequency": "123.450M", "clientIP": "192.0.2.10",
    "serverName": "Beispiel-Server", "serverVersion": "3.0.0", "serverBranch": "develop",
    "ric_list": "1000011, 1000043", "description_list": "Wache Nord (Beispiel), Fahrzeug 1 (Beispiel)",
    "message_list": ", ", "timestamp_list": "1700000000.5, 1700000000.8", "mode_list": "pocsag, pocsag",
    "subric_list": "1, 1", "subricText_list": "a, a", "bitrate_list": "1200, 1200",
    "multicastMode": "complete", "multicastRole": "recipient", "multicastSourceRic": "1000099",
    "multicastRecipientCount": "2", "multicastRecipientIndex": "1",
}
SAMPLE_MATCHED = ["RIC: 1000011"]

# Platzhalter für wörtliche Klammern, solange die Zeile ausgewertet wird.
_OPEN, _CLOSE = "\x00OPEN\x00", "\x00CLOSE\x00"


def template_from_fields(fields: list) -> str:
    """Vorlage für die ältere Einstellung 'notification_fields': je Feld eine Zeile."""
    lines = ["🚨 <b>BOS-ALARM</b> 🚨", ""]
    lines += [f"{{{field.upper()}_LIST}}" for field in fields]
    lines += ["", "<i>abonniert über: {MATCHED}</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- Prüfung

def html_problems(text: str) -> list:
    """Prüft Text auf gültiges Telegram-HTML und nennt alle Probleme."""
    problems, stack, leftover, pos = [], [], [], 0
    for mo in _TAG.finditer(text):
        leftover.append(text[pos:mo.start()])
        pos = mo.end()
        closing, name = mo.group(1) == "/", mo.group(2).lower()
        if name not in ALLOWED_TAGS:
            problems.append(f"HTML-Tag <{name}> kennt Telegram nicht (erlaubt: {', '.join(sorted(ALLOWED_TAGS))})")
        elif closing:
            if stack and stack[-1] == name:
                stack.pop()
            else:
                problems.append(f"</{name}> schließt nichts Offenes oder steht in falscher Reihenfolge")
        else:
            stack.append(name)
    leftover.append(text[pos:])
    rest = "".join(leftover)

    if "<" in rest or ">" in rest:
        problems.append("Ein '<' oder '>' steht nicht in einem Tag. Als Text bitte &lt; bzw. &gt; schreiben")
    if _BAD_AMPERSAND.search(rest):
        problems.append("Ein '&' steht nicht in einer HTML-Entität. Als Text bitte &amp; schreiben")
    problems += [f"<{name}> wird nicht geschlossen" for name in reversed(stack)]
    return problems


def validate_template(template) -> list:
    """Probleme einer Vorlage, leere Liste wenn sie in Ordnung ist."""
    if not isinstance(template, str):
        return ["muss ein Text sein. Bei Platzhaltern in YAML einen Block mit '|' oder Anführungszeichen verwenden"]
    if not template.strip():
        return ["ist leer"]

    static = template.replace("\r", "").replace("{{", "").replace("}}", "")
    static = PLACEHOLDER.sub("", static)
    problems = []
    for brace in ("{", "}"):
        if brace in static:
            problems.append(f"Die Klammer '{brace}' steht außerhalb eines Platzhalters. "
                            f"Ein Platzhalter sieht so aus: {{MESSAGE}}, eine wörtliche Klammer wird doppelt geschrieben")
    return problems + html_problems(static)


def custom_template_problems(template) -> list:
    """Probleme eines Templates, das ein User per Befehl setzen will. Leere Liste, wenn es in Ordnung ist.

    Zusätzlich zur Prüfung beim Start (validate_template) gilt: nicht zu lang, und mindestens ein Platzhalter,
    sonst käme eine Nachricht ohne jeden Inhalt aus dem Alarm."""
    problems = validate_template(template)
    if isinstance(template, str) and template.strip():
        if len(template) > MAX_TEMPLATE_LENGTH:
            problems.append(f"ist zu lang ({len(template)} Zeichen, erlaubt sind {MAX_TEMPLATE_LENGTH})")
        if not PLACEHOLDER.search(template.replace("{{", "").replace("}}", "")):
            problems.append("enthält keinen Platzhalter. Ohne z.B. {MESSAGE} oder {DESCRIPTION_LIST|RIC_LIST} "
                            "käme eine Nachricht ohne Inhalt aus dem Alarm")
    return problems


def unknown_placeholders(template: str, payload: dict) -> list:
    """Platzhalter, die in keinem Feld des Alarms vorkommen und deshalb immer leer blieben (z.B. ein Tippfehler).
    Bei 'A|B' zählt der Ausdruck nur, wenn KEINE Alternative bekannt ist. Reihenfolge wie im Text, ohne Doppelte."""
    known = {str(key).lower() for key in payload} | {"matched"}
    unknown = []
    for expression in dict.fromkeys(mo.group(1) for mo in PLACEHOLDER.finditer(template.replace("{{", "").replace("}}", ""))):
        def exists(name):
            key = name.lower()
            return key in known or (key.endswith("_list") and len(key) > 5 and key[:-5] in known)
        if not any(exists(name) for name in expression.split("|")):
            unknown.append(expression)
    return unknown


# ---------------------------------------------------------------- Ausgabe

def _clean(value) -> str:
    return "" if value is None else str(value).replace("\x00", "").strip()


def _entries(fields: dict, key: str) -> list:
    """Einträge einer Liste ('ric_list'), ohne leere und doppelte. Fehlt sie, gilt der einzelne Wert."""
    raw = fields.get(key)
    raw = "" if raw is None else str(raw).replace("\x00", "")   # nicht trimmen: ', ' ist der Trenner
    entries = []
    for part in split_list(raw):
        if part not in entries:
            entries.append(part)
    if not entries:
        single = _clean(fields.get(key[:-5]))
        if single:
            entries = [single]
    return entries


def _resolve(expression: str, fields: dict, matched: list):
    """('single', Text) oder ('list', Einträge) für einen Platzhalter, mit Rückfall bei 'A|B'."""
    result = ("single", "")
    for name in expression.split("|"):
        key = name.lower()
        if key == "matched":
            result = ("single", ", ".join(matched))
        elif key.endswith("_list") and len(key) > 5:
            result = ("list", _entries(fields, key))
        else:
            result = ("single", _clean(fields.get(key)))
        if result[1]:
            return result
    return result


def _as_text(resolved) -> str:
    kind, value = resolved
    return value if kind == "single" else ", ".join(value)


def _render_line(line: str, fields: dict, matched: list) -> list:
    line = line.replace("{{", _OPEN).replace("}}", _CLOSE)
    expressions = list(dict.fromkeys(mo.group(1) for mo in PLACEHOLDER.finditer(line)))
    if not expressions:
        return [line.replace(_OPEN, "{").replace(_CLOSE, "}")]

    resolved = {expr: _resolve(expr, fields, matched) for expr in expressions}
    if not any(_as_text(r) for r in resolved.values()):
        return []  # alle Platzhalter leer: Zeile entfällt

    texts = {expr: _as_text(r) for expr, r in resolved.items()}
    lists = [expr for expr, r in resolved.items() if r[0] == "list"]

    def fill(overrides=None):
        values = {**texts, **(overrides or {})}
        rendered = PLACEHOLDER.sub(lambda mo: html.escape(values[mo.group(1)]), line)
        return rendered.replace(_OPEN, "{").replace(_CLOSE, "}")

    if len(lists) == 1 and resolved[lists[0]][1]:
        return [fill({lists[0]: entry}) for entry in resolved[lists[0]][1]]
    return [fill()]


def _tidy(lines: list) -> str:
    """Mehrere leere Zeilen werden eine, am Anfang und Ende bleibt keine."""
    result = []
    for line in lines:
        if not line.strip() and (not result or not result[-1].strip()):
            continue
        result.append(line)
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)


def render(template: str, payload: dict, matched_aliases) -> str:
    """Setzt die Vorlage mit den Werten eines Alarms zusammen."""
    fields = {}
    for key, value in payload.items():
        fields.setdefault(str(key).lower(), value)
    matched = [str(alias) for alias in matched_aliases]

    lines = []
    for line in template.replace("\r", "").split("\n"):
        lines.extend(_render_line(line, fields, matched))
    text = _tidy(lines)
    if not text and template != DEFAULT_TEMPLATE:
        return render(DEFAULT_TEMPLATE, payload, matched_aliases)
    return text
