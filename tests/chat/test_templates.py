"""/template: Das Alarm-Template eines Chats per Befehl ändern, mit Vorschau und Bestätigung."""
import html
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from telegram import ForceReply
from telegram.error import BadRequest

from bos_telegram_bot.chat import prompts, templates
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE, MAX_TEMPLATE_LENGTH
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase
from tests.helpers import bot_message, make_callback, make_update, sent_text

LOGGER = "bos_telegram_bot.chat.templates"
GROUP = -1001234567890
NEW = "📟 <b>{DESCRIPTION_LIST|RIC_LIST}</b>\n{MESSAGE}\n<i>{MATCHED}</i>"


class TemplateTestCase(CommandTestCase):
    def setUp(self):
        super().setUp()
        self.user_data = {}                                   # je Test neu: user_data jedes Users, ohne den ersten
        db.set_user_status(self.env.db_path, 2, "allowed", "Zweiter User")

    def context_of(self, user):
        """Wie bei Telegram: bot_data ist gemeinsam, user_data gehört jeder Person einzeln."""
        if user == 1:
            return self.env.context
        base = self.env.context
        return NS(bot=base.bot, bot_data=base.bot_data, user_data=self.user_data.setdefault(user, {}), args=[])

    async def run_template(self, text="/template", user=1, chat=None, chat_type="private"):
        """Der Befehl. Liefert den reply_text-Mock."""
        update, reply = make_update(user, chat_id=chat, chat_type=chat_type, text=text,
                                    title=None if chat_type == "private" else "Gruppe")
        await templates.template_command(update, self.context_of(user))
        return reply

    async def answer(self, prompt_reply, text, user=1, chat=None, chat_type="private"):
        """Die Antwort auf die Rückfrage, die 'prompt_reply' (Mock von reply_text) gesendet hat."""
        replied = bot_message(sent_text(prompt_reply), prompt_reply.return_value.message_id)
        update, reply = make_update(user, chat_id=chat, chat_type=chat_type, text=text, reply_to=replied,
                                    title=None if chat_type == "private" else "Gruppe")
        await prompts.prompt_reply_handler(update, self.context_of(user))
        return reply

    async def press(self, data, user=1, chat=None, chat_type="private", message=None):
        update, query = make_callback(data, user, chat_id=chat, chat_type=chat_type, message=message)
        await templates.template_buttons(update, self.context_of(user))
        return query

    @staticmethod
    def buttons(reply):
        markup = reply.call_args.kwargs["reply_markup"]
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    def token(self, reply):
        return self.buttons(reply)[0][1].split(":")[2]

    def stored(self, chat=1):
        return db.get_chat_template(self.env.db_path, chat)

    async def propose(self, text, **where):
        """Frage stellen, mit text antworten. Liefert den reply_text-Mock der Vorschau."""
        question = await self.run_template(**where)
        return await self.answer(question, text, **where)


