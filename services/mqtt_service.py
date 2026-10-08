import asyncio
import html
import json

import aiomqtt

import database as db
import knowledge
from matching import candidate_values, match_sub, split_list


def _display_items(payload: dict, field: str) -> list:
    """Anzuzeigende Werte eines Feldes: bei Multicast alle Einträge aus der
    Liste (je Eintrag eine Zeile), sonst der einzelne Wert."""
    listed = []
    for part in split_list(payload.get(f"{field}_list")):
        if part not in listed:
            listed.append(part)
    if len(listed) > 1:
        return listed
    primary = payload.get(field)
    if primary not in (None, ""):
        return [str(primary)]
    return listed


def build_notification_text(payload: dict, notification_fields: list, matched_aliases: list) -> str:
    lines = ["🚨 <b>BOS-ALARM</b> 🚨", ""]
    for field in notification_fields:
        for item in _display_items(payload, field):
            lines.append(html.escape(item))
    lines.append("")
    lines.append(f"<i>abonniert über: {html.escape(', '.join(matched_aliases))}</i>")
    return "\n".join(lines)


async def _notify_subscriber(app, chat_id: int, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        # Häufigste Ursache in Gruppen: Bot wurde entfernt/geblockt, oder
        # 'Forbidden: bot was kicked from the group chat'. Abo bleibt dann
        # bestehen, bis jemand es manuell per /abo löscht (bewusst kein
        # Auto-Cleanup, um keine Abos ohne Nachfrage zu verlieren).
        print(f"[Telegram Error] Konnte Nachricht an Chat {chat_id} nicht senden: {e}")


async def handle_payload(app, db_path: str, active_fields: set, notification_fields: list, payload: dict) -> None:
    """Verteilt einen Alarm an alle Chats, deren Abos passen."""
    # DB-Zugriff ist synchron (sqlite3) -> in Thread auslagern, damit der
    # Event-Loop bei jedem Alarm nicht blockiert.
    subs = await asyncio.to_thread(db.get_all_subs, db_path)

    # Pro Chat genau EINE Nachricht, auch wenn mehrere Abos desselben Chats
    # passen (z.B. Wache und Fahrzeug eines Multicast-Alarms).
    matches = {}
    value_cache = {}
    for sub in subs:
        field = sub["field"]
        if field not in active_fields:
            continue  # Feld wurde aus der Konfiguration entfernt, Abo ruht

        if field not in value_cache:
            value_cache[field] = candidate_values(payload, field)
        values, raw_list = value_cache[field]
        if not values:
            continue

        resolved = match_sub(sub, values, raw_list)
        if resolved is None:
            continue
        aliases = matches.setdefault(sub["chat_id"], [])
        if resolved not in aliases:
            aliases.append(resolved)

    tasks = [
        _notify_subscriber(app, chat_id, build_notification_text(payload, notification_fields, aliases))
        for chat_id, aliases in matches.items()
    ]
    if tasks:
        await asyncio.gather(*tasks)


async def learn_payload(db_path: str, entries: list, payload: dict) -> None:
    """Merkt sich neue Wertepaare. Fehler hier dürfen die Alarmverteilung nie stören."""
    try:
        await asyncio.to_thread(knowledge.learn_from_payload, db_path, entries, payload)
    except Exception as e:
        print(f"[Lernen] Fehler (wird ignoriert): {e}")


async def start_mqtt_listener(app):
    config = app.bot_data["config"]
    mqtt_conf = config["mqtt"]
    db_path = config["files"]["db_path"]
    entries = app.bot_data["entries"]
    active_fields = app.bot_data["active_fields"]
    notification_fields = config.get("notification_fields") or ["description", "message", "ric"]

    while True:
        try:
            async with aiomqtt.Client(
                hostname=mqtt_conf["host"],
                port=mqtt_conf["port"],
                username=mqtt_conf.get("username") or None,
                password=mqtt_conf.get("password") or None,
            ) as client:
                await client.subscribe(mqtt_conf["topic"])
                print(f"[MQTT] Verbunden und lausche auf Topic: {mqtt_conf['topic']}")

                async for message in client.messages:
                    raw = message.payload.decode(errors="replace").strip()
                    if not raw:
                        continue

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        # Home-Assistant-Discovery-Configs oder andere
                        # Nicht-Alarm-Nachrichten auf demselben Topic-Ast.
                        continue

                    if not isinstance(payload, dict):
                        continue

                    # Keine eigene Dedupe-/Filterlogik hier: BOSWatch3 liefert
                    # bereits fertig aufbereitete, einzeln zustellbare Alarme.
                    app.bot_data["last_payload"] = payload
                    await handle_payload(app, db_path, active_fields, notification_fields, payload)
                    await learn_payload(db_path, entries, payload)

        except aiomqtt.MqttError as e:
            print(f"[MQTT Error] {e}. Verbinde neu in 5 Sekunden...")
            await asyncio.sleep(5)
