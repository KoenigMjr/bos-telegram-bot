import unittest


from bos_telegram_bot.chat import fields
from tests.chat.base import CommandTestCase
from tests.helpers import Env, make_update, sent_text


class HintTests(CommandTestCase):
    async def test_unknown_value_hint(self):
        text = sent_text(await self.command("ric", "9999999"))
        self.assertIn("kenne ich noch nicht", text)

    async def test_no_unknown_hint_when_nothing_is_known_yet(self):
        env_empty = Env(self)
        update, reply = make_update()
        env_empty.context.args = ["9999999"]
        await fields.make_for_handler(env_empty.entry("ric"))(update, env_empty.context)
        self.assertNotIn("kenne ich noch nicht", sent_text(reply))

    async def test_known_value_has_no_unknown_hint(self):
        self.assertNotIn("kenne ich noch nicht", sent_text(await self.command("ric", "1234567")))

    async def test_pattern_counts_known_entries(self):
        text = sent_text(await self.command("ric", "1234*"))
        self.assertIn("Trifft 1 bekannten Eintrag", text)
        self.assertIn("Rettungswagen Musterstadt A-Wehr", text)

    async def test_pattern_without_known_match(self):
        text = sent_text(await self.command("ric", "99*"))
        self.assertIn("kein passender Eintrag bekannt", text)
        self.assertIn("Wache-Muster der Liste werden dabei nicht geprüft", text)

    async def test_last_alarm_match(self):
        self.env.context.bot_data["last_payload"] = {"ric": "1234567", "message": "THL Tür"}
        self.assertIn("Passt auf den letzten Alarm", sent_text(await self.command("message", "THL*")))

    async def test_last_alarm_mismatch(self):
        self.env.context.bot_data["last_payload"] = {"ric": "1234567", "message": "RD 1"}
        self.assertIn("Passt nicht auf den letzten Alarm", sent_text(await self.command("message", "THL*")))

    async def test_last_alarm_uses_multicast_list(self):
        self.env.context.bot_data["last_payload"] = {"ric": "2", "ric_list": "1, 2"}
        self.assertIn("Passt auf den letzten Alarm", sent_text(await self.command("ric", "1")))

    async def test_last_alarm_without_field(self):
        self.env.context.bot_data["last_payload"] = {"ric": "1"}
        self.assertIn("enthält dieses Feld nicht", sent_text(await self.command("message", "THL*")))

    async def test_no_alarm_yet_no_hint(self):
        self.assertNotIn("letzten Alarm", sent_text(await self.command("message", "THL*")))

    async def test_subscription_is_created_even_with_warning(self):
        await self.command("ric", "9999999")
        self.assertEqual(len(self.subs()), 1)


if __name__ == "__main__":
    unittest.main()