class ShowTemplateTests(TemplateTestCase):
    async def test_the_default_is_shown_in_a_copy_block_and_the_user_is_asked(self):
        reply = await self.run_template()
        text = sent_text(reply)
        self.assertTrue(text.startswith(prompts.PROMPT_MARKER))                      # sonst erkennt der Bot die Antwort nicht
        self.assertIn("Template für diesen Chat (Standard)", text)
        self.assertIn(f"<pre>{html.escape(DEFAULT_TEMPLATE)}</pre>", text)
        self.assertIsInstance(reply.call_args.kwargs["reply_markup"], ForceReply)
        self.assertEqual(reply.call_args.kwargs["parse_mode"], "HTML")

    async def test_the_tags_in_the_block_are_escaped_so_the_user_sees_them_as_text(self):
        text = sent_text(await self.run_template())
        self.assertIn("&lt;b&gt;BOS-ALARM&lt;/b&gt;", text)
        self.assertNotIn("<b>BOS-ALARM</b>", text)

    async def test_the_default_is_not_offered_to_be_reset(self):
        self.assertNotIn("/template standard", sent_text(await self.run_template()))

    async def test_an_own_template_is_shown_instead_and_can_be_reset(self):
        db.set_chat_template(self.env.db_path, 1, NEW, 1)
        text = sent_text(await self.run_template())
        self.assertIn("Template für diesen Chat (eigenes)", text)
        self.assertIn(html.escape(NEW), text)
        self.assertNotIn(html.escape(DEFAULT_TEMPLATE), text)
        self.assertIn("<code>/template standard</code>", text)

    async def test_a_group_has_its_own_wording_and_its_own_template(self):
        db.set_chat_template(self.env.db_path, GROUP, NEW, 1)
        group = sent_text(await self.run_template(chat=GROUP, chat_type="supergroup"))
        private = sent_text(await self.run_template())
        self.assertIn("Template für diese Gruppe (eigenes)", group)
        self.assertIn("Template für diesen Chat (Standard)", private)

    async def test_the_help_names_the_placeholders_the_tags_and_the_next_step(self):
        text = sent_text(await self.run_template())
        for expected in ("{MESSAGE}", "{DESCRIPTION_LIST|RIC_LIST}", "{MATCHED}", "Antwort auf diese Nachricht",
                         "&lt;b&gt;", "Zeilenumbrüche bleiben", "Vorschau"):
            self.assertIn(expected, text)

    async def test_the_question_is_remembered_so_the_answer_reaches_the_template(self):
        reply = await self.run_template()
        key = (1, reply.return_value.message_id)
        self.assertEqual(self.env.context.user_data["prompts"][key], {"handler": templates.HANDLER_KEY})

    async def test_the_longest_allowed_template_still_fits_into_one_telegram_message(self):
        db.set_chat_template(self.env.db_path, 1, "{MESSAGE}<" * (MAX_TEMPLATE_LENGTH // 10), 1)
        self.assertLess(len(sent_text(await self.run_template())), 4096)

    async def test_a_rejected_too_long_draft_is_shown_shortened(self):
        draft = "{MESSAGE} " * 400                                                    # 4000 Zeichen
        reply = await self.propose(draft)
        text = sent_text(reply)
        self.assertLess(len(text), 4096)
        self.assertIn("ist zu lang", text)
        self.assertIn("…</pre>", text)

    async def test_an_unknown_user_gets_an_access_request_and_no_template(self):
        update, reply = make_update(77, text="/template")
        await templates.template_command(update, self.env.context)
        self.assertNotIn("<pre>", sent_text(reply))
        self.assertIn("Keine Berechtigung", sent_text(reply))

    async def test_the_command_with_the_bot_name_in_a_group_is_understood(self):
        reply = await self.run_template("/template@bos_bot <b>{MESSAGE}</b>", chat=GROUP, chat_type="supergroup")
        self.assertIn("Vorschau", sent_text(reply))


class ProposeTests(TemplateTestCase):
    async def test_a_valid_answer_shows_a_preview_with_the_sample_alarm_and_the_buttons(self):
        reply = await self.propose(NEW)
        text = sent_text(reply)
        self.assertIn("Vorschau mit einem Beispiel-Alarm", text)
        self.assertIn("📟 <b>Wache Nord (Beispiel)</b>", text)
        self.assertIn("THL 1 - Beispieltext", text)
        self.assertIn("<i>RIC: 1000011</i>", text)
        self.assertIn("Übernehmen?", text)
        self.assertEqual([label for label, _ in self.buttons(reply)], ["✅ Übernehmen", "✏️ Ändern", "✖️ Verwerfen"])
        self.assertEqual(reply.call_args.kwargs["parse_mode"], "HTML")

    async def test_nothing_is_saved_before_the_confirmation(self):
        await self.propose(NEW)
        self.assertIsNone(self.stored())

    async def test_the_preview_uses_the_sample_alarm_and_never_a_real_one(self):
        self.env.context.bot_data["last_payload"] = {"ric": "5555555", "message": "GEHEIMER ECHTER ALARMTEXT",
                                                     "description": "Fremde Wache"}
        text = sent_text(await self.propose("{MESSAGE} {RIC} {DESCRIPTION}"))
        self.assertNotIn("GEHEIMER", text)
        self.assertNotIn("5555555", text)
        self.assertNotIn("Fremde Wache", text)
        self.assertIn("1000011", text)

    async def test_the_line_breaks_of_the_answer_are_kept_in_the_preview_and_in_the_saved_template(self):
        reply = await self.propose("{DESCRIPTION}\n\n{MESSAGE}\n{RIC}")
        text = sent_text(reply)
        self.assertIn("Wache Nord (Beispiel)\n\nTHL 1", text)
        await self.press(f"tpl:ok:{self.token(reply)}")
        self.assertEqual(self.stored(), "{DESCRIPTION}\n\n{MESSAGE}\n{RIC}")

    async def test_the_raw_text_is_used_and_not_the_words_split_by_spaces(self):
        reply = await self.propose("A   {MESSAGE}   B")                              # mehrere Leerzeichen bleiben
        await self.press(f"tpl:ok:{self.token(reply)}")
        self.assertEqual(self.stored(), "A   {MESSAGE}   B")

    async def test_windows_line_breaks_are_normalized_and_blank_lines_at_the_edges_are_dropped(self):
        reply = await self.propose("\r\n{MESSAGE}\r\n{RIC}\r\n\r\n")
        await self.press(f"tpl:ok:{self.token(reply)}")
        self.assertEqual(self.stored(), "{MESSAGE}\n{RIC}")

    async def test_placeholders_that_never_have_a_value_are_pointed_out(self):
        text = sent_text(await self.propose("{MESSAGE} {GIBTSNICHT} {TIPPFEHLR|AUCHNICHT}"))
        self.assertIn("Unbekannte Platzhalter, sie bleiben leer", text)
        self.assertIn("<code>{GIBTSNICHT}</code>", text)
        self.assertIn("<code>{TIPPFEHLR|AUCHNICHT}</code>", text)

    async def test_known_placeholders_cause_no_warning(self):
        self.assertNotIn("Unbekannte", sent_text(await self.propose("{MESSAGE} {RIC_LIST} {MATCHED} {SUBRICTEXT}")))

    async def test_an_answer_that_fails_the_check_asks_again_with_the_users_own_text_and_the_reasons(self):
        reply = await self.propose("<b>{MESSAGE}")
        text = sent_text(reply)
        self.assertTrue(text.startswith(prompts.PROMPT_MARKER))
        self.assertIn("Dein Entwurf für diesen Chat", text)
        self.assertIn("So geht es noch nicht", text)
        self.assertIn("• &lt;b&gt; wird nicht geschlossen", text)
        self.assertIn("<pre>&lt;b&gt;{MESSAGE}</pre>", text)                          # sein Text, nicht der alte
        self.assertIsInstance(reply.call_args.kwargs["reply_markup"], ForceReply)
        self.assertIsNone(self.stored())

    async def test_the_reasons_read_as_sentences(self):
        text = sent_text(await self.propose("kein Platzhalter hier"))
        self.assertIn("• Das Template enthält keinen Platzhalter", text)

    async def test_a_stray_brace_is_explained(self):
        self.assertIn("Die Klammer '{' steht außerhalb eines Platzhalters", sent_text(await self.propose("{MESSAGE} {")))

    async def test_an_unknown_tag_is_refused(self):
        self.assertIn("So geht es noch nicht", sent_text(await self.propose("<blink>{MESSAGE}</blink>")))

    async def test_a_template_that_is_too_long_is_refused(self):
        text = sent_text(await self.propose("{MESSAGE}" + "x" * MAX_TEMPLATE_LENGTH))
        self.assertIn(f"erlaubt sind {MAX_TEMPLATE_LENGTH}", text)

    async def test_the_maximum_length_itself_is_fine(self):
        text = "{MESSAGE}" + "x" * (MAX_TEMPLATE_LENGTH - len("{MESSAGE}"))
        self.assertIn("Vorschau", sent_text(await self.propose(text)))

    async def test_blank_text_is_refused(self):
        self.assertIn("Das Template ist leer", sent_text(await self.propose("  \n  ")))

    async def test_the_same_template_as_now_is_reported_as_no_change(self):
        reply = await self.propose(DEFAULT_TEMPLATE)
        self.assertIn("schon das aktuelle Template", sent_text(reply))
        self.assertNotIn("reply_markup", reply.call_args.kwargs)

    async def test_an_own_template_can_be_replaced_by_the_default_text_as_an_own_one(self):
        db.set_chat_template(self.env.db_path, 1, NEW, 1)
        self.assertIn("Vorschau", sent_text(await self.propose(DEFAULT_TEMPLATE)))

    async def test_when_telegram_rejects_the_preview_the_user_goes_back_to_editing(self):
        update, reply = make_update(1, text="/template " + NEW)
        reply.side_effect = [BadRequest("Can't parse entities: Unsupported start tag at byte offset 3"),
                             NS(message_id=5)]
        await templates.template_command(update, self.env.context)
        self.assertEqual(reply.await_count, 2)
        text = sent_text(reply)
        self.assertIn("wird von Telegram abgelehnt", text)
        self.assertIn("Can't parse entities", text)                                    # Anführungszeichen bleiben lesbar
        self.assertIn(html.escape(NEW), text)
        self.assertEqual(self.env.context.bot_data.get("template_drafts", {}), {})        # der Entwurf ist wieder weg

    async def test_the_command_with_text_goes_straight_to_the_preview(self):
        reply = await self.run_template("/template " + NEW)
        self.assertIn("Vorschau", sent_text(reply))
        self.assertNotIn("<pre>", sent_text(reply))

    async def test_a_multiline_text_after_the_command_is_kept(self):
        reply = await self.run_template("/template\n{DESCRIPTION}\n{MESSAGE}")
        await self.press(f"tpl:ok:{self.token(reply)}")
        self.assertEqual(self.stored(), "{DESCRIPTION}\n{MESSAGE}")

    async def test_in_a_group_the_preview_quotes_the_message_of_the_user(self):
        reply = await self.propose(NEW, chat=GROUP, chat_type="supergroup")
        self.assertTrue(reply.call_args.kwargs["do_quote"])

    async def test_in_a_private_chat_the_preview_does_not_quote(self):
        self.assertNotIn("do_quote", (await self.propose(NEW)).call_args.kwargs)


class ConfirmTests(TemplateTestCase):
    async def test_the_confirmation_saves_the_template_for_this_chat_and_says_so(self):
        reply = await self.propose(NEW)
        with self.assertLogs(LOGGER, level="INFO") as logged:
            query = await self.press(f"tpl:ok:{self.token(reply)}")
        self.assertEqual(self.stored(), NEW)
        self.assertIn("Das Template für diesen Chat wurde geändert", query.edit_message_text.call_args[0][0])
        self.assertEqual(logged.output, [f"INFO:{LOGGER}:Template für Chat 1 geändert (User 1)"])

    async def test_in_a_group_the_template_belongs_to_the_group_and_not_to_the_user(self):
        reply = await self.propose(NEW, chat=GROUP, chat_type="supergroup")
        query = await self.press(f"tpl:ok:{self.token(reply)}", chat=GROUP, chat_type="supergroup")
        self.assertEqual(self.stored(GROUP), NEW)
        self.assertIsNone(self.stored(1))
        self.assertIn("für diese Gruppe", query.edit_message_text.call_args[0][0])

    async def test_private_and_group_templates_are_independent(self):
        first = await self.propose(NEW)
        await self.press(f"tpl:ok:{self.token(first)}")
        second = await self.propose("{MESSAGE}", chat=GROUP, chat_type="supergroup")
        await self.press(f"tpl:ok:{self.token(second)}", chat=GROUP, chat_type="supergroup")
        self.assertEqual((self.stored(1), self.stored(GROUP)), (NEW, "{MESSAGE}"))

    async def test_any_allowed_user_may_change_the_template_of_a_group(self):
        reply = await self.propose(NEW, user=2, chat=GROUP, chat_type="supergroup")
        await self.press(f"tpl:ok:{self.token(reply)}", user=2, chat=GROUP, chat_type="supergroup")
        self.assertEqual(db.get_chat_template(self.env.db_path, GROUP), NEW)

    async def test_the_change_is_recorded_with_who_made_it(self):
        reply = await self.propose(NEW, user=2, chat=GROUP, chat_type="supergroup")
        await self.press(f"tpl:ok:{self.token(reply)}", user=2, chat=GROUP, chat_type="supergroup")
        import sqlite3
        conn = sqlite3.connect(self.env.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT updated_by FROM chat_templates WHERE chat_id = ?", (GROUP,)).fetchone(), (2,))

    async def test_somebody_else_cannot_decide_about_a_proposal(self):
        reply = await self.propose(NEW, chat=GROUP, chat_type="supergroup")
        token = self.token(reply)
        query = await self.press(f"tpl:ok:{token}", user=2, chat=GROUP, chat_type="supergroup")
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        self.assertIn("Nur wer die Änderung vorgeschlagen hat", query.answer.call_args[0][0])
        query.edit_message_text.assert_not_awaited()
        self.assertIsNone(self.stored(GROUP))
        # Der Vorschlag gilt weiter: Der Urheber kann noch zustimmen.
        await self.press(f"tpl:ok:{token}", chat=GROUP, chat_type="supergroup")
        self.assertEqual(self.stored(GROUP), NEW)

    async def test_two_users_in_one_group_have_their_own_proposals(self):
        first = await self.propose("{MESSAGE}", user=1, chat=GROUP, chat_type="supergroup")
        second = await self.propose("{RIC}", user=2, chat=GROUP, chat_type="supergroup")
        self.assertNotEqual(self.token(first), self.token(second))
        await self.press(f"tpl:ok:{self.token(second)}", user=2, chat=GROUP, chat_type="supergroup")
        self.assertEqual(self.stored(GROUP), "{RIC}")

    async def test_discarding_keeps_the_old_template(self):
        db.set_chat_template(self.env.db_path, 1, "{RIC}", 1)
        reply = await self.propose(NEW)
        query = await self.press(f"tpl:no:{self.token(reply)}")
        self.assertEqual(self.stored(), "{RIC}")
        self.assertIn("Verworfen, das Template bleibt wie es war", query.edit_message_text.call_args[0][0])

    async def test_a_proposal_can_only_be_decided_once(self):
        reply = await self.propose(NEW)
        token = self.token(reply)
        await self.press(f"tpl:ok:{token}")
        query = await self.press(f"tpl:ok:{token}")
        self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0])

    async def test_an_unknown_token_is_reported_as_expired(self):
        query = await self.press("tpl:ok:00000000")
        self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0])
        self.assertIsNone(self.stored())

    async def test_a_proposal_expires_after_half_an_hour(self):
        reply = await self.propose(NEW)
        token = self.token(reply)
        self.env.context.bot_data["template_drafts"][token]["created"] -= templates.DRAFT_SECONDS + 1
        query = await self.press(f"tpl:ok:{token}")
        self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0])
        self.assertIsNone(self.stored())

    async def test_a_proposal_cannot_be_used_in_another_chat(self):
        reply = await self.propose(NEW)
        query = await self.press(f"tpl:ok:{self.token(reply)}", chat=GROUP, chat_type="supergroup")
        self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0])
        self.assertIsNone(self.stored(GROUP))
        self.assertIsNone(self.stored(1))

    async def test_broken_button_data_does_not_crash(self):
        for data in ("tpl:", "tpl:ok", "tpl", "tpl:ok:"):
            query = await self.press(data)
            self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0], data)

    async def test_an_unknown_action_is_reported_as_expired_and_changes_nothing(self):
        reply = await self.propose(NEW)
        query = await self.press(f"tpl:boese:{self.token(reply)}")
        self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0])
        self.assertIsNone(self.stored())

    async def test_the_button_data_fits_into_telegrams_limit(self):
        for _, data in self.buttons(await self.propose(NEW)):
            self.assertLessEqual(len(data.encode()), 64)

    async def test_the_number_of_open_proposals_is_limited(self):
        for i in range(templates.MAX_DRAFTS + 5):
            await self.propose("{MESSAGE} " + "x" * (i % 5 + 1))
        self.assertLessEqual(len(self.env.context.bot_data["template_drafts"]), templates.MAX_DRAFTS)

    async def test_a_user_who_is_not_allowed_cannot_press_the_buttons(self):
        reply = await self.propose(NEW)
        update, query = make_callback(f"tpl:ok:{self.token(reply)}", 99)
        await templates.template_buttons(update, self.env.context)
        self.assertIsNone(self.stored())
        query.edit_message_text.assert_not_awaited()


