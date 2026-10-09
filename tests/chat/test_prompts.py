import unittest
from types import SimpleNamespace as NS

from telegram import ForceReply

from bos_telegram_bot.chat import admin, fields, overview, prompts
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase, reply_buttons
from tests.helpers import bot_message, make_update, sent_text


class PromptTests(CommandTestCase):
    """Befehl ohne Eingabe: Anleitung plus Rückfrage, die Antwort führt den Befehl aus."""

    async def ask(self, for_key, kind="for", user=1, chat=None, chat_type="private"):
        update, reply = make_update(user, chat_id=chat, chat_type=chat_type,
                                    title=None if chat_type == "private" else "Gruppe")
        self.env.context.args = []
        make = fields.make_for_handler if kind == "for" else fields.make_add_handler
        await make(self.env.entry(for_key))(update, self.env.context)
        return reply

    def context_of(self, user):
        """Wie bei Telegram: bot_data ist gemeinsam, user_data gehört jeder Person einzeln."""
        if user == 1:
            return self.env.context
        base = self.env.context
        return NS(bot=base.bot, bot_data=base.bot_data, user_data={}, args=[])

    async def answer(self, prompt_reply, text, user=1, chat=None, chat_type="private"):
        """Antwort auf die Rückfrage, die 'prompt_reply' (Mock von reply_text) gesendet hat."""
        replied = bot_message(sent_text(prompt_reply), prompt_reply.return_value.message_id)
        update, reply = make_update(user, chat_id=chat, chat_type=chat_type, text=text, reply_to=replied)
        await prompts.prompt_reply_handler(update, self.context_of(user))
        return reply

    async def test_question_keeps_the_full_help_text(self):
        reply = await self.ask("ric")
        text = sent_text(reply)
        self.assertTrue(text.startswith(prompts.PROMPT_MARKER))
        self.assertIn("Einfach auf diese Nachricht antworten", text)
        for line in ("/ric Wert", "/ric Anfang*", "/ric *Teil*"):
            self.assertIn(line, text)
        markup = reply.call_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, ForceReply)
        self.assertEqual(markup.input_field_placeholder, "Wert oder Muster, z.B. 301*")

    async def test_placeholder_matches_the_command(self):
        for for_key, kind, expected in [("message", "for", "Text oder Muster, z.B. THL*"),
                                        ("ric", "add", "Name oder Muster, z.B. *wagen*")]:
            reply = await self.ask(for_key, kind)
            self.assertEqual(reply.call_args.kwargs["reply_markup"].input_field_placeholder, expected)
            self.assertIn("Einfach auf diese Nachricht antworten", sent_text(reply))

    async def test_name_search_question_keeps_help(self):
        text = sent_text(await self.ask("ric", "add"))
        self.assertIn("/description Name", text)
        self.assertIn("/description *Teil*", text)

    async def test_answer_runs_the_command(self):
        prompt = await self.ask("ric")
        reply = await self.answer(prompt, "1234567")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["target"], sub["alias"]), ("ric", "1234567", "Rettungswagen Musterstadt A-Wehr"))
        self.assertIn("Abonniert", sent_text(reply))

    async def test_answer_with_pattern_and_several_words(self):
        prompt = await self.ask("message")
        await self.answer(prompt, "RD 1*")
        self.assertEqual(self.subs()[0]["alias"], "Alarmstichwort: RD 1*")

    async def test_answer_to_name_search(self):
        prompt = await self.ask("ric", "add")
        await self.answer(prompt, "A-Wehr")
        self.assertEqual(self.subs()[0]["target"], "1234567")

    async def test_answer_to_name_search_with_several_hits_shows_buttons(self):
        prompt = await self.ask("ric", "add")
        reply = await self.answer(prompt, "Musterstadt")
        self.assertIn(("➕ Rettungswagen Musterstadt A-Wehr", "add:ric:0"), reply_buttons(reply))

    async def test_adduser_question_and_answer(self):
        update, reply = make_update(1)
        self.env.context.args = []
        await admin.adduser_handler(update, self.env.context)
        self.assertIn("Telegram-ID", sent_text(reply))
        self.assertIn("/adduser 123456789 Max Mustermann", sent_text(reply))
        self.assertEqual(reply.call_args.kwargs["reply_markup"].input_field_placeholder, "ID und optional Name")

        replied = bot_message(sent_text(reply), reply.return_value.message_id)
        update, _ = make_update(1, text="555 Erika Test", reply_to=replied)
        await prompts.prompt_reply_handler(update, self.env.context)
        self.assertEqual(db.get_user_status(self.env.db_path, 555), "allowed")

    async def test_private_chat_is_not_quoted_and_not_selective(self):
        reply = await self.ask("ric")
        self.assertNotIn("do_quote", reply.call_args.kwargs)
        self.assertFalse(reply.call_args.kwargs["reply_markup"].selective)

    async def test_group_question_is_quoted_and_selective(self):
        reply = await self.ask("ric", chat=-100, chat_type="group")
        self.assertIs(reply.call_args.kwargs["do_quote"], True)
        self.assertTrue(reply.call_args.kwargs["reply_markup"].selective)

    async def test_group_answer_subscribes_the_group(self):
        prompt = await self.ask("message", chat=-100, chat_type="group")
        await self.answer(prompt, "THL*", chat=-100, chat_type="group")
        self.assertEqual(self.subs(1), [])
        self.assertEqual(len(self.subs(-100)), 1)

    async def test_command_with_input_does_not_ask(self):
        reply = await self.command("ric", "1234567")
        self.assertNotIsInstance(reply.call_args.kwargs.get("reply_markup"), ForceReply)
        self.assertEqual(self.context_prompts(), {})

    def context_prompts(self):
        return self.env.context.user_data.get("prompts", {})

    async def test_two_open_questions_work_independently(self):
        first = await self.ask("ric")
        second = await self.ask("message")
        await self.answer(second, "THL*")
        await self.answer(first, "9999999")
        self.assertEqual(sorted(s["field"] for s in self.subs()), ["message", "ric"])

    async def test_question_can_be_answered_only_once(self):
        prompt = await self.ask("ric")
        await self.answer(prompt, "9999999")
        again = await self.answer(prompt, "8888888")
        self.assertIn("nicht mehr gültig", sent_text(again))
        self.assertEqual(len(self.subs()), 1)

    async def test_only_the_last_ten_questions_are_kept(self):
        first = await self.ask("ric")
        for _ in range(11):
            await self.ask("ric")
        self.assertEqual(len(self.context_prompts()), prompts.MAX_OPEN_PROMPTS)
        reply = await self.answer(first, "9999999")
        self.assertIn("nicht mehr gültig", sent_text(reply))
        self.assertEqual(self.subs(), [])

    async def test_expired_after_restart(self):
        prompt = await self.ask("ric")
        self.env.context.user_data.clear()
        reply = await self.answer(prompt, "9999999")
        self.assertIn("nicht mehr gültig", sent_text(reply))
        self.assertEqual(self.subs(), [])

    async def test_other_person_cannot_answer_a_foreign_question(self):
        db.set_user_status(self.env.db_path, 5, "allowed")
        prompt = await self.ask("ric", chat=-100, chat_type="group")
        reply = await self.answer(prompt, "9999999", user=5, chat=-100, chat_type="group")
        self.assertIn("nicht mehr gültig", sent_text(reply))
        self.assertEqual(self.subs(-100), [])

    async def test_removed_command_is_reported(self):
        prompt = await self.ask("ric")
        del self.env.context.bot_data["prompt_handlers"][("for", "ric")]
        reply = await self.answer(prompt, "9999999")
        self.assertIn("nicht mehr konfiguriert", sent_text(reply))

    async def test_reply_to_other_bot_messages_is_ignored(self):
        replied = bot_message("🚨 BOS-ALARM 🚨\nRD 1", 7)
        update, reply = make_update(77, text="danke", reply_to=replied)
        await prompts.prompt_reply_handler(update, self.env.context)
        reply.assert_not_called()
        self.assertIsNone(db.get_user_status(self.env.db_path, 77))

    async def test_reply_to_people_is_ignored_and_triggers_no_access_request(self):
        replied = NS(from_user=NS(id=4711), text=f"{prompts.PROMPT_MARKER} sieht nur so aus", message_id=7)
        update, reply = make_update(77, text="ok", reply_to=replied)
        await prompts.prompt_reply_handler(update, self.env.context)
        reply.assert_not_called()
        self.assertIsNone(db.get_user_status(self.env.db_path, 77))
        self.assertEqual(self.env.bot.sent, [])

    async def test_message_without_reply_is_ignored(self):
        update, reply = make_update(77, text="hallo")
        await prompts.prompt_reply_handler(update, self.env.context)
        reply.assert_not_called()

    async def test_stranger_answering_a_question_goes_through_access_request(self):
        prompt = await self.ask("ric", chat=-100, chat_type="group")
        await self.answer(prompt, "9999999", user=77, chat=-100, chat_type="group")
        self.assertEqual(db.get_user_status(self.env.db_path, 77), "pending")
        self.assertEqual(self.subs(-100), [])

    async def test_start_explains_the_questions(self):
        update, reply = make_update()
        await overview.start_handler(update, self.env.context)
        self.assertIn("fragt dich danach", sent_text(reply))


if __name__ == "__main__":
    unittest.main()
