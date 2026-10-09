"""Verbindung zum MQTT-Broker: nimmt Alarme entgegen und gibt sie weiter."""

import asyncio
import json

import aiomqtt

from bos_telegram_bot.mqtt.dispatch import handle_payload, learn_payload


async def start_mqtt_listener(app):
    config = app.bot_data["config"]
    mqtt_conf = config["mqtt"]
    db_path = config["files"]["db_path"]
    entries = app.bot_data["entries"]
    active_fields = app.bot_data["active_fields"]
    template = app.bot_data["notification_template"]

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
                    await handle_payload(app, db_path, active_fields, template, payload)
                    await learn_payload(db_path, entries, payload)

        except aiomqtt.MqttError as e:
            print(f"[MQTT Error] {e}. Verbinde neu in 5 Sekunden...")
            await asyncio.sleep(5)
