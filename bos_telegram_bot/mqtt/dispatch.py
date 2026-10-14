"""Verteilung eines Alarms an die Chats, deren Abos passen."""

import asyncio
import logging

from bos_telegram_bot.core.delivery import alarm_key
from bos_telegram_bot.core.matching import candidate_values, match_sub, split_list
from bos_telegram_bot.core.template import render
from bos_telegram_bot.storage import database as db, knowledge

log = logging.getLogger(__name__)

MAX_NAME_LENGTH = 60


def _distinct(values) -> list:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _limited_names(names: list) -> str:
    """Ganze Namen, solange sie in MAX_NAME_LENGTH Zeichen passen. Den Rest zählt die Angabe '+3 weitere',
    so wird nie mitten in einem Namen abgeschnitten (und keine Klammer bleibt offen). Nur ein einzelner
    Name, der allein zu lang ist, wird gekürzt."""
    shown, length = [], 0
    for name in names:
        needed = len(name) + (2 if shown else 0)           # 2 Zeichen für das ', ' davor
        if shown and length + needed > MAX_NAME_LENGTH:
            break
        shown.append(name)
        length += needed
    text = ", ".join(shown)
    if len(text) > MAX_NAME_LENGTH:
        text = text[:MAX_NAME_LENGTH - 1] + "…"
    rest = len(names) - len(shown)
    return text + (f" +{rest} weitere" if rest else "")


def describe_alarm(payload: dict) -> str:
    """Kurze Kennzeichnung eines Alarms für das Log: RIC(s) und Name(n).

    Der Alarmtext gehört bewusst nicht dazu, er kann Adressen und andere persönliche Angaben
    enthalten. Den kompletten Payload zeigt nur das Debug-Log."""
    idents = []
    for key in ("ric", "fms", "tone"):
        idents = _distinct(split_list(payload.get(f"{key}_list")))
        if not idents and payload.get(key) not in (None, ""):
            idents = [str(payload[key])]
        if idents:
            break

    names = _distinct(split_list(payload.get("description_list")))
    if not names and payload.get("description") not in (None, ""):
        names = [str(payload["description"])]
    names = [name for name in names if name not in idents]   # unbekannte RIC trägt die RIC als Namen

    label = ", ".join(idents[:3]) + (f" +{len(idents) - 3}" if len(idents) > 3 else "")
    text = _limited_names(names)
    if text:
        label += f" ({text})"
    return label or "ohne Kennung"


def build_notification_text(payload: dict, template: str, matched_aliases: list) -> str:
    """Setzt die Nachricht nach der konfigurierten Vorlage zusammen (siehe template.py)."""
    return render(template, payload, matched_aliases)


def _load_subscriptions_and_templates(db_path: str):
    """Abos und eigene Templates in einem Rutsch (läuft im Thread). Die Templates sind nur eine Zugabe: Gibt es
    Probleme damit, geht der Alarm trotzdem raus, dann mit dem Standard-Template."""
    subs = db.get_all_subs(db_path)
    try:
        templates = db.get_chat_templates(db_path)
    except Exception as error:
        log.warning("Eigene Templates konnten nicht gelesen werden, es gilt überall der Standard (%s: %s)",
                    type(error).__name__, error)
        templates = {}
    return subs, templates


def _text_for_chat(chat_id, payload: dict, default_template: str, chat_template, aliases: list):
    """(Text, Rückfalltext) für einen Chat. Hat der Chat ein eigenes Template, ist der Rückfalltext der Standardtext:
    Lehnt Telegram die Formatierung des eigenen ab, geht der Alarm damit trotzdem raus."""
    standard = build_notification_text(payload, default_template, aliases)
    if not chat_template:
        return standard, None
    try:
        own = build_notification_text(payload, chat_template, aliases)
    except Exception as error:
        log.warning("Chat %s: Das eigene Template lässt sich nicht anwenden, es gilt der Standard (%s: %s)",
                    chat_id, type(error).__name__, error)
        return standard, None
    log.debug("Chat %s: eigenes Template", chat_id)
    return (own, standard) if own != standard else (standard, None)


class _AlarmDelivery:
    """Sammelt die Ergebnisse der Nachrichten eines Alarms und schreibt am Ende die Zusammenfassung.

    Die Nachrichten gehen in die Versandschlange und sind erst später fertig. Jede meldet ihr Ergebnis hierher:
    Was Telegram angenommen hat, gilt im Gedächtnis als zugestellt. Scheitert der Versand endgültig, wird die
    Reservierung freigegeben, und die nächste Kopie des Alarms darf es noch einmal versuchen."""

    def __init__(self, label, key, delivered, sender, total, matched_subs, is_new):
        self.label, self.key, self.delivered, self.sender = label, key, delivered, sender
        self.total, self.matched_subs, self.is_new = total, matched_subs, is_new
        self.started = sender.now()
        self.done = 0
        self.sent = 0

    def on_result(self, chat_id, accepted, reason) -> None:
        if self.delivered is not None:
            if accepted:
                self.delivered.mark_delivered(self.key, chat_id)       # nur was Telegram angenommen hat, gilt als zugestellt
            else:
                self.delivered.release(self.key, chat_id)
        self.done += 1
        self.sent += 1 if accepted else 0
        if self.done == self.total:
            self._log_summary()

    def _log_summary(self) -> None:
        elapsed = self.sender.now() - self.started
        level = logging.INFO if self.sent == self.total else logging.WARNING
        if self.is_new:
            log.log(level, "Alarm %s: %d Abo(s) in %d Chat(s), gesendet %d/%d in %.1f s",
                    self.label, self.matched_subs, self.total, self.sent, self.total, elapsed)
        else:
            log.log(level, "Alarm %s: Wiederholung, %d Chat(s) noch offen, gesendet %d/%d in %.1f s",
                    self.label, self.total, self.sent, self.total, elapsed)


