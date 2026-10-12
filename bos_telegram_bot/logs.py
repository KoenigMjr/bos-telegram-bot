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


class QuietPolling(logging.Filter):
    """Blendet die erfolgreiche Abfrage neuer Nachrichten aus (getUpdates, etwa alle 10 Sekunden): Sie ist im Log nur
    Rauschen. Alles andere von httpx bleibt, z.B. jedes sendMessage mit seiner Dauer. Auch eine fehlgeschlagene
    Abfrage (anderer Status als 200) bleibt sichtbar, sie deutet auf ein Problem hin."""

    def filter(self, record):
        if record.name != "httpx":
            return True
        message = record.getMessage()
        return not ("/getUpdates" in message and '"HTTP/1.1 200 OK"' in message)


class RedactingFormatter(logging.Formatter):
    """Kurzes Zeitformat mit Zeitzonenversatz und geschwärzten Geheimnissen."""

    def __init__(self, secrets=()):
        super().__init__("%(asctime)s %(shown_level)-7s %(short_name)s: %(message)s", "%Y-%m-%d %H:%M:%S%z")
        self.secrets = [s for s in secrets if s]

    def formatMessage(self, record):
        record.message = _LINE_BREAKS.sub(" ⏎ ", record.message)     # nur die Meldung, nicht den Traceback
        return super().formatMessage(record)

    def format(self, record):
        prefix = PACKAGE + "."
        record.short_name = record.name[len(prefix):] if record.name.startswith(prefix) else record.name
        # Die Bibliotheken (httpx, telegram) sagen bei ihren Meldungen INFO. Die sind aber nur im DEBUG-Modus
        # sichtbar, deshalb steht dort auch DEBUG. Die Meldung selbst bleibt unverändert.
        from_library = not (record.name == PACKAGE or record.name.startswith(prefix))
        record.shown_level = "DEBUG" if (record.levelno == logging.INFO and from_library) else record.levelname
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
    handler.addFilter(QuietPolling())
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