class ChangeAgainTests(TemplateTestCase):
    async def test_change_asks_again_with_the_draft_as_the_copy_block(self):
        reply = await self.propose(NEW)
        anchor = NS(reply_text=AsyncMock(return_value=NS(message_id=77)))
        preview = NS(reply_to_message=anchor, reply_text=AsyncMock())
        query = await self.press(f"tpl:again:{self.token(reply)}", message=preview)
        text = anchor.reply_text.call_args[0][0]
        self.assertTrue(text.startswith(prompts.PROMPT_MARKER))
        self.assertIn("Dein Entwurf für diesen Chat", text)
        self.assertIn(f"<pre>{html.escape(NEW)}</pre>", text)
        self.assertIsInstance(anchor.reply_text.call_args.kwargs["reply_markup"], ForceReply)
        self.assertIn((1, 77), self.env.context.user_data["prompts"])
        self.assertIn("Entwurf steht in der nächsten Nachricht", query.edit_message_text.call_args[0][0])

    async def test_the_new_question_hangs_on_the_message_of_the_user_not_on_the_preview(self):
        reply = await self.propose(NEW, chat=GROUP, chat_type="supergroup")
        anchor = NS(reply_text=AsyncMock(return_value=NS(message_id=78)))
        preview = NS(reply_to_message=anchor, reply_text=AsyncMock(return_value=NS(message_id=79)))
        await self.press(f"tpl:again:{self.token(reply)}", chat=GROUP, chat_type="supergroup", message=preview)
        anchor.reply_text.assert_awaited_once()
        preview.reply_text.assert_not_awaited()

    async def test_without_the_original_message_it_hangs_on_the_preview(self):
        reply = await self.propose(NEW)
        preview = NS(reply_to_message=None, reply_text=AsyncMock(return_value=NS(message_id=80)))
        await self.press(f"tpl:again:{self.token(reply)}", message=preview)
        preview.reply_text.assert_awaited_once()

    async def test_the_answer_to_the_new_question_leads_to_a_new_preview(self):
        reply = await self.propose(NEW)
        anchor = NS(reply_text=AsyncMock(return_value=NS(message_id=81)))
        await self.press(f"tpl:again:{self.token(reply)}", message=NS(reply_to_message=anchor, reply_text=AsyncMock()))
        second = await self.answer(anchor.reply_text, "{MESSAGE} geändert")
        self.assertIn("THL 1 - Beispieltext, bitte nicht ernst nehmen geändert", sent_text(second))
        self.assertIsNone(self.stored())                                              # noch nichts gespeichert

    async def test_the_old_proposal_is_gone_after_changing(self):
        reply = await self.propose(NEW)
        token = self.token(reply)
        anchor = NS(reply_text=AsyncMock(return_value=NS(message_id=82)))
        await self.press(f"tpl:again:{token}", message=NS(reply_to_message=anchor, reply_text=AsyncMock()))
        query = await self.press(f"tpl:ok:{token}")
        self.assertIn("abgelaufen", query.edit_message_text.call_args[0][0])

    async def test_somebody_else_cannot_change_a_proposal_either(self):
        reply = await self.propose(NEW, chat=GROUP, chat_type="supergroup")
        anchor = NS(reply_text=AsyncMock())
        query = await self.press(f"tpl:again:{self.token(reply)}", user=2, chat=GROUP, chat_type="supergroup",
                                 message=NS(reply_to_message=anchor, reply_text=AsyncMock()))
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        anchor.reply_text.assert_not_awaited()


