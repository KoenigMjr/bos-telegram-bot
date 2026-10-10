import unittest

from bos_telegram_bot.storage import database as db
from bos_telegram_bot.core.matching import wildcard_to_regex
from bos_telegram_bot.mqtt import dispatch
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE, template_from_fields
from bos_telegram_bot.core.delivery import DeliveryMemory
from tests.helpers import Clock, Env

NOTIFY = ["description", "message", "ric"]
TEMPLATE = template_from_fields(NOTIFY)   # der Aufbau früherer Versionen


class DistributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env(self)

    async def alarm(self, payload):
        self.env.bot.sent.clear()
        app = type("App", (), {"bot": self.env.bot})()
        await dispatch.handle_payload(app, self.env.db_path, self.env.context.bot_data["active_fields"], TEMPLATE, payload)
        return {m["chat_id"]: m["text"] for m in self.env.bot.sent}

    def sub(self, chat, field, target, alias="Alias", regex=False):
        db.add_sub(self.env.db_path, chat, field, target, alias, regex)

    MULTICAST = {
        "ric": "1000002", "ric_list": "1000001, 1000002",
        "description": "Rettungswagen Musterstadt", "description_list": "Wache Musterstadt, Rettungswagen Musterstadt",
        "message": "RD 1 - Test", "message_list": ", ",
    }

    async def test_subscription_matches_value_only_present_in_list(self):
        self.sub(11, "ric", "1000001", "Wache")
        self.assertEqual(list(await self.alarm(self.MULTICAST)), [11])

    async def test_primary_value_and_unrelated(self):
        self.sub(22, "ric", "1000002")
        self.sub(44, "ric", "1000099")
        self.assertEqual(list(await self.alarm(self.MULTICAST)), [22])

    async def test_one_message_per_chat_with_all_aliases(self):
        self.sub(33, "ric", "1000001", "Wache")
        self.sub(33, "ric", r"^10000([0-9]{2})$", r"Gruppe \1", True)
        self.sub(33, "message", ".*RD.*", "Stichwort RD", True)
        sent = await self.alarm(self.MULTICAST)
        self.assertEqual(list(sent), [33])
        self.assertEqual(sent[33].count("BOS-ALARM"), 1)
        self.assertIn("abonniert über: Wache, Gruppe 02, Stichwort RD", sent[33])

    async def test_description_with_comma_in_name_matches(self):
        payload = dict(self.MULTICAST, description_list="Wache Nord, Süd, Rettungswagen Musterstadt")
        self.sub(55, "description", "Wache Nord, Süd")
        self.assertEqual(list(await self.alarm(payload)), [55])

    async def test_pattern_on_message_and_description(self):
        self.sub(66, "message", wildcard_to_regex("thl*"), "THL", True)
        self.sub(77, "description", wildcard_to_regex("*WAGEN*"), "Wagen", True)
        sent = await self.alarm(dict(self.MULTICAST, message="THL Tür"))
        self.assertEqual(sorted(sent), [66, 77])

    async def test_payload_without_list_fields(self):
        self.sub(11, "ric", "1000001")
        self.assertEqual(list(await self.alarm({"ric": "1000001", "description": "X", "message": "Y"})), [11])

    async def test_field_missing_in_payload(self):
        self.sub(11, "ric", "1000001")
        self.assertEqual(await self.alarm({"message": "nur Text"}), {})

    async def test_inactive_field_is_ignored(self):
        self.sub(11, "altes_feld", "x")
        self.assertEqual(await self.alarm({"altes_feld": "x"}), {})

    async def test_empty_payload_value_for_exact_target(self):
        self.sub(11, "ric", "1000001")
        self.assertEqual(await self.alarm({"ric": ""}), {})


