import unittest

from bos_telegram_bot.chat import overview
from tests.chat.base import CommandTestCase
from tests.helpers import make_update, sent_text


class OverviewTests(CommandTestCase):
    async def test_start_lists_all_commands(self):
        update, reply = make_update()
        await overview.start_handler(update, self.env.context)
        text = sent_text(reply)
        for command in ("/ric", "/description", "/message", "/subrictext", "/abo", "/lastraw"):
            self.assertIn(command, text)

    async def test_start_in_group_mentions_shared_subscriptions(self):
        update, reply = make_update(chat_type="group", chat_id=-100, title="Gruppe")
        await overview.start_handler(update, self.env.context)
        self.assertIn("für alle Mitglieder", sent_text(reply))

    async def test_abo_lists_with_labels_and_deletes(self):
        await self.command("ric", "1234567")
        await self.command("message", "THL*")
        update, reply = make_update()
        await overview.abo_handler(update, self.env.context)
        text = sent_text(reply)
        self.assertIn("• RIC: Rettungswagen Musterstadt A-Wehr", text)
        self.assertIn("• Alarmstichwort: THL*", text)

        sub_id = self.subs()[0]["id"]
        await self.click(f"del:{sub_id}")
        self.assertEqual(len(self.subs()), 1)

    async def test_abo_does_not_repeat_the_field_name(self):
        await self.command("message", "B 3")
        await self.command("ric", "9999999")
        await self.command("ric", "*wagen*", kind="add")
        update, reply = make_update()
        await overview.abo_handler(update, self.env.context)
        text = sent_text(reply)
        for doubled in ("Alarmstichwort: Alarmstichwort", "RIC: RIC", "Fahrzeug / Wache: Fahrzeug / Wache", "["):
            self.assertNotIn(doubled, text)
        self.assertIn("• Alarmstichwort: B 3", text)
        self.assertIn("• RIC: 9999999", text)
        self.assertIn("• Fahrzeug / Wache: *wagen*", text)
        labels = [b.text for row in reply.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("🗑️ Alarmstichwort: B 3", labels)
        self.assertFalse(any("Alarmstichwort: Alarmstichwort" in label for label in labels))

    async def test_abo_empty_private_and_group(self):
        update, reply = make_update()
        await overview.abo_handler(update, self.env.context)
        self.assertIn("keine Abonnements", sent_text(reply))
        update, reply = make_update(chat_type="group", chat_id=-100, title="Gruppe")
        await overview.abo_handler(update, self.env.context)
        self.assertIn("Diese Gruppe", sent_text(reply))

    async def test_lastraw_suggests_unconfigured_fields(self):
        self.env.context.bot_data["last_payload"] = {
            "ric": "1", "message": "x", "description": "y", "timestamp": "1", "clientName": "c",
            "ric_list": "1", "stadtteil": "Nord", "objekt": "Halle 3"}
        update, reply = make_update()
        await overview.lastraw_handler(update, self.env.context)
        text = sent_text(reply)
        self.assertIn("EXTRA_FIELDS=stadtteil,objekt", text)
        self.assertNotIn("EXTRA_FIELDS=stadtteil,objekt,", text)

    async def test_lastraw_without_news_and_without_alarm(self):
        update, reply = make_update()
        await overview.lastraw_handler(update, self.env.context)
        self.assertIn("noch kein Alarm", sent_text(reply))
        self.env.context.bot_data["last_payload"] = {"ric": "1", "message": "x", "description": "y"}
        await overview.lastraw_handler(update, self.env.context)
        self.assertNotIn("EXTRA_FIELDS", sent_text(reply))


if __name__ == "__main__":
    unittest.main()