class ResetTests(TemplateTestCase):
    async def test_standard_asks_for_confirmation_and_shows_what_an_alarm_would_look_like(self):
        db.set_chat_template(self.env.db_path, 1, NEW, 1)
        reply = await self.run_template("/template standard")
        text = sent_text(reply)
        self.assertIn("Standard-Template wiederherstellen?", text)
        self.assertIn("🚨 <b>BOS-ALARM</b> 🚨", text)
        self.assertIn("THL 1 - Beispieltext", text)
        self.assertEqual([label for label, _ in self.buttons(reply)], ["✅ Wiederherstellen", "✖️ Verwerfen"])
        self.assertEqual(self.stored(), NEW)                                           # noch nicht zurückgesetzt

    async def test_the_confirmation_removes_the_own_template(self):
        db.set_chat_template(self.env.db_path, 1, NEW, 1)
        reply = await self.run_template("/template standard")
        with self.assertLogs(LOGGER, level="INFO") as logged:
            query = await self.press(f"tpl:ok:{self.token(reply)}")
        self.assertIsNone(self.stored())
        self.assertIn("Das Standard-Template gilt wieder für diesen Chat", query.edit_message_text.call_args[0][0])
        self.assertEqual(logged.output, [f"INFO:{LOGGER}:Template für Chat 1 auf den Standard zurückgesetzt (User 1)"])

    async def test_discarding_keeps_the_own_template(self):
        db.set_chat_template(self.env.db_path, 1, NEW, 1)
        reply = await self.run_template("/template standard")
        await self.press(f"tpl:no:{self.token(reply)}")
        self.assertEqual(self.stored(), NEW)

    async def test_without_an_own_template_there_is_nothing_to_reset(self):
        reply = await self.run_template("/template standard")
        self.assertIn("nutzt bereits das Standard-Template", sent_text(reply))
        self.assertNotIn("reply_markup", reply.call_args.kwargs)

    async def test_the_words_standard_and_reset_work_in_any_case_and_in_the_reply_too(self):
        db.set_chat_template(self.env.db_path, 1, NEW, 1)
        for word in ("STANDARD", "reset", "Standard"):
            self.assertIn("wiederherstellen?", sent_text(await self.run_template(f"/template {word}")), word)
        question = await self.run_template()
        self.assertIn("wiederherstellen?", sent_text(await self.answer(question, "  standard  ")))

    async def test_reset_in_a_group_only_touches_the_group(self):
        db.set_chat_template(self.env.db_path, GROUP, NEW, 1)
        db.set_chat_template(self.env.db_path, 1, "{RIC}", 1)
        reply = await self.run_template("/template standard", chat=GROUP, chat_type="supergroup")
        query = await self.press(f"tpl:ok:{self.token(reply)}", chat=GROUP, chat_type="supergroup")
        self.assertIsNone(self.stored(GROUP))
        self.assertEqual(self.stored(1), "{RIC}")
        self.assertIn("für diese Gruppe", query.edit_message_text.call_args[0][0])

    async def test_a_template_that_merely_contains_the_word_is_not_a_reset(self):
        self.assertIn("Vorschau", sent_text(await self.run_template("/template {MESSAGE} standard")))

    async def test_a_default_that_changed_in_the_config_applies_to_chats_without_an_own_template(self):
        self.env.context.bot_data["notification_template"] = "NEUER STANDARD {MESSAGE}"
        self.assertIn("NEUER STANDARD {MESSAGE}", sent_text(await self.run_template()))


