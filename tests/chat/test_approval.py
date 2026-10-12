"""Nach der Freigabe erfährt der User, dass er jetzt Zugriff hat, auch wenn Telegram ihm nicht privat schreiben lässt."""
from unittest.mock import AsyncMock

from telegram.error import Forbidden

from bos_telegram_bot.chat import access, admin, buttons, overview
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase
from tests.helpers import FakeBot, make_callback, make_update, sent_text

ACCESS = "bos_telegram_bot.chat.access"
USER = 77
GROUP = -1001234567890
APPROVED = "✅ Du wurdest freigeschaltet. Schreibe /start für eine Übersicht."


class SelectiveBot(FakeBot):
    """Wie der Fake-Bot, aber Telegram verweigert die Zustellung an bestimmte Chats, so wie bei einem User, der
    dem Bot nie privat geschrieben hat."""

    def __init__(self, refused=()):
        super().__init__()
        self.refused = set(refused)
        self.attempts = []

    async def send_message(self, chat_id, text, **kwargs):
        self.attempts.append(chat_id)
        if chat_id in self.refused:
            raise Forbidden("bot can't initiate conversation with a user")
        await super().send_message(chat_id, text, **kwargs)


class ApprovalTestCase(CommandTestCase):
    def use_bot(self, refused=()):
        self.bot = SelectiveBot(refused)
        self.env.context.bot = self.bot
        return self.bot

    async def request(self, user=USER, chat=None, chat_type="private", name="Fremde Person"):
        """Ein nicht berechtigter User schreibt /start, im privaten Chat oder in einer Gruppe."""
        update, reply = make_update(user, chat_id=chat, chat_type=chat_type, name=name)
        await overview.start_handler(update, self.env.context)
        self.bot.sent.clear()                                        # die Anfrage an die Admins interessiert hier nicht
        self.bot.attempts.clear()
        return reply

    async def approve_by_button(self, user=USER):
        update, query = make_callback(f"acc:ok:{user}", user_id=1)
        await buttons.button_handler(update, self.env.context)
        return query.edit_message_text.call_args[0][0]

    def messages_to(self, chat):
        return [m for m in self.bot.sent if m["chat_id"] == chat]


class RequestChatIsRememberedTests(ApprovalTestCase):
    async def test_a_private_request_stores_the_private_chat(self):
        self.use_bot()
        await self.request()
        self.assertEqual(db.get_user(self.env.db_path, USER)["request_chat_id"], USER)

    async def test_a_request_from_a_group_stores_the_group(self):
        self.use_bot()
        await self.request(chat=GROUP, chat_type="supergroup")
        self.assertEqual(db.get_user(self.env.db_path, USER)["request_chat_id"], GROUP)

    async def test_a_second_attempt_while_pending_does_not_change_it(self):
        self.use_bot()
        await self.request(chat=GROUP, chat_type="supergroup")
        update, _ = make_update(USER, chat_id=-555, chat_type="supergroup")
        await overview.start_handler(update, self.env.context)           # "bereits gesendet", wird nicht neu angelegt
        self.assertEqual(db.get_user(self.env.db_path, USER)["request_chat_id"], GROUP)


class PrivateDeliveryTests(ApprovalTestCase):
    async def test_the_user_is_told_in_private_and_the_admin_text_is_unchanged(self):
        self.use_bot()
        await self.request()
        admin_text = await self.approve_by_button()
        self.assertEqual([m["text"] for m in self.messages_to(USER)], [APPROVED])
        self.assertEqual(admin_text, f"✅ Freigeschaltet: {USER}")           # kein Zusatz, wenn alles geklappt hat

    async def test_only_one_attempt_is_made_when_private_works(self):
        self.use_bot()
        await self.request(chat=GROUP, chat_type="supergroup")
        await self.approve_by_button()
        self.assertEqual(self.bot.attempts, [USER])                          # die Gruppe wird nicht zusätzlich belästigt

    async def test_the_user_is_really_allowed_afterwards(self):
        self.use_bot()
        await self.request()
        await self.approve_by_button()
        self.assertEqual(db.get_user_status(self.env.db_path, USER), "allowed")

    async def test_the_message_names_the_next_step(self):
        self.use_bot()
        await self.request()
        await self.approve_by_button()
        self.assertIn("/start", self.messages_to(USER)[0]["text"])


