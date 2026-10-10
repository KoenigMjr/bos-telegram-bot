import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import aiomqtt

from bos_telegram_bot.core.delivery import DeliveryMemory, alarm_key
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE
from bos_telegram_bot.mqtt import dispatch, listener
from bos_telegram_bot.storage import database as db
from tests.helpers import Env

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
        session = self.sessions.pop(0) if self.sessions else []
        client = NS(params=params, subscribed=[], subscribed_qos=[], queued=[] if isinstance(session, Exception) else session)
        self.clients.append(client)

        class Context:
            async def __aenter__(self_inner):
                if isinstance(session, Exception):
                    raise session          # der Verbindungsaufbau schlägt fehl

                async def subscribe(topic, qos=0):
                    client.subscribed.append(topic)
                    client.subscribed_qos.append(qos)

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


class ListenerTestCase(unittest.IsolatedAsyncioTestCase):
    """Gemeinsame Hilfen, selbst ohne Tests."""

    def make_app(self, **mqtt):
        conf = {"host": "mqtt.test", "port": 1883, "username": "", "password": "", "topic": "homeassistant/boswatch/alarm/+"}
        conf.update(mqtt)
        return NS(bot_data={
            "config": {"mqtt": conf, "files": {"db_path": "unused.sqlite3"}},
            "entries": ["entries"], "active_fields": {"ric"}, "notification_template": "{RIC}", "last_payload": None,
        })

    async def run_listener(self, app, *sessions, stops_after=1, handle=None):
        """Lässt den Listener laufen, bis er zum stops_after-ten Mal wartet, und beendet ihn dann."""
        broker = FakeBroker(*sessions)
        sleep = AsyncMock(side_effect=[None] * (stops_after - 1) + [asyncio.CancelledError()])
        handle, learn = handle or AsyncMock(), AsyncMock()
        with patch.object(listener.aiomqtt, "Client", broker), patch.object(listener.asyncio, "sleep", sleep), \
                patch.object(listener, "handle_payload", handle), patch.object(listener, "learn_payload", learn):
            with self.assertRaises(asyncio.CancelledError):
                await listener.start_mqtt_listener(app)
        return broker, handle, learn, sleep


class ListenerLoopTests(ListenerTestCase):
    async def test_an_alarm_is_passed_on_and_remembered_as_last_payload(self):
        app = self.make_app()
        _, handle, learn, _ = await self.run_listener(app, [message(ALARM)])
        expected = {"ric": "1000011", "description": "Wache Nord", "message": "TEST"}
        handle.assert_awaited_once_with(app, "unused.sqlite3", {"ric"}, "{RIC}", expected, None)   # None: kein Gedächtnis
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

    async def test_it_subscribes_with_qos_0_by_default(self):
        broker, *_ = await self.run_listener(self.make_app(), [])
        self.assertEqual(broker.clients[0].subscribed_qos, [0])

    async def test_it_subscribes_with_the_configured_qos(self):
        broker, *_ = await self.run_listener(self.make_app(qos=1), [])
        self.assertEqual(broker.clients[0].subscribed_qos, [1])

    async def test_the_qos_is_part_of_the_connect_line(self):
        with self.assertLogs(LOGGER, level="INFO") as logged:
            await self.run_listener(self.make_app(qos=1), [])
        self.assertTrue(logged.output[0].endswith("lausche auf Topic homeassistant/boswatch/alarm/+ (QoS 1)"), logged.output[0])

    async def test_every_reconnect_subscribes_again_with_the_same_qos(self):
        broker, *_ = await self.run_listener(self.make_app(qos=1), [], [], stops_after=2)
        self.assertEqual([c.subscribed_qos for c in broker.clients], [[1], [1]])

    async def test_empty_credentials_become_none(self):
        broker, *_ = await self.run_listener(self.make_app(), [])
        self.assertIsNone(broker.clients[0].params["username"])
        self.assertIsNone(broker.clients[0].params["password"])

    async def test_connect_is_announced_at_info(self):
        with self.assertLogs(LOGGER, level="INFO") as logged:
            await self.run_listener(self.make_app(), [])
        self.assertEqual(logged.output[0], f"INFO:{LOGGER}:Verbunden mit MQTT-Broker mqtt.test:1883, lausche auf Topic homeassistant/boswatch/alarm/+ (QoS 0)")

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


ALARM_COPY = '{"timestamp": "1700000000.1", "ric": "1000011", "republished": true}'
ALARM_ORIGINAL = '{"timestamp": "1700000000.1", "ric": "1000011"}'