class EndToEndTests(TemplateTestCase):
    """Vom Befehl bis zum Alarm: Das bestätigte Template gilt für den nächsten Alarm dieses Chats, für keinen anderen."""
    ALARM = {"ric": "1000011", "description": "Wache Nord", "message": "TEST Beispieltext",
             "timestamp": "1700000001.0", "timestamp_list": "1700000001.0"}

    def setUp(self):
        super().setUp()
        for chat in (1, 2, GROUP):
            db.add_sub(self.env.db_path, chat, "ric", "1000011", "RIC: 1000011", False)

    async def alarm(self, **changes):
        from tests.helpers import SendHarness
        payload = dict(self.ALARM, **changes)
        self.env.bot.sent.clear()
        await SendHarness(self.env.bot, attempts=1).handle(
            self.env.db_path, self.env.context.bot_data["active_fields"], DEFAULT_TEMPLATE, payload)
        return {m["chat_id"]: m["text"] for m in self.env.bot.sent}

    async def test_a_changed_template_reaches_the_next_alarm_of_that_chat_only(self):
        before = await self.alarm()
        reply = await self.propose("🔔 {DESCRIPTION}: {MESSAGE}")
        await self.press(f"tpl:ok:{self.token(reply)}")
        after = await self.alarm(timestamp="1700000002.0", timestamp_list="1700000002.0")
        self.assertIn("BOS-ALARM", before[1])
        self.assertEqual(after[1], "🔔 Wache Nord: TEST Beispieltext")
        self.assertIn("BOS-ALARM", after[2])
        self.assertIn("BOS-ALARM", after[GROUP])

    async def test_a_group_changes_its_own_template_for_all_its_members(self):
        reply = await self.propose("GRUPPE {RIC}", user=2, chat=GROUP, chat_type="supergroup")
        await self.press(f"tpl:ok:{self.token(reply)}", user=2, chat=GROUP, chat_type="supergroup")
        texts = await self.alarm()
        self.assertEqual(texts[GROUP], "GRUPPE 1000011")
        self.assertIn("BOS-ALARM", texts[1])

    async def test_a_proposal_that_was_not_confirmed_changes_nothing(self):
        await self.propose("NIE BESTÄTIGT {RIC}")
        self.assertIn("BOS-ALARM", (await self.alarm())[1])

    async def test_after_the_reset_the_chat_gets_the_default_again(self):
        first = await self.propose("EIGEN {RIC}")
        await self.press(f"tpl:ok:{self.token(first)}")
        reset = await self.run_template("/template standard")
        await self.press(f"tpl:ok:{self.token(reset)}")
        self.assertIn("BOS-ALARM", (await self.alarm())[1])

    async def test_a_template_the_preview_accepted_but_the_real_alarm_breaks_still_delivers_the_alarm(self):
        """Der Sicherheitsgurt: Auch wenn Telegram das Template beim echten Alarm doch ablehnt, geht er raus."""
        from tests.mqtt.test_dispatch import RejectingBot
        reply = await self.propose("KAPUTT {RIC}")
        await self.press(f"tpl:ok:{self.token(reply)}")
        from tests.helpers import SendHarness
        bot = RejectingBot()
        with self.assertLogs("bos_telegram_bot", level="WARNING"):
            await SendHarness(bot, attempts=1).handle(self.env.db_path, {"ric"}, DEFAULT_TEMPLATE, self.ALARM)
        self.assertIn(1, [chat for chat, _ in bot.sent])
        self.assertIn("BOS-ALARM", dict(bot.sent)[1])


if __name__ == "__main__":
    import unittest
    unittest.main()