class TemplateInDistributionTests(unittest.IsolatedAsyncioTestCase):
    """Die konfigurierte Vorlage bestimmt den Aufbau der gesendeten Nachricht."""

    MULTICAST = {
        "ric": "1000043", "ric_list": "1000011, 1000043",
        "description": "1000043", "description_list": "Lagedienst Musterstadt, 1000043",
        "message": "TEST Beispieltext", "message_list": ", ",
    }

    async def send(self, template, payload=None):
        env = Env(self)
        db.add_sub(env.db_path, 11, "ric", "1000011", "RIC: 1000011", False)
        app = type("App", (), {"bot": env.bot})()
        await dispatch.handle_payload(app, env.db_path, env.context.bot_data["active_fields"], template,
                                payload or self.MULTICAST)
        return env.bot.sent[0]["text"]

    async def test_default_layout(self):
        text = await self.send(DEFAULT_TEMPLATE)
        self.assertEqual(text, "🚨 <b>BOS-ALARM</b> 🚨\n\nLagedienst Musterstadt\n1000043\nTEST Beispieltext"
                               "\n\n<i>abonniert über: RIC: 1000011</i>")

    async def test_ric_numbers_are_not_repeated_in_the_default_layout(self):
        text = await self.send(DEFAULT_TEMPLATE)
        self.assertEqual(text.count("1000011"), 1)      # nur im 'abonniert über'
        self.assertEqual(text.count("1000043"), 1)

    async def test_custom_template_is_used(self):
        text = await self.send("<b>{MESSAGE}</b>\n• {DESCRIPTION_LIST}\n{MATCHED}")
        self.assertEqual(text, "<b>TEST Beispieltext</b>\n• Lagedienst Musterstadt\n• 1000043\nRIC: 1000011")

    async def test_old_layout_from_notification_fields_is_unchanged(self):
        text = await self.send(TEMPLATE)
        lines = text.split("\n")
        for expected in ("Lagedienst Musterstadt", "1000043", "TEST Beispieltext", "1000011"):
            self.assertIn(expected, lines)


class NotificationTextTests(unittest.TestCase):
    def test_multicast_shows_every_entry_on_its_own_line(self):
        text = dispatch.build_notification_text(DistributionTests.MULTICAST, TEMPLATE, ["Wache"])
        lines = text.split("\n")
        for expected in ("Wache Musterstadt", "Rettungswagen Musterstadt", "RD 1 - Test", "1000001", "1000002"):
            self.assertIn(expected, lines)

    def test_single_alarm_and_html_escaping(self):
        text = dispatch.build_notification_text({"description": "A & B <x>", "ric": "1"}, TEMPLATE, ["Alias <b>"])
        self.assertIn("A &amp; B &lt;x&gt;", text)
        self.assertIn("abonniert über: Alias &lt;b&gt;", text)


class LearningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_learn_payload_fills_known_rows(self):
        env = Env(self)
        await dispatch.learn_payload(env.db_path, env.entries, DistributionTests.MULTICAST)
        learned = {r["for_value"]: r["add_value"] for r in db.list_learned(env.db_path, "ric")}
        self.assertEqual(learned, {"1000002": "Rettungswagen Musterstadt", "1000001": "Wache Musterstadt"})

    async def test_learning_errors_never_propagate(self):
        env = Env(self)
        with self.assertLogs("bos_telegram_bot.mqtt.dispatch", level="WARNING") as logged:
            await dispatch.learn_payload("/gibt/es/nicht/db.sqlite3", env.entries, {"ric": "1", "description": "A"})
        self.assertIn("Lernen fehlgeschlagen", logged.output[0])


class DescribeAlarmTests(unittest.TestCase):
    """Die Kennzeichnung für das INFO-Log: RIC und Name, nie der Alarmtext."""

    def test_single_alarm(self):
        self.assertEqual(dispatch.describe_alarm({"ric": "1000011", "description": "Wache Nord", "message": "THL 1"}),
                         "1000011 (Wache Nord)")

    def test_multicast_shows_all_rics_and_names(self):
        payload = {"ric": "1000043", "ric_list": "1000011, 1000043",
                   "description": "1000043", "description_list": "Lagedienst Musterstadt, 1000043"}
        self.assertEqual(dispatch.describe_alarm(payload), "1000011, 1000043 (Lagedienst Musterstadt)")

    def test_an_unresolved_name_that_is_just_the_ric_is_left_out(self):
        self.assertEqual(dispatch.describe_alarm({"ric": "1000011", "description": "1000011"}), "1000011")

    def test_many_recipients_are_shortened(self):
        payload = {"ric_list": "1, 2, 3, 4, 5, 6"}
        self.assertEqual(dispatch.describe_alarm(payload), "1, 2, 3 +3")

    def test_long_names_are_cut(self):
        label = dispatch.describe_alarm({"ric": "1", "description": "N" * 200})
        self.assertLessEqual(len(label), len("1 ()") + dispatch.MAX_NAME_LENGTH)
        self.assertTrue(label.endswith("…)"))

    def test_duplicate_entries_collapse(self):
        self.assertEqual(dispatch.describe_alarm({"ric_list": "1, 1, 2", "description_list": "A, A"}), "1, 2 (A)")

    def test_fms_and_zvei_have_no_ric(self):
        self.assertEqual(dispatch.describe_alarm({"fms": "12345678", "description": "Florian 1"}), "12345678 (Florian 1)")
        self.assertEqual(dispatch.describe_alarm({"tone": "12345"}), "12345")

    def test_nothing_known(self):
        self.assertEqual(dispatch.describe_alarm({"message": "nur Text"}), "ohne Kennung")
        self.assertEqual(dispatch.describe_alarm({}), "ohne Kennung")

    def test_the_message_is_never_part_of_the_label(self):
        label = dispatch.describe_alarm({"ric": "1", "message": "B 3 - Musterstraße 5", "description": "Wache"})
        self.assertNotIn("Musterstraße", label)


