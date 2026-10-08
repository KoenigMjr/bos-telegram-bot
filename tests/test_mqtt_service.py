import unittest

import database as db
from matching import wildcard_to_regex
from services import mqtt_service as ms
from tests.helpers import Env

NOTIFY = ["description", "message", "ric"]


class DistributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env(self)

    async def alarm(self, payload):
        self.env.bot.sent.clear()
        app = type("App", (), {"bot": self.env.bot})()
        await ms.handle_payload(app, self.env.db_path, self.env.context.bot_data["active_fields"], NOTIFY, payload)
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


class NotificationTextTests(unittest.TestCase):
    def test_multicast_shows_every_entry_on_its_own_line(self):
        text = ms.build_notification_text(DistributionTests.MULTICAST, NOTIFY, ["Wache"])
        lines = text.split("\n")
        for expected in ("Wache Musterstadt", "Rettungswagen Musterstadt", "RD 1 - Test", "1000001", "1000002"):
            self.assertIn(expected, lines)

    def test_single_alarm_and_html_escaping(self):
        text = ms.build_notification_text({"description": "A & B <x>", "ric": "1"}, NOTIFY, ["Alias <b>"])
        self.assertIn("A &amp; B &lt;x&gt;", text)
        self.assertIn("abonniert über: Alias &lt;b&gt;", text)


class LearningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_learn_payload_fills_known_rows(self):
        env = Env(self)
        await ms.learn_payload(env.db_path, env.entries, DistributionTests.MULTICAST)
        learned = {r["for_value"]: r["add_value"] for r in db.list_learned(env.db_path, "ric")}
        self.assertEqual(learned, {"1000002": "Rettungswagen Musterstadt", "1000001": "Wache Musterstadt"})

    async def test_learning_errors_never_propagate(self):
        env = Env(self)
        await ms.learn_payload("/gibt/es/nicht/db.sqlite3", env.entries, {"ric": "1", "description": "A"})


if __name__ == "__main__":
    unittest.main()
