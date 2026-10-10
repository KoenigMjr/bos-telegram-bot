"""Verbindung zum MQTT-Broker: nimmt Alarme entgegen und gibt sie weiter."""

import asyncio
import json
import logging

import aiomqtt

from bos_telegram_bot.core.delivery import alarm_key
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
    delivered = app.bot_data.get("delivered")      # Gedächtnis für doppelte Alarme, None = abgeschaltet

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

                    # Dieselbe Nachricht kann mehrfach eintreffen (siehe core/delivery.py). Eine Wiederholung
                    # ändert weder /lastraw noch muss sie noch einmal gelernt werden.
                    is_new = not (delivered is not None and delivered.knows(alarm_key(payload)))
                    if is_new:
                        app.bot_data["last_payload"] = payload

                    try:
                        await handle_payload(app, db_path, active_fields, template, payload, delivered)
                        if is_new:
                            await learn_payload(db_path, entries, payload)
                    except Exception:
                        # Ein Fehler bei einem Alarm darf die Schleife nicht beenden: Der Bot würde sonst
                        # weiter auf Befehle antworten, aber keinen Alarm mehr zustellen, ohne dass es auffällt.
                        log.exception("Alarm konnte nicht verarbeitet werden (Topic %s)", message.topic)

        except aiomqtt.MqttError as e:
            log.warning("MQTT-Verbindung gestört: %s. Neuer Versuch in 5 s", e)
            await asyncio.sleep(5)
        except Exception:
            # Auch ein unerwarteter Fehler (z.B. beim Verbinden) darf die Schleife nicht beenden.
            log.exception("Unerwarteter Fehler bei der MQTT-Verbindung. Neuer Versuch in 5 s")
            await asyncio.sleep(5)
