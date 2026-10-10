import unittest

from bos_telegram_bot.chat import admin, overview
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase, buttons_of
from tests.helpers import Env, make_update, sent_text


class AccessTests(CommandTestCase):
    def setUp(self):
        self.env = Env(self, admins=(1,))

    async def start(self, user):
        update, reply = make_update(user, name="Fremde Person")
        await overview.start_handler(update, self.env.context)
        return reply

    def admin_messages(self):
        return [m for m in self.env.bot.sent if m["chat_id"] == 1]

    async def test_admin_passes(self):
        reply = await self.start(1)
        self.assertIn("BOS-Alarm-Bot aktiv", sent_text(reply))

    async def test_stranger_gets_id_and_admin_gets_request(self):
        reply = await self.start(77)
        self.assertIn("77", sent_text(reply))
        self.assertEqual(db.get_user_status(self.env.db_path, 77), "pending")
        (message,) = self.admin_messages()
        self.assertEqual([d for _, d in buttons_of(message)], ["acc:ok:77", "acc:no:77"])

    async def test_repeated_attempt_does_not_spam_admins(self):
        await self.start(77)
        reply = await self.start(77)
        self.assertIn("wartet auf Freigabe", sent_text(reply))
        self.assertEqual(len(self.admin_messages()), 1)

    async def test_approval_unlocks_and_notifies_user(self):
        await self.start(77)
        await self.click("acc:ok:77")
        self.assertEqual(db.get_user_status(self.env.db_path, 77), "allowed")
        self.assertTrue(any(m["chat_id"] == 77 for m in self.env.bot.sent))
        self.assertIn("BOS-Alarm-Bot aktiv", sent_text(await self.start(77)))

    async def test_denied_user_is_ignored_silently(self):
        await self.start(77)
        await self.click("acc:no:77")
        sent_before = len(self.env.bot.sent)
        reply = await self.start(77)
        reply.assert_not_called()
        self.assertEqual(len(self.env.bot.sent), sent_before)

    async def test_stale_approval_button(self):
        await self.start(77)
        await self.click("acc:ok:77")
        query = await self.click("acc:ok:77")
        self.assertIn("bereits bearbeitet", sent_text(query.edit_message_text))

    async def test_normal_user_cannot_press_admin_buttons(self):
        db.set_user_status(self.env.db_path, 5, "allowed")
        query = await self.click("usr:del:1", user=5)
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        self.assertEqual(db.get_user_status(self.env.db_path, 5), "allowed")

    async def test_removal_deletes_private_but_not_group_subscriptions(self):
        db.set_user_status(self.env.db_path, 77, "allowed")
        db.add_sub(self.env.db_path, 77, "ric", "1", "privat", False)
        db.add_sub(self.env.db_path, -100, "ric", "1", "gruppe", False)
        await self.click("usr:del:77")
        self.assertIsNone(db.get_user_status(self.env.db_path, 77))
        self.assertEqual(self.env.subs(77), [])
        self.assertEqual(len(self.env.subs(-100)), 1)

    async def test_users_and_adduser_are_admin_only(self):
        db.set_user_status(self.env.db_path, 5, "allowed")
        update, reply = make_update(5)
        await admin.users_handler(update, self.env.context)
        self.assertIn("Nur für Admins", sent_text(reply))

        update, reply = make_update(1)
        self.env.context.args = ["555", "Erika", "Test"]
        await admin.adduser_handler(update, self.env.context)
        self.assertEqual(db.get_user_status(self.env.db_path, 555), "allowed")
        self.env.context.args = []
        await admin.users_handler(update, self.env.context)
        self.assertIn("Erika Test", sent_text(reply))

        self.env.context.args = ["abc"]
        await admin.adduser_handler(update, self.env.context)
        self.assertIn("Ungültige ID", sent_text(reply))


if __name__ == "__main__":
    unittest.main()