class RepeatedAlarmTests(ListenerTestCase):
    """Der Listener mit Gedächtnis für doppelte Alarme."""

    def app_with_memory(self):
        app = self.make_app()
        app.bot_data["delivered"] = DeliveryMemory(300)
        return app

    @staticmethod
    def registering_handle():
        """Ein handle_payload, das den Alarm wie das echte im Gedächtnis einträgt."""
        async def handle(app, db_path, fields, template, payload, delivered):
            delivered.register(alarm_key(payload))
        return AsyncMock(side_effect=handle)

    async def test_the_memory_is_passed_to_handle_payload(self):
        app = self.app_with_memory()
        _, handle, _, _ = await self.run_listener(app, [message(ALARM_ORIGINAL)])
        self.assertIs(handle.await_args.args[5], app.bot_data["delivered"])

    async def test_a_repeat_is_neither_learned_again_nor_shown_as_last_payload(self):
        app = self.app_with_memory()
        _, handle, learn, _ = await self.run_listener(
            app, [message(ALARM_ORIGINAL), message(ALARM_COPY)], handle=self.registering_handle())
        self.assertEqual(handle.await_count, 2)                    # die Verteilung sieht beide (für Wiederholungen)
        self.assertEqual(learn.await_count, 1)                     # gelernt wird nur einmal
        self.assertNotIn("republished", app.bot_data["last_payload"])   # /lastraw zeigt das Original

    async def test_a_new_alarm_after_a_repeat_is_shown_and_learned(self):
        app = self.app_with_memory()
        other = '{"timestamp": "1700000999.9", "ric": "1000043"}'
        _, _, learn, _ = await self.run_listener(
            app, [message(ALARM_ORIGINAL), message(ALARM_COPY), message(other)], handle=self.registering_handle())
        self.assertEqual(learn.await_count, 2)
        self.assertEqual(app.bot_data["last_payload"]["ric"], "1000043")

    async def test_a_copy_without_its_original_counts_as_new(self):
        app = self.app_with_memory()
        _, _, learn, _ = await self.run_listener(app, [message(ALARM_COPY)], handle=self.registering_handle())
        self.assertEqual(learn.await_count, 1)
        self.assertTrue(app.bot_data["last_payload"]["republished"])

    async def test_without_a_memory_everything_counts_as_new(self):
        app = self.make_app()                                      # kein "delivered" in bot_data
        _, handle, learn, _ = await self.run_listener(app, [message(ALARM_ORIGINAL), message(ALARM_COPY)])
        self.assertEqual((handle.await_count, learn.await_count), (2, 2))

    async def test_an_alarm_without_timestamp_is_always_new(self):
        app = self.app_with_memory()
        _, _, learn, _ = await self.run_listener(
            app, [message('{"ric": "1"}'), message('{"ric": "1"}')], handle=self.registering_handle())
        self.assertEqual(learn.await_count, 2)


