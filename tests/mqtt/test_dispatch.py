import asyncio
import unittest

from telegram.error import TimedOut

from bos_telegram_bot.storage import database as db
from bos_telegram_bot.core.matching import wildcard_to_regex
from bos_telegram_bot.mqtt import dispatch
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE, template_from_fields
from bos_telegram_bot.core.delivery import DeliveryMemory
from tests.helpers import Clock, Env, ScriptedBot, SendHarness

NOTIFY = ["description", "message", "ric"]
TEMPLATE = template_from_fields(NOTIFY)   # der Aufbau früherer Versionen


class DistributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env(self)
        self.harness = SendHarness(self.env.bot)

    async def alarm(self, payload):
        self.env.bot.sent.clear()
        await self.harness.handle(self.env.db_path, self.env.context.bot_data["active_fields"], TEMPLATE, payload)
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
        await SendHarness(env.bot).handle(env.db_path, env.context.bot_data["active_fields"], template,
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

    def test_names_are_never_cut_in_the_middle_the_rest_is_counted(self):
        names = ["A" * 25, "B" * 25, "C" * 25, "D" * 25]
        label = dispatch.describe_alarm({"ric": "1", "description_list": ", ".join(names)})
        self.assertEqual(label, f"1 ({'A' * 25}, {'B' * 25} +2 weitere)")

    def test_a_name_that_fits_exactly_is_still_shown(self):
        limit = dispatch.MAX_NAME_LENGTH
        fits = ["A" * 30, "B" * (limit - 32)]                       # 30 + 2 + (limit - 32) = limit
        self.assertEqual(dispatch.describe_alarm({"ric": "1", "description_list": ", ".join(fits)}), f"1 ({fits[0]}, {fits[1]})")

    def test_one_character_more_and_the_name_moves_to_the_count(self):
        limit = dispatch.MAX_NAME_LENGTH
        names = ["A" * 30, "B" * (limit - 31)]                      # ein Zeichen zu lang
        self.assertEqual(dispatch.describe_alarm({"ric": "1", "description_list": ", ".join(names)}), f"1 ({'A' * 30} +1 weitere)")

    def test_a_single_name_that_is_too_long_is_shortened_and_the_others_counted(self):
        label = dispatch.describe_alarm({"ric": "1", "description_list": "A" * 100 + ", Kurz"})
        self.assertEqual(label, f"1 ({'A' * (dispatch.MAX_NAME_LENGTH - 1)}… +1 weitere)")

    def test_names_with_brackets_never_leave_a_bracket_open(self):
        names = [f"FW0{i} Nord (Wache-AB)" for i in range(1, 8)]
        label = dispatch.describe_alarm({"ric_list": ", ".join(str(i) for i in range(1, 8)), "description_list": ", ".join(names)})
        self.assertEqual(label.count("("), label.count(")"), label)
        self.assertTrue(label.endswith(" weitere)"), label)

    def test_the_first_name_is_always_shown(self):
        self.assertTrue(dispatch.describe_alarm({"ric": "1", "description_list": "Erster, Zweiter"}).startswith("1 (Erster"))

    def test_the_count_of_the_rest_ignores_duplicates_and_names_equal_to_a_ric(self):
        names = ["A" * 40, "A" * 40, "1", "B" * 40, "C" * 40]       # nach dem Zusammenfassen: A, B, C
        label = dispatch.describe_alarm({"ric": "1", "description_list": ", ".join(names)})
        self.assertEqual(label, f"1 ({'A' * 40} +2 weitere)")

    def test_the_label_stays_short_even_with_many_long_names(self):
        label = dispatch.describe_alarm({"ric_list": "1, 2, 3", "description_list": ", ".join("N" * 48 + f"{i:02d}" for i in range(30))})
        self.assertLessEqual(len(label), len("1, 2, 3 ()") + dispatch.MAX_NAME_LENGTH + len(" +99 weitere"))
        self.assertIn("+29 weitere", label)

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
    ALL = "bos_telegram_bot"          # alle Logger des Bots: die Verteilung und der Sender
    ALARM = {"ric": "1000011", "description": "Wache Nord", "message": "TEST Beispieltext (Musterstraße 5)"}

    def setUp(self):
        self.env = Env(self)
        db.add_sub(self.env.db_path, 11, "ric", "1000011", "RIC: 1000011", False)

    async def run_alarm(self, level, bot=None, payload=None):
        harness = SendHarness(bot or self.env.bot, attempts=1)
        with self.assertLogs(self.ALL, level=level) as logged:
            await harness.handle(self.env.db_path, self.env.context.bot_data["active_fields"],
                                 DEFAULT_TEMPLATE, payload or self.ALARM)
        return logged.output

    async def test_one_info_line_for_a_delivered_alarm(self):
        output = await self.run_alarm("INFO")
        self.assertEqual(output, [f"INFO:{self.LOGGER}:Alarm 1000011 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 1/1 in 0.0 s"])

    async def test_one_info_line_without_a_matching_subscription(self):
        output = await self.run_alarm("INFO", payload={"ric": "1000099", "description": "Wache Süd"})
        self.assertEqual(output, [f"INFO:{self.LOGGER}:Alarm 1000099 (Wache Süd): kein passendes Abo"])

    async def test_counts_subscriptions_and_chats_separately(self):
        db.add_sub(self.env.db_path, 11, "message", ".*Beispiel.*", "Stichwort", True)
        db.add_sub(self.env.db_path, 22, "ric", "1000011", "RIC: 1000011", False)
        output = await self.run_alarm("INFO")
        self.assertIn("3 Abo(s) in 2 Chat(s), gesendet 2/2", output[0])

    async def test_a_failed_send_is_logged_with_the_reason_and_the_alarm_ends_with_a_warning(self):
        output = await self.run_alarm("WARNING", bot=FlakyBot(failing=[11]))
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0], "ERROR:bos_telegram_bot.chat.sender:Senden an Chat 11 aufgegeben nach 1 Versuchen (TimedOut: Timed out)")
        self.assertEqual(output[1], f"WARNING:{self.LOGGER}:Alarm 1000011 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 0/1 in 0.0 s")

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

    async def test_the_summary_comes_once_after_the_last_chat_has_a_result(self):
        for chat in (22, 33):
            db.add_sub(self.env.db_path, chat, "ric", "1000011", "RIC: 1000011", False)
        output = await self.run_alarm("INFO")
        self.assertEqual(len(output), 1)
        self.assertIn("3 Abo(s) in 3 Chat(s), gesendet 3/3", output[0])

    async def test_debug_adds_matches_message_and_timing(self):
        output = "\n".join(await self.run_alarm("DEBUG"))
        self.assertRegex(output, r"Abo \d+ passt \(Chat 11, Feld ric, Ziel '1000011'\): RIC: 1000011")
        self.assertIn("Nachricht an Chat 11:", output)
        self.assertIn("Musterstraße 5", output)     # der volle Text ist nur auf DEBUG im Log
        self.assertRegex(output, r"Chat 11: gesendet in \d+\.\d\d s")

    async def test_the_message_text_is_a_single_formatted_log_line(self):
        """Die fertige Nachricht ist mehrzeilig. Im Log, wie Docker es sieht, muss sie trotzdem eine Zeile sein."""
        import contextlib
        import io

        from bos_telegram_bot import logs
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            logs.setup_logging("DEBUG")
            self.addCleanup(logs.reset_logging)
            await SendHarness(self.env.bot).handle(self.env.db_path, self.env.context.bot_data["active_fields"],
                                                   DEFAULT_TEMPLATE, self.ALARM)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        message = [line for line in lines if "Nachricht an Chat 11:" in line]
        self.assertEqual(len(message), 1)
        self.assertIn("Wache Nord ⏎ TEST Beispieltext (Musterstraße 5)", message[0])
        self.assertTrue(all(line[:4].isdigit() for line in lines), [line for line in lines if not line[:4].isdigit()])


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

    async def deliver(self, payload, bot=None, memory="default", attempts=1):
        """Eine MQTT-Nachricht verarbeiten und warten, bis sie zugestellt oder endgültig gescheitert ist.
        Gibt die Chats zurück, an die gesendet wurde. attempts=1: ein Fehlschlag ist gleich endgültig."""
        bot = bot or FlakyBot()
        memory = self.memory if memory == "default" else memory
        await SendHarness(bot, attempts=attempts).handle(self.env.db_path, self.env.context.bot_data["active_fields"],
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
        self.assertIn("Chat 11: Alarm schon zugestellt oder in Arbeit, übersprungen", text)
        self.assertIn("Wiederholung, alle 1 Chat(s) schon beliefert oder in Arbeit", text)

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


class QueuedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Die Verteilung reiht nur ein. Jetzt kann eine Kopie eintreffen, während das Original noch wartet."""
    LOGGER = "bos_telegram_bot.mqtt.dispatch"
    ALL = "bos_telegram_bot"

    def setUp(self):
        self.env = Env(self)
        self.memory = DeliveryMemory(300)
        db.add_sub(self.env.db_path, 11, "ric", "1000011", "RIC: 1000011", False)

    def harness(self, script=None, **settings):
        self.h = SendHarness(ScriptedBot(None, script), **settings)
        self.h.bot.clock = self.h.time.clock
        return self.h

    async def feed(self, payload, memory="default"):
        """Verteilt einen Alarm, wartet aber NICHT, bis er zugestellt ist (wie der Empfang es auch nicht tut)."""
        memory = self.memory if memory == "default" else memory
        await dispatch.handle_payload(self.env.db_path, self.env.context.bot_data["active_fields"], DEFAULT_TEMPLATE,
                                      payload, self.h.sender, memory)

    async def finish(self):
        await self.h.time.run_until_idle(self.h.sender)

    async def test_handle_payload_returns_before_anything_is_sent(self):
        self.harness()
        await self.feed(SINGLE)
        self.assertEqual(self.h.bot.calls, [])                      # nur eingereiht
        self.assertEqual(self.h.sender.pending, 1)
        await self.finish()
        self.assertEqual(len(self.h.bot.texts(11)), 1)

    async def test_a_copy_arriving_while_the_original_is_in_flight_is_not_queued_again(self):
        gate = asyncio.get_running_loop().create_future()
        self.harness(script={11: [gate]})
        await self.feed(SINGLE)
        await self.h.time.settle()                                  # das Original hängt bei Telegram
        self.assertEqual(len(self.h.bot.calls), 1)
        await self.feed(copy_of(SINGLE))
        self.assertEqual(self.h.sender.pending, 1)                  # die Kopie wurde nicht eingereiht
        gate.set_result(None)
        await self.finish()
        self.assertEqual(len(self.h.bot.calls), 1)
        self.assertTrue(self.memory.is_delivered("1700000500.5", 11))

    async def test_a_copy_arriving_while_the_original_is_still_waiting_is_not_queued_again(self):
        """Das Original steht hinter einer anderen Nachricht an denselben Chat, die Kopie darf nicht dazukommen."""
        self.harness()
        other = dict(SINGLE, timestamp="1700000100.1", timestamp_list="1700000100.1")
        await self.feed(other)
        await self.feed(SINGLE)                                     # muss eine Sekunde Abstand zu 'other' halten
        await self.feed(copy_of(SINGLE))
        self.assertEqual(len(self.h.bot.calls), 1)                  # nur 'other' ist raus, das Original wartet noch
        self.assertEqual(self.h.sender.pending, 1)                  # und die Kopie wurde nicht zusätzlich eingereiht
        await self.finish()
        self.assertEqual(len(self.h.bot.calls), 2)                  # 'other' und das Original, die Kopie nicht
        self.assertEqual(self.h.bot.times(11)[1] - self.h.bot.times(11)[0], 1.0)

    async def test_a_copy_during_the_retry_pause_does_not_duplicate_the_message(self):
        self.harness(script={11: [TimedOut(), None]})
        with self.assertLogs(self.ALL, level="WARNING"):
            await self.feed(SINGLE)
            await self.h.time.settle()                              # erster Versuch gescheitert, Pause läuft
            self.assertEqual(len(self.h.bot.calls), 1)
            await self.feed(copy_of(SINGLE))
            await self.finish()
        self.assertEqual(len(self.h.bot.calls), 2)                  # der Wiederholungsversuch, keine zweite Nachricht
        self.assertTrue(self.memory.is_delivered("1700000500.5", 11))

    async def test_after_a_final_failure_the_next_copy_may_try_again(self):
        self.harness(script={11: [TimedOut(), TimedOut()]}, attempts=2)
        await self.feed(SINGLE)
        with self.assertLogs(self.ALL, level="ERROR"):
            await self.finish()
        self.assertEqual(len(self.h.bot.calls), 2)
        self.assertFalse(self.memory.is_pending("1700000500.5", 11))      # Reservierung wurde freigegeben
        self.assertFalse(self.memory.is_delivered("1700000500.5", 11))
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            await self.feed(copy_of(SINGLE))
            await self.finish()
        self.assertEqual(len(self.h.bot.calls), 3)
        self.assertIn("Wiederholung, 1 Chat(s) noch offen, gesendet 1/1", logged.output[0])
        self.assertTrue(self.memory.is_delivered("1700000500.5", 11))

    async def test_while_the_chat_is_pending_a_later_copy_is_skipped_and_logged_at_debug(self):
        gate = asyncio.get_running_loop().create_future()
        self.harness(script={11: [gate]})
        await self.feed(SINGLE)
        await self.h.time.settle()
        with self.assertLogs(self.LOGGER, level="DEBUG") as logged:
            await self.feed(copy_of(SINGLE))
        text = "\n".join(logged.output)
        self.assertIn("Chat 11: Alarm schon zugestellt oder in Arbeit, übersprungen", text)
        self.assertIn("Wiederholung, alle 1 Chat(s) schon beliefert oder in Arbeit", text)
        gate.set_result(None)
        await self.finish()

    async def test_the_summary_waits_for_the_slowest_chat_and_names_the_total_time(self):
        for chat in (22, 33):
            db.add_sub(self.env.db_path, chat, "ric", "1000011", "RIC: 1000011", False)
        gate = asyncio.get_running_loop().create_future()
        self.harness(script={33: [gate]})
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            await self.feed(SINGLE)
            await self.h.time.run_until(lambda: self.h.sender.pending == 1)   # zwei fertig, Chat 33 hängt noch
            self.h.time.now += 3.0
            gate.set_result(None)
            await self.finish()
        self.assertEqual(len(logged.output), 1)                              # erst jetzt, einmal
        self.assertRegex(logged.output[0], r"3 Abo\(s\) in 3 Chat\(s\), gesendet 3/3 in 3\.\d s")

    async def test_the_summary_time_includes_the_retry_pause(self):
        self.harness(script={11: [TimedOut(), None]})
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            await self.feed(SINGLE)
            with self.assertLogs("bos_telegram_bot.chat.sender", level="WARNING"):
                await self.finish()
        self.assertEqual(logged.output, [
            f"INFO:{self.LOGGER}:Alarm 1000011 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 1/1 in 2.0 s"])

    async def test_the_default_settings_try_three_times(self):
        self.harness(script={11: [TimedOut(), TimedOut(), None]})
        await self.feed(SINGLE)
        with self.assertLogs(self.ALL, level="WARNING"):
            await self.finish()
        self.assertEqual(self.h.bot.times(11), [self.h.time.now - 7.0, self.h.time.now - 5.0, self.h.time.now])
        self.assertTrue(self.memory.is_delivered("1700000500.5", 11))

    async def test_a_chat_that_cannot_be_queued_does_not_stop_the_others(self):
        db.add_sub(self.env.db_path, 22, "ric", "1000011", "RIC: 1000011", False)
        self.harness()
        real = self.h.sender.submit

        def submit(chat_id, text, on_result):
            if chat_id == 22:
                raise RuntimeError("kaputt")
            real(chat_id, text, on_result)

        self.h.sender.submit = submit
        with self.assertLogs(self.ALL, level="WARNING") as logged:
            await self.feed(SINGLE)
            await self.finish()
        self.assertEqual(len(self.h.bot.texts()), 1)                         # Chat 11 bekam seinen Alarm
        self.assertFalse(self.memory.is_pending("1700000500.5", 22))        # und die Reservierung für 22 ist wieder frei
        self.assertTrue(any("Einreihen für Chat 22 fehlgeschlagen" in line for line in logged.output))
        self.assertTrue(any("gesendet 1/2" in line and line.startswith("WARNING:") for line in logged.output))

    async def test_without_a_memory_the_copy_is_queued_again(self):
        self.harness()
        await self.feed(SINGLE, memory=None)
        await self.feed(copy_of(SINGLE), memory=None)
        await self.finish()
        self.assertEqual(len(self.h.bot.calls), 2)

    async def test_a_burst_of_chats_leaves_nothing_behind(self):
        for chat in range(100, 160):
            db.add_sub(self.env.db_path, chat, "ric", "1000011", "RIC: 1000011", False)
        self.harness()
        await self.feed(SINGLE)
        await self.feed(copy_of(SINGLE))
        await self.finish()
        self.assertEqual(len(self.h.bot.calls), 61)                           # 60 + Chat 11, jeder genau einmal
        self.assertEqual(len({c["chat_id"] for c in self.h.bot.calls}), 61)
        self.assertEqual((self.h.sender.pending, self.h.sender.queued), (0, 0))


if __name__ == "__main__":
    unittest.main()