class FallbackToTheRequestChatTests(ApprovalTestCase):
    async def test_when_private_is_refused_the_message_goes_to_the_group_with_a_mention(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup")
        await self.approve_by_button()
        (message,) = self.messages_to(GROUP)
        self.assertEqual(message["parse_mode"], "HTML")
        self.assertIn(f'<a href="tg://user?id={USER}">Fremde Person</a>', message["text"])
        self.assertIn("wurde freigeschaltet", message["text"])
        self.assertIn("/start", message["text"])

    async def test_the_admin_is_told_where_the_message_went(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup")
        text = await self.approve_by_button()
        self.assertTrue(text.startswith(f"✅ Freigeschaltet: {USER}"))
        self.assertIn("Privat war er nicht erreichbar, die Meldung ging in den Chat seiner Anfrage", text)
        self.assertNotIn("⚠️", text)

    async def test_private_is_tried_first_then_the_group(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup")
        await self.approve_by_button()
        self.assertEqual(self.bot.attempts, [USER, GROUP])

    async def test_the_fallback_is_logged_at_info(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup")
        with self.assertLogs(ACCESS, level="INFO") as logged:
            await self.approve_by_button()
        self.assertTrue(any(f"Freigabe-Meldung für {USER} ging in den Chat {GROUP}" in line and line.startswith("INFO:")
                            for line in logged.output), logged.output)

    async def test_a_name_with_markup_cannot_change_the_message(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup", name='<b>Evil</b> & "Co"')
        await self.approve_by_button()
        text = self.messages_to(GROUP)[0]["text"]
        self.assertNotIn("<b>Evil", text)
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; &amp; &quot;Co&quot;", text)

    async def test_a_missing_name_falls_back_to_the_id(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup", name="")
        await self.approve_by_button()
        self.assertIn(f'<a href="tg://user?id={USER}">{USER}</a>', self.messages_to(GROUP)[0]["text"])

    async def test_the_mention_link_points_to_the_user_not_to_someone_else(self):
        self.use_bot(refused=[USER, 88])
        await self.request(chat=GROUP, chat_type="supergroup")
        await self.request(user=88, chat=-2002, chat_type="supergroup", name="Andere Person")
        await self.approve_by_button(user=88)
        self.assertIn("tg://user?id=88", self.messages_to(-2002)[0]["text"])
        self.assertEqual(self.messages_to(GROUP), [])


class NoWayToReachTheUserTests(ApprovalTestCase):
    async def test_a_private_request_that_cannot_be_delivered_warns_the_admin(self):
        self.use_bot(refused=[USER])
        await self.request()                                                 # Anfrage privat: es gibt keinen anderen Chat
        text = await self.approve_by_button()
        self.assertEqual(self.bot.attempts, [USER])                          # nicht zweimal an denselben Chat
        self.assertTrue(text.startswith(f"✅ Freigeschaltet: {USER}"))
        self.assertIn("⚠️ Der User konnte nicht benachrichtigt werden", text)
        self.assertIn("einmal privat /start schreiben", text)

    async def test_when_both_are_refused_the_admin_is_warned(self):
        self.use_bot(refused=[USER, GROUP])
        await self.request(chat=GROUP, chat_type="supergroup")
        text = await self.approve_by_button()
        self.assertEqual(self.bot.attempts, [USER, GROUP])
        self.assertIn("⚠️ Der User konnte nicht benachrichtigt werden", text)

    async def test_the_user_is_allowed_even_if_nobody_could_tell_him(self):
        self.use_bot(refused=[USER, GROUP])
        await self.request(chat=GROUP, chat_type="supergroup")
        await self.approve_by_button()
        self.assertEqual(db.get_user_status(self.env.db_path, USER), "allowed")

    async def test_the_undeliverable_message_is_logged_as_a_warning(self):
        self.use_bot(refused=[USER])
        await self.request()
        with self.assertLogs(ACCESS, level="WARNING") as logged:
            await self.approve_by_button()
        self.assertIn(f"Nachricht an {USER} nicht zustellbar (Forbidden", logged.output[0])

    async def test_a_user_from_an_old_database_without_a_request_chat(self):
        self.use_bot(refused=[USER])
        db.set_user_status(self.env.db_path, USER, "pending", "Alt-Nutzer")     # kein Anfrage-Chat bekannt
        text = await self.approve_by_button()
        self.assertEqual(self.bot.attempts, [USER])
        self.assertIn("⚠️", text)

    async def test_an_unknown_user_id_does_not_crash(self):
        self.use_bot(refused=[4242])
        result = await access.notify_approved(self.env.context, 4242)
        self.assertEqual(result, "none")


class AddUserTests(ApprovalTestCase):
    async def run_adduser(self, *args):
        update, reply = make_update(1)
        self.env.context.args = list(args)
        await admin.adduser_handler(update, self.env.context)
        return sent_text(reply)

    async def test_adduser_tells_the_user_and_keeps_the_admin_text(self):
        self.use_bot()
        text = await self.run_adduser("55", "Max", "Mustermann")
        self.assertEqual([m["text"] for m in self.messages_to(55)], [APPROVED])
        self.assertEqual(text, "✅ Freigeschaltet: <code>55</code>")

    async def test_adduser_warns_the_admin_when_the_user_cannot_be_reached(self):
        self.use_bot(refused=[55])
        text = await self.run_adduser("55")
        self.assertEqual(self.bot.attempts, [55])                            # es gibt keinen Anfrage-Chat
        self.assertTrue(text.startswith("✅ Freigeschaltet: <code>55</code>"))
        self.assertIn("⚠️ Der User konnte nicht benachrichtigt werden", text)

    async def test_adduser_for_a_user_who_asked_from_a_group_uses_that_group(self):
        self.use_bot(refused=[USER])
        await self.request(chat=GROUP, chat_type="supergroup")
        text = await self.run_adduser(str(USER))
        self.assertEqual(len(self.messages_to(GROUP)), 1)
        self.assertIn("Privat war er nicht erreichbar", text)


class ApprovalNoteTests(CommandTestCase):
    def test_nothing_is_added_when_it_worked(self):
        self.assertEqual(access.approval_note("private"), "")

    def test_each_case_has_its_own_note(self):
        self.assertIn("ℹ️", access.approval_note("chat"))
        self.assertIn("⚠️", access.approval_note("none"))

    def test_an_unknown_value_adds_nothing(self):
        self.assertEqual(access.approval_note("unbekannt"), "")


class DenialStaysSilentTests(ApprovalTestCase):
    async def test_a_denied_request_sends_nothing_to_the_user(self):
        self.use_bot()
        await self.request()
        update, query = make_callback(f"acc:no:{USER}", user_id=1)
        await buttons.button_handler(update, self.env.context)
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(db.get_user_status(self.env.db_path, USER), "denied")
