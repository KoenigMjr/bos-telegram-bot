"""Logging des Bots.

INFO    eine kurze Zeile pro Ereignis (Alarm verarbeitet, Abo angelegt, Verbindung hergestellt)
DEBUG   zusätzlich die Rohdaten: Topic, QoS, Payload, welches Abo wann passt, fertige Nachricht
WARNING/ERROR  Probleme, die Aufmerksamkeit brauchen

Geschrieben wird auf die Standardausgabe, dort holt sie Docker bzw. Portainer ab. Das Token des Bots
wird in jeder Ausgabe geschwärzt, auch in Fehlermeldungen der Bibliotheken (die URL der Telegram-API
enthält es).

Jeder Eintrag steht in einer Zeile. Docker zerlegt mehrzeilige Einträge in einzelne Zeilen ohne Zeitstempel,
und Oberflächen wie Portainer können dabei Zeichen verfälschen. Zeilenumbrüche in einer Meldung (z.B. in der
Alarmnachricht) erscheinen deshalb als Zeichen ⏎. Nur der Traceback eines Fehlers bleibt mehrzeilig."""

import logging
import re
import sys

PACKAGE = "bos_telegram_bot"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Bibliotheken: sonst WARNING. Im DEBUG-Modus INFO, damit man jede Anfrage an Telegram samt Dauer sieht.
_LIBRARIES = ("httpx", "telegram", "apscheduler", "aiomqtt", "paho")
# Sehr gesprächig und für uns uninteressant: bleiben immer auf WARNING.
_NOISY = ("httpcore", "hpack", "asyncio")

_TOKEN_IN_URL = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
_LINE_BREAKS = re.compile(r"\s*[\r\n]\s*")        # ein Umbruch samt umgebendem Leerraum, auch mehrere hintereinander
_MARKER = "_bos_telegram_bot_handler"


def normalize_level(value):
    """'info' -> 'INFO'. Gibt None zurück, wenn der Wert kein gültiger Pegel ist."""
    name = str(value).strip().upper()
    return name if name in LEVELS else None


class RedactingFormatter(logging.Formatter):
    """Kurzes Zeitformat mit Zeitzonenversatz und geschwärzten Geheimnissen."""

    def __init__(self, secrets=()):
        super().__init__("%(asctime)s %(levelname)-7s %(short_name)s: %(message)s", "%Y-%m-%d %H:%M:%S%z")
        self.secrets = [s for s in secrets if s]

    def formatMessage(self, record):
        record.message = _LINE_BREAKS.sub(" ⏎ ", record.message)     # nur die Meldung, nicht den Traceback
        return super().formatMessage(record)

    def format(self, record):
        prefix = PACKAGE + "."
        record.short_name = record.name[len(prefix):] if record.name.startswith(prefix) else record.name
        return self.redact(super().format(record))   # auch der Traceback läuft hier durch

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "<TOKEN>")
        return _TOKEN_IN_URL.sub("bot<TOKEN>", text)


def reset_logging():
    """Entfernt unseren Handler und stellt die Pegel zurück (für Tests und erneutes Einrichten)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
    for name in (PACKAGE, *_LIBRARIES, *_NOISY):
        logging.getLogger(name).setLevel(logging.NOTSET)
    root.setLevel(logging.WARNING)


def setup_logging(level="INFO", secrets=()):
    """Richtet das Logging ein. Mehrfaches Aufrufen ersetzt die Einrichtung, es entstehen keine Doppelzeilen."""
    reset_logging()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(secrets))
    setattr(handler, _MARKER, True)
    logging.getLogger().addHandler(handler)
    set_level(level)


def set_level(level):
    """Stellt den Pegel des Bots um. Ungültige Werte ergeben INFO (die Konfiguration meldet sie vorher)."""
    numeric = getattr(logging, normalize_level(level) or "INFO")
    logging.getLogger(PACKAGE).setLevel(numeric)
    library_level = logging.INFO if numeric <= logging.DEBUG else logging.WARNING
    for name in _LIBRARIES:
        logging.getLogger(name).setLevel(library_level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