class FlakyBot:
    """Schickt nur an bestimmte Chats erfolgreich, an alle anderen wirft er den Fehler, den Telegram bei Zeitüberschreitung liefert."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.failing:
            from telegram.error import TimedOut
            raise TimedOut()
        self.sent.append(chat_id)


class AlarmLogTests(unittest.IsolatedAsyncioTestCase):
    LOGGER = "bos_telegram_bot.mqtt.dispatch"
    ALARM = {"ric": "1000011", "description": "Wache Nord", "message": "TEST Beispieltext (Musterstraße 5)"}

    def setUp(self):
        self.env = Env(self)
        db.add_sub(self.env.db_path, 11, "ric", "1000011", "RIC: 1000011", False)

    async def run_alarm(self, level, bot=None, payload=None):
        app = type("App", (), {"bot": bot or self.env.bot})()
        with self.assertLogs(self.LOGGER, level=level) as logged:
            await dispatch.handle_payload(app, self.env.db_path, self.env.context.bot_data["active_fields"],
                                          DEFAULT_TEMPLATE, payload or self.ALARM)
        return logged.output

    async def test_one_info_line_for_a_delivered_alarm(self):
        output = await self.run_alarm("INFO")
        self.assertEqual(output, [f"INFO:{self.LOGGER}:Alarm 1000011 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 1/1"])

    async def test_one_info_line_without_a_matching_subscription(self):
        output = await self.run_alarm("INFO", payload={"ric": "1000099", "description": "Wache Süd"})
        self.assertEqual(output, [f"INFO:{self.LOGGER}:Alarm 1000099 (Wache Süd): kein passendes Abo"])

    async def test_counts_subscriptions_and_chats_separately(self):
        db.add_sub(self.env.db_path, 11, "message", ".*Beispiel.*", "Stichwort", True)
        db.add_sub(self.env.db_path, 22, "ric", "1000011", "RIC: 1000011", False)
        output = await self.run_alarm("INFO")
        self.assertIn("3 Abo(s) in 2 Chat(s), gesendet 2/2", output[0])

    async def test_a_failed_send_is_a_warning_with_the_reason(self):
        output = await self.run_alarm("WARNING", bot=FlakyBot(failing=[11]))
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0], f"WARNING:{self.LOGGER}:Senden an Chat 11 fehlgeschlagen (TimedOut: Timed out)")
        self.assertIn("gesendet 0/1", output[1])
        self.assertTrue(output[1].startswith("WARNING:"))

    async def test_partial_failure_counts_what_arrived(self):
        db.add_sub(self.env.db_path, 22, "ric", "1000011", "RIC: 1000011", False)
        bot = FlakyBot(failing=[22])
        output = await self.run_alarm("INFO", bot=bot)
        self.assertEqual(bot.sent, [11])
        self.assertIn("gesendet 1/2", output[-1])
        self.assertTrue(output[-1].startswith("WARNING:"))

    async def test_a_send_failure_never_raises(self):
        await self.run_alarm("INFO", bot=FlakyBot(failing=[11]))     # kein Fehler nach außen

    async def test_the_alarm_text_never_appears_at_info(self):
        output = "\n".join(await self.run_alarm("INFO"))
        self.assertNotIn("Beispieltext", output)
        self.assertNotIn("Musterstraße", output)

    async def test_debug_adds_matches_message_and_timing(self):
        output = "\n".join(await self.run_alarm("DEBUG"))
        self.assertRegex(output, r"Abo \d+ passt \(Chat 11, Feld ric, Ziel '1000011'\): RIC: 1000011")
        self.assertIn("Nachricht an Chat 11:", output)
        self.assertIn("Musterstraße 5", output)     # der volle Text ist nur auf DEBUG im Log
        self.assertRegex(output, r"Chat 11: gesendet in \d+\.\d\d s")

    async def test_debug_mentions_subscriptions_of_removed_fields(self):
        db.add_sub(self.env.db_path, 33, "altes_feld", "x", "alt", False)
        output = "\n".join(await self.run_alarm("DEBUG"))
        self.assertIn("ruht: Feld 'altes_feld' ist nicht mehr konfiguriert", output)

    async def test_multicast_alarm_label(self):
        payload = {"ric": "1000043", "ric_list": "1000011, 1000043", "description": "1000043",
                   "description_list": "Lagedienst Musterstadt, 1000043"}
        output = await self.run_alarm("INFO", payload=payload)
        self.assertIn("Alarm 1000011, 1000043 (Lagedienst Musterstadt): 1 Abo(s)", output[0])

    async def test_learning_failure_is_a_warning_and_never_raises(self):
        with self.assertLogs(self.LOGGER, level="WARNING") as logged:
            await dispatch.learn_payload("/gibt/es/nicht/db.sqlite3", self.env.entries, {"ric": "1", "description": "A"})
        self.assertIn("Lernen fehlgeschlagen", logged.output[0])


# Ein Multicast mit zwei Empfängern, wie ihn BOSWatch3 veröffentlicht: der zusammengeführte Alarm geht auf
# das Topic jedes Empfängers, und eine Automation veröffentlicht jede Nachricht danach noch einmal.
MERGED = {
    "ric_list": "1000011, 1000043", "description_list": "Wache Nord, Lagedienst Musterstadt",
    "timestamp_list": "1700000000.1, 1700000000.4", "message": "TEST Beispieltext",
    "multicastMode": "complete", "multicastRecipientCount": "2",
}
FIRST = dict(MERGED, ric="1000011", description="Wache Nord", timestamp="1700000000.1", multicastRecipientIndex="1")
SECOND = dict(MERGED, ric="1000043", description="Lagedienst Musterstadt", timestamp="1700000000.4", multicastRecipientIndex="2")
SINGLE = {"ric": "1000011", "description": "Wache Nord", "message": "TEST", "timestamp": "1700000500.5", "timestamp_list": "1700000500.5"}


def copy_of(payload):
    """Die zweite Veröffentlichung derselben Nachricht durch eine Automation."""
    return dict(payload, republished=True)


class DuplicateTests(unittest.IsolatedAsyncioTestCase):
    LOGGER = "bos_telegram_bot.mqtt.dispatch"

    def setUp(self):
        self.env = Env(self)
        self.clock = Clock()
        self.memory = DeliveryMemory(300, clock=self.clock)

    def sub(self, chat, field, target):
        db.add_sub(self.env.db_path, chat, field, target, f"{field}: {target}", False)

    async def deliver(self, payload, bot=None, memory="default"):
        """Eine MQTT-Nachricht verarbeiten. Gibt die Chats zurück, an die gesendet wurde."""
        bot = bot or FlakyBot()
        memory = self.memory if memory == "default" else memory
        app = type("App", (), {"bot": bot})()
        await dispatch.handle_payload(app, self.env.db_path, self.env.context.bot_data["active_fields"],
                                      DEFAULT_TEMPLATE, payload, memory)
        return bot.sent

    async def test_an_alarm_and_its_copy_are_sent_once(self):
        self.sub(11, "ric", "1000011")
        sent = []
        for message in (SINGLE, copy_of(SINGLE)):
            sent += await self.deliver(message)
        self.assertEqual(sent, [11])

    async def test_a_copy_without_the_republished_flag_is_recognized_too(self):
        self.sub(11, "ric", "1000011")
        sent = await self.deliver(SINGLE) + await self.deliver(dict(SINGLE))
        self.assertEqual(sent, [11])

    async def test_four_messages_of_a_multicast_reach_every_chat_exactly_once(self):
        self.sub(11, "ric", "1000011")
        self.sub(11, "ric", "1000043")
        self.sub(22, "ric", "1000043")
        sent = []
        for message in (FIRST, SECOND, copy_of(FIRST), copy_of(SECOND)):       # Reihenfolge wie im echten Log
            sent += await self.deliver(message)
        self.assertEqual(sorted(sent), [11, 22])

    async def test_each_chat_gets_one_message_even_if_it_only_matches_the_second_topic(self):
        self.sub(22, "ric", "1000043")
        sent = []
        for message in (FIRST, SECOND, copy_of(FIRST), copy_of(SECOND)):
            sent += await self.deliver(message)
        self.assertEqual(sent, [22])

    async def test_info_shows_one_line_for_the_whole_burst(self):
        self.sub(11, "ric", "1000011")
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            for message in (FIRST, SECOND, copy_of(FIRST), copy_of(SECOND)):
                await self.deliver(message)
        self.assertEqual(len(logged.output), 1)
        self.assertIn("Alarm 1000011, 1000043 (Wache Nord, Lagedienst Musterstadt): 1 Abo(s) in 1 Chat(s), gesendet 1/1", logged.output[0])

    async def test_debug_explains_what_was_skipped(self):
        self.sub(11, "ric", "1000011")
        await self.deliver(SINGLE)
        with self.assertLogs(self.LOGGER, level="DEBUG") as logged:
            await self.deliver(copy_of(SINGLE))
        text = "\n".join(logged.output)
        self.assertIn("Chat 11: Alarm schon zugestellt, übersprungen", text)
        self.assertIn("Wiederholung, alle 1 Chat(s) schon beliefert", text)

    async def test_a_failed_send_is_tried_again_by_the_copy(self):
        self.sub(11, "ric", "1000011")
        self.assertEqual(await self.deliver(SINGLE, bot=FlakyBot(failing=[11])), [])
        self.assertFalse(self.memory.is_delivered("1700000500.5", 11))          # gescheitert zählt nicht als zugestellt
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            sent = await self.deliver(copy_of(SINGLE))
        self.assertEqual(sent, [11])
        self.assertIn("Wiederholung, 1 Chat(s) noch offen, gesendet 1/1", logged.output[0])
        self.assertEqual(await self.deliver(copy_of(SINGLE)), [])                # jetzt ist es zugestellt

    async def test_only_the_chat_that_failed_gets_the_retry(self):
        self.sub(11, "ric", "1000011")
        self.sub(22, "ric", "1000011")
        first = await self.deliver(SINGLE, bot=FlakyBot(failing=[22]))
        self.assertEqual(first, [11])
        self.assertEqual(await self.deliver(copy_of(SINGLE)), [22])

    async def test_a_retry_that_fails_again_is_a_warning_and_is_tried_once_more(self):
        self.sub(11, "ric", "1000011")
        await self.deliver(SINGLE, bot=FlakyBot(failing=[11]))
        with self.assertLogs(self.LOGGER, level="WARNING") as logged:
            await self.deliver(copy_of(SINGLE), bot=FlakyBot(failing=[11]))
        self.assertTrue(any("Wiederholung, 1 Chat(s) noch offen, gesendet 0/1" in line for line in logged.output))
        self.assertEqual(await self.deliver(copy_of(SINGLE)), [11])

    async def test_a_copy_without_its_original_is_delivered_normally(self):
        self.sub(11, "ric", "1000011")
        self.assertEqual(await self.deliver(copy_of(SINGLE)), [11])

    async def test_a_chat_subscribed_after_the_original_gets_the_copy(self):
        self.sub(11, "ric", "1000011")
        sent = await self.deliver(SINGLE)
        self.sub(22, "ric", "1000011")
        sent += await self.deliver(copy_of(SINGLE))
        self.assertEqual(sent, [11, 22])

    async def test_different_alarms_for_the_same_ric_are_both_delivered(self):
        self.sub(11, "ric", "1000011")
        later = dict(SINGLE, timestamp="1700000900.9", timestamp_list="1700000900.9")
        self.assertEqual(await self.deliver(SINGLE) + await self.deliver(later), [11, 11])

    async def test_an_alarm_without_timestamp_is_never_taken_for_a_duplicate(self):
        self.sub(11, "ric", "1000011")
        payload = {"ric": "1000011", "description": "Wache Nord"}
        self.assertEqual(await self.deliver(payload) + await self.deliver(dict(payload)), [11, 11])

    async def test_the_same_alarm_is_delivered_again_after_the_memory_expired(self):
        self.sub(11, "ric", "1000011")
        sent = await self.deliver(SINGLE)
        self.clock.advance(301)
        sent += await self.deliver(copy_of(SINGLE))
        self.assertEqual(sent, [11, 11])

    async def test_without_a_memory_every_message_is_delivered(self):
        self.sub(11, "ric", "1000011")
        sent = await self.deliver(SINGLE, memory=None) + await self.deliver(copy_of(SINGLE), memory=None)
        self.assertEqual(sent, [11, 11])

    async def test_no_subscriber_is_reported_once_per_alarm(self):
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            await self.deliver(FIRST)
            await self.deliver(SECOND)
            await self.deliver(copy_of(FIRST))
        self.assertEqual(len(logged.output), 1)
        self.assertIn("kein passendes Abo", logged.output[0])

    async def test_a_repeat_is_reported_at_debug_when_nobody_subscribed(self):
        await self.deliver(SINGLE)
        with self.assertLogs(self.LOGGER, level="DEBUG") as logged:
            await self.deliver(copy_of(SINGLE))
        self.assertIn("Wiederholung, weiterhin kein passendes Abo", logged.output[0])


if __name__ == "__main__":
    unittest.main()
