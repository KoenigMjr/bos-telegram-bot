"""Einstellungen für den Versand der Alarmnachrichten (Block 'sending:' der Konfiguration).

Rein und ohne Abhängigkeiten, damit die Konfiguration sie prüfen und der Sender sie verwenden kann."""

DEFAULTS = {
    "min_interval_seconds": 1.0,     # Mindestabstand zwischen zwei Nachrichten an denselben Chat
    "max_per_second": 25.0,          # Obergrenze über alle Chats zusammen (Telegram erlaubt etwa 30)
    "attempts": 3,                   # Versuche je Nachricht
    "retry_pause_seconds": [2.0, 5.0],   # Pause vor dem 2., 3., ... Versuch (die letzte gilt für alle weiteren)
    "max_wait_seconds": 120.0,       # so lange wird eine Nachricht höchstens wiederholt
    "timeout_seconds": 15.0,         # Zeitüberschreitung einer Anfrage an Telegram (Standard der Bibliothek: 5)
}

_NUMBER = "eine Zahl von 0 an"
_POSITIVE = "eine Zahl größer als 0"
_ATTEMPTS = "eine ganze Zahl von 1 bis 10"
_PAUSES = "eine Liste von Zahlen von 0 an (oder eine einzelne Zahl)"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid(key: str, value) -> bool:
    if key in ("min_interval_seconds", "max_wait_seconds"):
        return _is_number(value) and value >= 0
    if key in ("max_per_second", "timeout_seconds"):
        return _is_number(value) and value > 0
    if key == "attempts":
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10
    if key == "retry_pause_seconds":
        if _is_number(value):
            return value >= 0
        return isinstance(value, list) and len(value) > 0 and all(_is_number(v) and v >= 0 for v in value)
    return False


def _rule(key: str) -> str:
    return {"min_interval_seconds": _NUMBER, "max_wait_seconds": _NUMBER, "max_per_second": _POSITIVE,
            "timeout_seconds": _POSITIVE, "attempts": _ATTEMPTS, "retry_pause_seconds": _PAUSES}[key]


def problems(block) -> list:
    """Alle Probleme eines 'sending:'-Blocks als lesbare Sätze. Leere Liste, wenn alles in Ordnung ist."""
    if not isinstance(block, dict):
        return ["sending muss ein Block sein, z.B. 'sending:' mit 'attempts: 3' darunter"]
    found = []
    for key, value in block.items():
        if key not in DEFAULTS:
            found.append(f"sending.{key} ist unbekannt (erlaubt: {', '.join(DEFAULTS)})")
        elif not _valid(key, value):
            found.append(f"sending.{key} muss {_rule(key)} sein (ist '{value}')")
    return found


def normalized(block) -> dict:
    """Der vollständige Satz Einstellungen: fehlende Werte aus den Standardwerten, Zahlen als float,
    die Pausen immer als Liste. Setzt einen geprüften Block voraus."""
    block = block if isinstance(block, dict) else {}
    merged = {**DEFAULTS, **block}
    pauses = merged["retry_pause_seconds"]
    return {
        "min_interval_seconds": float(merged["min_interval_seconds"]),
        "max_per_second": float(merged["max_per_second"]),
        "attempts": int(merged["attempts"]),
        "retry_pause_seconds": [float(p) for p in (pauses if isinstance(pauses, list) else [pauses])],
        "max_wait_seconds": float(merged["max_wait_seconds"]),
        "timeout_seconds": float(merged["timeout_seconds"]),
    }
