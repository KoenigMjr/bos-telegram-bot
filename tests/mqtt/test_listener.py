import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import aiomqtt

from bos_telegram_bot.mqtt import listener

LOGGER = "bos_telegram_bot.mqtt.listener"
TOPIC = "homeassistant/boswatch/alarm/1000011"
ALARM = '{"ric": "1000011", "description": "Wache Nord", "message": "TEST"}'


def message(payload, topic=TOPIC, qos=1, retain=False):
    data = payload if isinstance(payload, bytes) else payload.encode()
    return NS(topic=aiomqtt.Topic(topic), payload=data, qos=qos, retain=retain)


class FakeBroker:
    """Ersetzt aiomqtt.Client. Jede Sitzung liefert ihre Nachrichten und bricht dann wie ein
    verlorener Broker mit MqttError ab. Alle Sitzungen und ihre Parameter werden gemerkt."""

    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.clients = []

    def __call__(self, **params):
        client = NS(params=params, subscribed=[], queued=self.sessions.pop(0) if self.sessions else [])
        self.clients.append(client)
        broker = self

        class Context:
            async def __aenter__(self_inner):
                async def subscribe(topic):
                    client.subscribed.append(topic)

                async def stream():
                    for item in client.queued:
                        yield item
                    raise aiomqtt.MqttError("Verbindung verloren (Test)")

                client.subscribe = subscribe
                client.messages = stream()
                return client

            async def __aexit__(self_inner, *exc):
                return False

        return Context()


class LogIncomingTests(unittest.TestCase):
    def test_debug_shows_topic_qos_retain_and_payload(self):
        with self.assertLogs(LOGGER, level="DEBUG") as logged:
            listener.log_incoming(aiomqtt.Topic(TOPIC), 1, False, ALARM)
        self.assertEqual(logged.output, [
            f"DEBUG:{LOGGER}:Empfangen: Topic {TOPIC}, QoS 1, retain=False, {len(ALARM)} Zeichen",
            f"DEBUG:{LOGGER}:Payload: {ALARM}",
        ])

    def test_very_long_payloads_are_cut_with_a_hint(self):
        raw = "x" * (listener.MAX_RAW_LOG + 250)
        with self.assertLogs(LOGGER, level="DEBUG") as logged:
            listener.log_incoming("t", 0, True, raw)
        self.assertIn("retain=True", logged.output[0])
        self.assertTrue(logged.output[1].endswith("… (+250 Zeichen)"))
        self.assertLess(len(logged.output[1]), listener.MAX_RAW_LOG + 100)

    def test_nothing_at_info(self):
        with self.assertRaises(AssertionError):          # assertLogs schlägt fehl, wenn nichts geloggt wurde
            with self.assertLogs(LOGGER, level="INFO"):
                listener.log_incoming("t", 0, False, ALARM)


class ListenerLoopTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, **mqtt):
        conf = {"host": "mqtt.test", "port": 1883, "username": "", "password": "", "topic": "homeassistant/boswatch/alarm/+"}
        conf.update(mqtt)
        return NS(bot_data={
            "config": {"mqtt": conf, "files": {"db_path": "unused.sqlite3"}},
            "entries": ["entries"], "active_fields": {"ric"}, "notification_template": "{RIC}", "last_payload": None,
        })

    async def run_listener(self, app, *sessions, stops_after=1):
        """Lässt den Listener laufen, bis er zum stops_after-ten Mal wartet, und beendet ihn dann."""
        broker = FakeBroker(*sessions)
        sleep = AsyncMock(side_effect=[None] * (stops_after - 1) + [asyncio.CancelledError()])
        handle, learn = AsyncMock(), AsyncMock()
        with patch.object(listener.aiomqtt, "Client", broker), patch.object(listener.asyncio, "sleep", sleep), \
                patch.object(listener, "handle_payload", handle), patch.object(listener, "learn_payload", learn):
            with self.assertRaises(asyncio.CancelledError):
                await listener.start_mqtt_listener(app)
        return broker, handle, learn, sleep

    async def test_an_alarm_is_passed_on_and_remembered_as_last_payload(self):
        app = self.make_app()
        _, handle, learn, _ = await self.run_listener(app, [message(ALARM)])
        expected = {"ric": "1000011", "description": "Wache Nord", "message": "TEST"}
        handle.assert_awaited_once_with(app, "unused.sqlite3", {"ric"}, "{RIC}", expected)
        learn.assert_awaited_once_with("unused.sqlite3", ["entries"], expected)
        self.assertEqual(app.bot_data["last_payload"], expected)

    async def test_everything_that_is_not_an_alarm_object_is_ignored(self):
        app = self.make_app()
        junk = [message(""), message("   \n"), message("kein json"), message("[1, 2]"), message("42"),
                message('"text"'), message(b"\xff\xfe")]
        _, handle, learn, _ = await self.run_listener(app, junk + [message(ALARM)])
        self.assertEqual(handle.await_count, 1)
        self.assertEqual(learn.await_count, 1)

    async def test_the_loop_survives_an_empty_message_and_goes_on(self):
        app = self.make_app()
        second = '{"ric": "1000043"}'
        _, handle, _, _ = await self.run_listener(app, [message(""), message(ALARM), message("x"), message(second)])
        self.assertEqual([c.args[4]["ric"] for c in handle.await_args_list], ["1000011", "1000043"])

    async def test_connection_parameters_and_subscription(self):
        app = self.make_app(username="bot", password="geheim", port=1884)
        broker, *_ = await self.run_listener(app, [])
        client = broker.clients[0]
        self.assertEqual(client.params, {"hostname": "mqtt.test", "port": 1884, "username": "bot", "password": "geheim"})
        self.assertEqual(client.subscribed, ["homeassistant/boswatch/alarm/+"])

    async def test_empty_credentials_become_none(self):
        broker, *_ = await self.run_listener(self.make_app(), [])
        self.assertIsNone(broker.clients[0].params["username"])
        self.assertIsNone(broker.clients[0].params["password"])

    async def test_connect_is_announced_at_info(self):
        with self.assertLogs(LOGGER, level="INFO") as logged:
            await self.run_listener(self.make_app(), [])
        self.assertEqual(logged.output[0], f"INFO:{LOGGER}:Verbunden mit MQTT-Broker mqtt.test:1883, lausche auf Topic homeassistant/boswatch/alarm/+")

    async def test_a_lost_connection_is_a_warning_and_waits_five_seconds(self):
        with self.assertLogs(LOGGER, level="WARNING") as logged:
            _, _, _, sleep = await self.run_listener(self.make_app(), [])
        self.assertEqual(logged.output, [f"WARNING:{LOGGER}:MQTT-Verbindung gestört: Verbindung verloren (Test). Neuer Versuch in 5 s"])
        sleep.assert_awaited_once_with(5)

    async def test_it_reconnects_and_keeps_delivering(self):
        app = self.make_app()
        broker, handle, _, sleep = await self.run_listener(app, [message(ALARM)], [message('{"ric": "1000043"}')], stops_after=2)
        self.assertEqual(len(broker.clients), 2)
        self.assertEqual([c.args[4]["ric"] for c in handle.await_args_list], ["1000011", "1000043"])
        self.assertEqual(sleep.await_count, 2)

    async def test_debug_shows_raw_messages_and_why_something_was_ignored(self):
        with self.assertLogs(LOGGER, level="DEBUG") as logged:
            await self.run_listener(self.make_app(), [message("kein json"), message(""), message("[1]"), message(ALARM)])
        text = "\n".join(logged.output)
        self.assertIn("Payload: kein json", text)
        self.assertIn(f"Kein JSON, ignoriert (Topic {TOPIC})", text)
        self.assertIn(f"Leere Nachricht ignoriert (Topic {TOPIC})", text)
        self.assertIn(f"JSON ist kein Objekt, ignoriert (Topic {TOPIC})", text)
        self.assertIn(f"Payload: {ALARM}", text)

    async def test_info_stays_quiet_about_ignored_messages(self):
        with self.assertLogs(LOGGER, level="INFO") as logged:
            await self.run_listener(self.make_app(), [message("kein json"), message("")])
        self.assertEqual(len(logged.output), 2)            # nur "Verbunden" und "Verbindung gestört"


if __name__ == "__main__":
    unittest.main()