class WholeChainTests(unittest.IsolatedAsyncioTestCase):
    """Broker -> Empfangsschleife -> echte Verteilung -> echte Datenbank -> Telegram. Nichts davon ist ersetzt,
    nur der Broker (FakeBroker) und das Warten bei Verbindungsverlust."""

    MERGED = {"ric_list": "1000011, 1000043", "description_list": "Wache Nord, Lagedienst Musterstadt",
              "timestamp_list": "1700000000.1, 1700000000.4", "message": "TEST Beispieltext", "multicastMode": "complete"}
    FIRST = dict(MERGED, ric="1000011", description="Wache Nord", timestamp="1700000000.1")
    SECOND = dict(MERGED, ric="1000043", description="Lagedienst Musterstadt", timestamp="1700000000.4")

    async def run_chain(self, env, memory, messages):
        app = NS(bot=env.bot, bot_data={
            "config": {"mqtt": {"host": "mqtt.test", "port": 1883, "topic": "alarm/+"}, "files": {"db_path": env.db_path}},
            "entries": env.entries, "active_fields": env.context.bot_data["active_fields"],
            "notification_template": DEFAULT_TEMPLATE, "last_payload": None, "delivered": memory,
        })
        sleep = AsyncMock(side_effect=asyncio.CancelledError())
        with patch.object(listener.aiomqtt, "Client", FakeBroker(messages)), patch.object(listener.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await listener.start_mqtt_listener(app)
        return app

    def burst(self):
        """Die vier Nachrichten eines Multicasts, wie im echten Log: je Empfänger das Original und danach die
        kompakt formatierte Kopie der Automation."""
        def compact(payload):
            return json.dumps(dict(payload, republished=True), separators=(",", ":"))
        return [message(json.dumps(self.FIRST)), message(json.dumps(self.SECOND)),
                message(compact(self.FIRST)), message(compact(self.SECOND))]

    async def test_a_multicast_burst_of_four_messages_reaches_each_chat_once(self):
        env = Env(self)
        for chat, ric in ((11, "1000011"), (11, "1000043"), (22, "1000043")):
            db.add_sub(env.db_path, chat, "ric", ric, f"RIC: {ric}", False)
        env.bot.sent.clear()
        app = await self.run_chain(env, DeliveryMemory(300), self.burst())
        self.assertEqual(sorted(m["chat_id"] for m in env.bot.sent), [11, 22])
        self.assertNotIn("republished", app.bot_data["last_payload"])             # /lastraw zeigt das Original

    async def test_without_the_memory_the_same_burst_is_delivered_four_times_per_chat(self):
        """Der Gegenbeweis: ohne Erkennung (remember_seconds: 0) ist es genau das beobachtete Verhalten."""
        env = Env(self)
        db.add_sub(env.db_path, 11, "ric", "1000011", "RIC: 1000011", False)
        env.bot.sent.clear()
        await self.run_chain(env, None, self.burst())
        self.assertEqual([m["chat_id"] for m in env.bot.sent], [11, 11, 11, 11])

    async def test_a_burst_is_learned_only_once_but_learns_every_recipient(self):
        env = Env(self)
        real = dispatch.knowledge.learn_from_payload
        with patch.object(dispatch.knowledge, "learn_from_payload", wraps=real) as spy:
            await self.run_chain(env, DeliveryMemory(300), self.burst())
        self.assertEqual(spy.call_count, 1)                                        # nicht viermal
        for ric, name in (("1000011", "Wache Nord"), ("1000043", "Lagedienst Musterstadt")):
            self.assertEqual(db.get_learned(env.db_path, "ric", ric), name)        # beide Empfänger der Liste

    async def test_without_the_memory_every_message_is_learned(self):
        env = Env(self)
        real = dispatch.knowledge.learn_from_payload
        with patch.object(dispatch.knowledge, "learn_from_payload", wraps=real) as spy:
            await self.run_chain(env, None, self.burst())
        self.assertEqual(spy.call_count, 4)


class ResilienceTests(ListenerTestCase):
    """Ein Fehler bei einem Alarm darf die Schleife nie beenden: sonst bliebe der Bot taub, ohne dass es jemand merkt."""

    async def test_an_error_while_processing_one_alarm_does_not_stop_the_next(self):
        app = self.make_app()
        handle = AsyncMock(side_effect=[RuntimeError("Datenbank gesperrt"), None])
        second = '{"ric": "1000043"}'
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            _, handle, learn, _ = await self.run_listener(app, [message(ALARM), message(second)], handle=handle)
        self.assertEqual(handle.await_count, 2)
        self.assertEqual(learn.await_count, 1)                     # gelernt wird nur, was verarbeitet werden konnte
        self.assertIn("Alarm konnte nicht verarbeitet werden", logged.output[0])
        self.assertIn(TOPIC, logged.output[0])
        self.assertIn("Datenbank gesperrt", logged.output[0])       # Traceback steht dabei
        self.assertIn("RuntimeError", logged.output[0])

    async def test_the_failed_alarm_is_still_shown_as_last_payload(self):
        app = self.make_app()
        handle = AsyncMock(side_effect=RuntimeError("kaputt"))
        with self.assertLogs(LOGGER, level="ERROR"):
            await self.run_listener(app, [message(ALARM)], handle=handle)
        self.assertEqual(app.bot_data["last_payload"]["ric"], "1000011")

    async def test_an_unexpected_error_while_connecting_is_retried(self):
        app = self.make_app()
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            _, handle, _, sleep = await self.run_listener(
                app, OSError("Netzwerk nicht erreichbar"), [message(ALARM)], stops_after=2)
        self.assertEqual(handle.await_count, 1)                    # nach dem Fehler ging es weiter
        self.assertEqual(sleep.await_args_list[0].args, (5,))
        self.assertIn("Unerwarteter Fehler bei der MQTT-Verbindung", logged.output[0])
        self.assertIn("Netzwerk nicht erreichbar", logged.output[0])

    async def test_stopping_the_task_is_still_possible(self):
        app = self.make_app()
        broker, *_ = await self.run_listener(app, [])              # CancelledError wird nicht verschluckt
        self.assertEqual(len(broker.clients), 1)


if __name__ == "__main__":
    unittest.main()
