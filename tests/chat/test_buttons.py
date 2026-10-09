import unittest


from bos_telegram_bot.chat import admin, overview
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase, reply_buttons
from tests.helpers import make_update, sent_text


class CancelTests(CommandTestCase):
    async def test_every_page_of_the_result_list_has_a_cancel_button(self):
        reply = await self.command("ric", "Musterstadt", kind="add")
        self.assertIn(("✖️ Abbrechen", "cancel:ric"), reply_buttons(reply))

    async def test_cancel_is_the_last_row_on_its_own(self):
        reply = await self.command("ric", "Musterstadt", kind="add")
        rows = reply.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([b.callback_data for b in rows[-1]], ["cancel:ric"])

    async def test_cancel_closes_the_menu_without_subscribing(self):
        await self.command("ric", "Musterstadt", kind="add")
        query = await self.click("cancel:ric")
        self.assertIn("Abgebrochen", sent_text(query.edit_message_text))
        self.assertIn("nichts abonniert", sent_text(query.edit_message_text))
        self.assertEqual(self.subs(), [])

    async def test_cancel_forgets_the_search(self):
        await self.command("ric", "Musterstadt", kind="add")
        await self.click("cancel:ric")
        self.assertNotIn("search:ric", self.env.context.user_data)
        self.assertNotIn("term:ric", self.env.context.user_data)
        for data in ("add:ric:0", "page:ric:0", "pat:ric"):
            query = await self.click(data)
            self.assertIn("abgelaufen", sent_text(query.edit_message_text), data)
        self.assertEqual(self.subs(), [])

    async def test_cancel_after_no_hit(self):
        reply = await self.command("ric", "Unbekannt", kind="add")
        self.assertIn(("✖️ Abbrechen", "cancel:ric"), reply_buttons(reply))
        query = await self.click("cancel:ric")
        self.assertIn("Abgebrochen", sent_text(query.edit_message_text))
        self.assertEqual(self.subs(), [])

    async def test_cancel_works_twice_and_without_a_search(self):
        for _ in range(2):
            query = await self.click("cancel:ric")
            self.assertIn("Abgebrochen", sent_text(query.edit_message_text))

    async def test_cancel_for_an_unknown_field_is_harmless(self):
        query = await self.click("cancel:gibtsnicht")
        self.assertIn("Abgebrochen", sent_text(query.edit_message_text))

    async def test_a_new_search_after_cancel_works(self):
        await self.command("ric", "Musterstadt", kind="add")
        await self.click("cancel:ric")
        await self.command("ric", "Musterstadt", kind="add")
        await self.click("add:ric:0")
        self.assertEqual(self.subs()[0]["target"], "1234567")

    async def test_stranger_cannot_press_cancel(self):
        query = await self.click("cancel:ric", user=77)
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        query.edit_message_text.assert_not_called()

    async def test_users_menu_can_be_closed(self):
        db.set_user_status(self.env.db_path, 5, "allowed", "Erika")
        update, reply = make_update(1)
        await admin.users_handler(update, self.env.context)
        self.assertIn(("✔️ Schließen", "close_menu"), reply_buttons(reply))
        query = await self.click("close_menu")
        self.assertIn("geschlossen", sent_text(query.edit_message_text))
        self.assertEqual(db.get_user_status(self.env.db_path, 5), "allowed")

    async def test_users_without_buttons_have_no_empty_menu(self):
        update, reply = make_update(1)
        await admin.users_handler(update, self.env.context)
        self.assertIsNone(reply.call_args.kwargs["reply_markup"])

    async def test_abo_menu_keeps_its_close_button(self):
        await self.command("message", "THL*")
        update, reply = make_update()
        await overview.abo_handler(update, self.env.context)
        self.assertIn(("✔️ Alles passt", "close_menu"), reply_buttons(reply))


if __name__ == "__main__":
    unittest.main()
