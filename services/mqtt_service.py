import asyncio
import html
import json
import re

import aiomqtt

import database as db


def _build_notification_text(payload: dict, notification_fields: list, matched_alias: str) -> str:
    lines = ["🚨 <b>BOS-ALARM</b> 🚨", ""]
    for field in notification_fields:
        value = payload.get(field)
        if value:
            lines.append(html.escape(str(value)))
    lines.append("")
    lines.append(f"<i>abonniert über: {html.escape(matched_alias)}</i>")
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


async def _handle_payload(app, db_path: str, fields_cfg: dict, notification_fields: list, payload: dict) -> None:
    # DB-Zugriff ist synchron (sqlite3) -> in Thread auslagern, damit der
    # Event-Loop bei jedem Alarm nicht blockiert.
    subs = await asyncio.to_thread(db.get_all_subs, db_path)

    tasks = []
    for sub in subs:
        field_key = sub["field"]
        field_cfg = fields_cfg.get(field_key)
        if not field_cfg:
            continue  # Feld wurde aus der Config entfernt, Sub ignorieren

        json_key = field_cfg["json_key"]
        value = payload.get(json_key)
        if value is None:
            continue

        value = str(value)
        target = sub["target"]
        is_regex = sub["is_regex"]

        match_found = False
        resolved_alias = sub["alias"]

        if is_regex:
            m = re.match(target, value)
            if m:
                match_found = True
                try:
                    # WICHTIG: kein zusätzliches Backslash-Escaping hier -
                    # m.expand() erwartet \1 / \g<name> genau so, wie es aus
                    # der CSV kommt. Doppeltes Escapen würde die Platzhalter-
                    # Auflösung unwirksam machen.
                    resolved_alias = m.expand(sub["alias"])
                except (re.error, IndexError) as e:
                    print(f"[Regex] Konnte Alias '{sub['alias']}' nicht auflösen: {e}")
        else:
            if value == target:
                match_found = True

        if match_found:
            text = _build_notification_text(payload, notification_fields, resolved_alias)
            tasks.append(_notify_subscriber(app, sub["chat_id"], text))

    if tasks:
        await asyncio.gather(*tasks)


async def start_mqtt_listener(app):
    config = app.bot_data["config"]
    mqtt_conf = config["mqtt"]
    db_path = config["files"]["db_path"]
    fields_cfg = config["fields"]
    notification_fields = config.get("notification_fields") or list(
        {f["json_key"] for f in fields_cfg.values()}
    )

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
                    await _handle_payload(app, db_path, fields_cfg, notification_fields, payload)

        except aiomqtt.MqttError as e:
            print(f"[MQTT Error] {e}. Verbinde neu in 5 Sekunden...")
            await asyncio.sleep(5)
