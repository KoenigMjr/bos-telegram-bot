"""Verbindung zum MQTT-Broker: nimmt Alarme entgegen und gibt sie weiter."""

import asyncio
import json
import logging

import aiomqtt

from bos_telegram_bot.mqtt.dispatch import handle_payload, learn_payload

log = logging.getLogger(__name__)

MAX_RAW_LOG = 4000


def log_incoming(topic, qos, retain, raw: str) -> None:
    """Debug-Log: was vom Broker angekommen ist, bevor irgendetwas damit passiert."""
    log.debug("Empfangen: Topic %s, QoS %s, retain=%s, %d Zeichen", topic, qos, retain, len(raw))
    shown = raw if len(raw) <= MAX_RAW_LOG else raw[:MAX_RAW_LOG] + f"… (+{len(raw) - MAX_RAW_LOG} Zeichen)"
    log.debug("Payload: %s", shown)


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
                log.info("Verbunden mit MQTT-Broker %s:%s, lausche auf Topic %s",
                         mqtt_conf["host"], mqtt_conf["port"], mqtt_conf["topic"])

                async for message in client.messages:
                    raw = message.payload.decode(errors="replace").strip()
                    log_incoming(message.topic, message.qos, message.retain, raw)
                    if not raw:
                        log.debug("Leere Nachricht ignoriert (Topic %s)", message.topic)
                        continue

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        # Home-Assistant-Discovery-Configs oder andere
                        # Nicht-Alarm-Nachrichten auf demselben Topic-Ast.
                        log.debug("Kein JSON, ignoriert (Topic %s)", message.topic)
                        continue

                    if not isinstance(payload, dict):
                        log.debug("JSON ist kein Objekt, ignoriert (Topic %s)", message.topic)
                        continue

                    # Keine eigene Dedupe-/Filterlogik hier: BOSWatch3 liefert
                    # bereits fertig aufbereitete, einzeln zustellbare Alarme.
                    app.bot_data["last_payload"] = payload
                    await handle_payload(app, db_path, active_fields, template, payload)
                    await learn_payload(db_path, entries, payload)

        except aiomqtt.MqttError as e:
            log.warning("MQTT-Verbindung gestört: %s. Neuer Versuch in 5 s", e)
            await asyncio.sleep(5)
