"""Verteilung eines Alarms an die Chats, deren Abos passen."""

import asyncio
import logging
import time

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
    text = ", ".join(names)
    if len(text) > MAX_NAME_LENGTH:
        text = text[:MAX_NAME_LENGTH - 1] + "…"
    if text:
        label += f" ({text})"
    return label or "ohne Kennung"


def build_notification_text(payload: dict, template: str, matched_aliases: list) -> str:
    """Setzt die Nachricht nach der konfigurierten Vorlage zusammen (siehe template.py)."""
    return render(template, payload, matched_aliases)


async def _notify_subscriber(app, chat_id: int, text: str) -> bool:
    """Sendet die Nachricht an einen Chat. True, wenn Telegram sie angenommen hat."""
    started = time.monotonic()
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        # Häufigste Ursache in Gruppen: Bot wurde entfernt/geblockt, oder
        # 'Forbidden: bot was kicked from the group chat'. Abo bleibt dann
        # bestehen, bis jemand es manuell per /abo löscht (bewusst kein
        # Auto-Cleanup, um keine Abos ohne Nachfrage zu verlieren).
        log.warning("Senden an Chat %s fehlgeschlagen (%s: %s)", chat_id, type(e).__name__, e)
        return False
    log.debug("Chat %s: gesendet in %.2f s", chat_id, time.monotonic() - started)
    return True


async def handle_payload(app, db_path: str, active_fields: set, template: str, payload: dict) -> None:
    """Verteilt einen Alarm an alle Chats, deren Abos passen."""
    label = describe_alarm(payload)

    # DB-Zugriff ist synchron (sqlite3) -> in Thread auslagern, damit der
    # Event-Loop bei jedem Alarm nicht blockiert.
    subs = await asyncio.to_thread(db.get_all_subs, db_path)

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
        log.info("Alarm %s: kein passendes Abo", label)
        return

    texts = {chat_id: build_notification_text(payload, template, aliases) for chat_id, aliases in matches.items()}
    for chat_id, text in texts.items():
        log.debug("Nachricht an Chat %s:\n%s", chat_id, text)

    results = await asyncio.gather(*(_notify_subscriber(app, chat_id, text) for chat_id, text in texts.items()))
    sent = sum(results)
    log.log(logging.INFO if sent == len(texts) else logging.WARNING,
            "Alarm %s: %d Abo(s) in %d Chat(s), gesendet %d/%d", label, matched_subs, len(texts), sent, len(texts))


async def learn_payload(db_path: str, entries: list, payload: dict) -> None:
    """Merkt sich neue Wertepaare. Fehler hier dürfen die Alarmverteilung nie stören."""
    try:
        await asyncio.to_thread(knowledge.learn_from_payload, db_path, entries, payload)
    except Exception as e:
        log.warning("Lernen fehlgeschlagen (wird ignoriert): %s", e)