async def handle_payload(db_path: str, active_fields: set, template: str, payload: dict, sender, delivered=None) -> None:
    """Verteilt einen Alarm an alle Chats, deren Abos passen.

    Die Nachrichten werden nur in die Versandschlange (sender) gestellt, der Empfang wartet nie auf Telegram.
    delivered ist das Gedächtnis für doppelte Alarme (core/delivery.py). Ohne es wird jede Nachricht behandelt,
    als wäre sie neu. Mit ihm geht ein Alarm an jeden Chat höchstens einmal raus, auch wenn er mehrfach
    eintrifft; nur Chats, bei denen das Senden endgültig scheiterte, werden bei der nächsten Kopie erneut
    versucht."""
    label = describe_alarm(payload)
    key = alarm_key(payload) if delivered is not None else None
    is_new = delivered.register(key) if delivered is not None else True

    # DB-Zugriff ist synchron (sqlite3) -> in Thread auslagern, damit der
    # Event-Loop bei jedem Alarm nicht blockiert.
    subs, chat_templates = await asyncio.to_thread(_load_subscriptions_and_templates, db_path)

    # Pro Chat genau EINE Nachricht, auch wenn mehrere Abos desselben Chats
    # passen (z.B. Wache und Fahrzeug eines Multicast-Alarms).
    matches = {}
    matched_subs = 0
    value_cache = {}
    for sub in subs:
        field = sub["field"]
        if field not in active_fields:
            log.debug("Abo %s ruht: Feld '%s' ist nicht mehr konfiguriert", sub["id"], field)
            continue  # Feld wurde aus der Konfiguration entfernt, Abo ruht

        if field not in value_cache:
            value_cache[field] = candidate_values(payload, field)
        values, raw_list = value_cache[field]
        if not values:
            continue

        resolved = match_sub(sub, values, raw_list)
        if resolved is None:
            continue
        matched_subs += 1
        log.debug("Abo %s passt (Chat %s, Feld %s, Ziel %r): %s", sub["id"], sub["chat_id"], field, sub["target"], resolved)
        aliases = matches.setdefault(sub["chat_id"], [])
        if resolved not in aliases:
            aliases.append(resolved)

    if not matches:
        if is_new:
            log.info("Alarm %s: kein passendes Abo", label)
        else:
            log.debug("Alarm %s: Wiederholung, weiterhin kein passendes Abo", label)
        return

    # Chats, an die dieser Alarm schon ging oder gerade geht (Kopie des Alarms, zweites Multicast-Topic), werden
    # übersprungen. claim() reserviert den Chat gleich, damit eine Kopie ihn nicht ein zweites Mal einreiht.
    pending = {}
    for chat_id, aliases in matches.items():
        if delivered is not None and not delivered.claim(key, chat_id):
            log.debug("Chat %s: Alarm schon zugestellt oder in Arbeit, übersprungen", chat_id)
        else:
            pending[chat_id] = aliases
    if not pending:
        log.debug("Alarm %s: Wiederholung, alle %d Chat(s) schon beliefert oder in Arbeit", label, len(matches))
        return

    texts = {chat_id: _text_for_chat(chat_id, payload, template, chat_templates.get(chat_id), aliases)
             for chat_id, aliases in pending.items()}
    delivery = _AlarmDelivery(label, key, delivered, sender, len(texts), matched_subs, is_new)
    for chat_id, (text, fallback) in texts.items():
        log.debug("Nachricht an Chat %s: %s", chat_id, text)
        try:
            sender.submit(chat_id, text, delivery.on_result, fallback=fallback)
        except Exception as error:                     # darf die übrigen Chats nicht um ihren Alarm bringen
            log.exception("Alarm %s: Einreihen für Chat %s fehlgeschlagen", label, chat_id)
            delivery.on_result(chat_id, False, str(error))


async def learn_payload(db_path: str, entries: list, payload: dict) -> None:
    """Merkt sich neue Wertepaare. Fehler hier dürfen die Alarmverteilung nie stören."""
    try:
        await asyncio.to_thread(knowledge.learn_from_payload, db_path, entries, payload)
    except Exception as e:
        log.warning("Lernen fehlgeschlagen (wird ignoriert): %s", e)
