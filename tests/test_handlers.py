import unittest
from types import SimpleNamespace as NS

from telegram import ForceReply

import database as db
import handlers
from matching import candidate_values, match_sub
from tests.helpers import BOT_ID, Env, bot_message, make_callback, make_update, sent_text

CSV_ROWS = [
    {"for": "1234567", "add": "Rettungswagen Musterstadt A-Wehr", "isRegex": False},
    {"for": r"^23456([0-9]{2})$", "add": r"Feuerwehr Musterstadt \1", "isRegex": True},
]


def buttons(mock):
    """(Text, callback_data) aller Inline-Buttons des letzten Aufrufs."""
    markup = mock.call_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def matches(sub, payload, key):
    values, raw = candidate_values(payload, key)
    return match_sub(sub, values, raw) is not None


class CommandTestCase(unittest.IsolatedAsyncioTestCase):
    csv_rows = CSV_ROWS

    def setUp(self):
        self.env = Env(self, csv_rows=self.csv_rows)

    async def command(self, for_key, *args, kind="for", user=1, chat=None):
        update, reply = make_update(user, chat_id=chat)
        self.env.context.args = list(args)
        make = handlers.make_for_handler if kind == "for" else handlers.make_add_handler
        await make(self.env.entry(for_key))(update, self.env.context)
        return reply

    async def click(self, data, user=1):
        update, query = make_callback(data, user)
        await handlers.button_handler(update, self.env.context)
        return query

    def subs(self, chat=1):
        return [dict(s) for s in self.env.subs(chat)]


class ForCommandTests(CommandTestCase):
    async def test_exact_unknown_value(self):
        reply = await self.command("ric", "9999999")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["target"], sub["is_regex"], sub["alias"]),
                         ("ric", "9999999", 0, "RIC: 9999999"))
        self.assertIn("Abonniert", sent_text(reply))

    async def test_exact_resolves_name_from_csv(self):
        reply = await self.command("ric", "1234567")
        (sub,) = self.subs()
        self.assertEqual((sub["target"], sub["alias"]), ("1234567", "Rettungswagen Musterstadt A-Wehr"))
        self.assertIn("Rettungswagen Musterstadt A-Wehr", sent_text(reply))

    async def test_exact_resolves_name_from_regex_row(self):
        await self.command("ric", "2345625")
        (sub,) = self.subs()
        self.assertEqual((sub["target"], sub["is_regex"], sub["alias"]), ("2345625", 0, "Feuerwehr Musterstadt 25"))

    async def test_exact_resolves_learned_name(self):
        db.upsert_learned(self.env.db_path, "ric", "5555555", "Gelernter Name")
        await self.command("ric", "5555555")
        self.assertEqual(self.subs()[0]["alias"], "Gelernter Name")

    async def test_exact_is_not_a_substring_search(self):
        await self.command("ric", "301")
        (sub,) = self.subs()
        self.assertFalse(matches(sub, {"ric": "0230100"}, "ric"))
        self.assertTrue(matches(sub, {"ric": "301"}, "ric"))

    async def test_prefix_wildcard_needs_star(self):
        await self.command("ric", "301*")
        (sub,) = self.subs()
        self.assertEqual(sub["is_regex"], 1)
        self.assertTrue(matches(sub, {"ric": "3010001"}, "ric"))
        self.assertFalse(matches(sub, {"ric": "0230100"}, "ric"))

    async def test_contains_with_stars_on_both_sides(self):
        await self.command("ric", "*301*")
        (sub,) = self.subs()
        self.assertTrue(matches(sub, {"ric": "0230100"}, "ric"))

    async def test_message_defaults_to_contains_and_ignores_case(self):
        reply = await self.command("message", "thl")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["is_regex"], sub["alias"]), ("message", 1, "Alarmstichwort: thl"))
        self.assertTrue(matches(sub, {"message": "Alarm THL Tür"}, "message"))
        self.assertIn("Filter angelegt", sent_text(reply))

    async def test_message_prefix(self):
        await self.command("message", "THL*")
        (sub,) = self.subs()
        self.assertTrue(matches(sub, {"message": "THL Tür"}, "message"))
        self.assertFalse(matches(sub, {"message": "RD THL"}, "message"))

    async def test_multi_word_input(self):
        await self.command("message", "RD", "1*")
        self.assertEqual(self.subs()[0]["alias"], "Alarmstichwort: RD 1*")

    async def test_subric_is_exact(self):
        await self.command("subricText", "a")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["is_regex"]), ("subricText", 0))

    async def test_invalid_pattern_is_rejected(self):
        reply = await self.command("message", "re:(")
        self.assertEqual(self.subs(), [])
        self.assertIn("Ungültiges Muster", sent_text(reply))

    async def test_usage_without_arguments(self):
        exact = sent_text(await self.command("ric"))
        contains = sent_text(await self.command("message"))
        self.assertIn("genau dieser Wert", exact)
        self.assertIn("enthält Text", contains)
        self.assertEqual(self.subs(), [])

    async def test_same_subscription_twice_is_stored_once(self):
        await self.command("ric", "9999999")
        await self.command("ric", "9999999")
        self.assertEqual(len(self.subs()), 1)

    async def test_group_subscription_belongs_to_the_group(self):
        await self.command("message", "THL*", user=1, chat=-100)
        self.assertEqual(self.subs(1), [])
        self.assertEqual(len(self.subs(-100)), 1)


class RangeCommandTests(CommandTestCase):
    async def test_message_b3_plus(self):
        reply = await self.command("message", "B", "3+")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["is_regex"], sub["alias"]), ("message", 1, "Alarmstichwort: B 3+"))
        self.assertIn("Filter angelegt", sent_text(reply))
        for text, expected in [("B 1 - Kleinbrand", False), ("B 2 - Zimmerbrand", False),
                               ("B 3 - Wohnungsbrand", True), ("B 4", True), ("B 10 - Lage", True),
                               ("RD 3 - Herz", False)]:
            self.assertEqual(matches(sub, {"message": text}, "message"), expected, text)

    async def test_old_plain_subscription_still_works_next_to_it(self):
        await self.command("message", "B", "3")
        await self.command("message", "B", "3+")
        self.assertEqual(len(self.subs()), 2)

    async def test_exact_field_anchors_the_range_to_the_whole_value(self):
        await self.command("ric", "1000000+")
        (sub,) = self.subs()
        self.assertEqual(sub["is_regex"], 1)
        for value, expected in [("1000000", True), ("1000001", True), ("9999999", True),
                                ("0999999", False), ("999999", False), ("x1000001", False)]:
            self.assertEqual(matches(sub, {"ric": value}, "ric"), expected, value)

    async def test_ric_with_leading_zero_compares_in_same_length(self):
        await self.command("ric", "0230100+")
        (sub,) = self.subs()
        for value, expected in [("0230100", True), ("0230101", True), ("0999999", True),
                                ("0230099", False), ("0100000", False), ("10230100", False)]:
            self.assertEqual(matches(sub, {"ric": value}, "ric"), expected, value)

    async def test_range_in_name_search_creates_a_pattern_directly(self):
        reply = await self.command("ric", "Fahrzeug 3+", kind="add")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["is_regex"]), ("description", 1))
        self.assertTrue(matches(sub, {"description": "Fahrzeug 7 Nord"}, "description"))
        self.assertFalse(matches(sub, {"description": "Fahrzeug 2 Nord"}, "description"))
        self.assertIn("Filter angelegt", sent_text(reply))

    async def test_multicast_list_is_checked_for_ranges_too(self):
        await self.command("message", "B", "3+")
        (sub,) = self.subs()
        payload = {"message": "B 1", "message_list": "B 1, B 4"}
        self.assertTrue(matches(sub, payload, "message"))

    async def test_hint_against_last_alarm(self):
        self.env.context.bot_data["last_payload"] = {"message": "B 5 - Großbrand"}
        self.assertIn("Passt auf den letzten Alarm", sent_text(await self.command("message", "B", "3+")))
        self.env.context.bot_data["last_payload"] = {"message": "B 2 - Zimmerbrand"}
        self.assertIn("Passt nicht auf den letzten Alarm", sent_text(await self.command("message", "B", "4+")))

    async def test_help_shows_the_syntax_for_text_fields_only(self):
        self.assertIn("/message B 3+", sent_text(await self.command("message")))
        self.assertNotIn("3+", sent_text(await self.command("ric")))


class HintTests(CommandTestCase):
    async def test_unknown_value_hint(self):
        text = sent_text(await self.command("ric", "9999999"))
        self.assertIn("kenne ich noch nicht", text)

    async def test_no_unknown_hint_when_nothing_is_known_yet(self):
        env_empty = Env(self)
        update, reply = make_update()
        env_empty.context.args = ["9999999"]
        await handlers.make_for_handler(env_empty.entry("ric"))(update, env_empty.context)
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


class NameSearchTests(CommandTestCase):
    async def test_exact_name_subscribes_the_ric(self):
        reply = await self.command("ric", "rettungswagen musterstadt a-wehr", kind="add")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["target"], sub["is_regex"], sub["alias"]),
                         ("ric", "1234567", 0, "Rettungswagen Musterstadt A-Wehr"))
        self.assertIn("Abonniert", sent_text(reply))

    async def test_single_hit_subscribes_automatically(self):
        await self.command("ric", "A-Wehr", kind="add")
        self.assertEqual(self.subs()[0]["target"], "1234567")

    async def test_regex_row_subscribes_the_pattern(self):
        await self.command("ric", "Feuerwehr", kind="add")
        (sub,) = self.subs()
        self.assertEqual((sub["target"], sub["is_regex"]), (r"^23456([0-9]{2})$", 1))
        self.assertTrue(matches(sub, {"ric": "2345625"}, "ric"))

    async def test_several_hits_show_buttons_and_pattern_option(self):
        reply = await self.command("ric", "Musterstadt", kind="add")
        self.assertEqual(self.subs(), [])
        labels = [text for text, _ in buttons(reply)]
        self.assertTrue(any("Rettungswagen" in t for t in labels))
        self.assertTrue(any("als Muster" in t for t in labels))
        self.assertIn(("➕ Rettungswagen Musterstadt A-Wehr", "add:ric:0"), buttons(reply))

    async def test_picking_a_button_subscribes(self):
        await self.command("ric", "Musterstadt", kind="add")
        query = await self.click("add:ric:0")
        self.assertEqual(self.subs()[0]["target"], "1234567")
        self.assertIn("Abonniert", sent_text(query.edit_message_text))

    async def test_pattern_button_after_several_hits(self):
        await self.command("ric", "Musterstadt", kind="add")
        await self.click("pat:ric")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["is_regex"]), ("description", 1))
        self.assertTrue(matches(sub, {"description": "Anderes Fahrzeug MUSTERSTADT Nord"}, "description"))

    async def test_no_hit_offers_pattern_anyway(self):
        reply = await self.command("ric", "Unbekannt", kind="add")
        self.assertIn("Keine Treffer", sent_text(reply))
        self.assertEqual(buttons(reply), [("🔎 Trotzdem als Muster anlegen", "pat:ric"),
                                          ("✖️ Abbrechen", "cancel:ric")])
        self.assertEqual(self.subs(), [])
        await self.click("pat:ric")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["alias"]), ("description", "Fahrzeug / Wache: Unbekannt"))

    async def test_wildcard_creates_pattern_directly_on_description(self):
        reply = await self.command("ric", "*wagen*", kind="add")
        (sub,) = self.subs()
        self.assertEqual((sub["field"], sub["is_regex"]), ("description", 1))
        self.assertIn("Filter angelegt", sent_text(reply))
        self.assertTrue(matches(sub, {"description": "Rettungswagen Nord"}, "description"))

    async def test_pattern_on_description_matches_multicast_list(self):
        await self.command("ric", "*Wache*", kind="add")
        (sub,) = self.subs()
        payload = {"description": "Fahrzeug", "description_list": "Wache Nord, Fahrzeug"}
        self.assertTrue(matches(sub, payload, "description"))

    async def test_learned_names_are_searchable(self):
        db.upsert_learned(self.env.db_path, "ric", "7777777", "Gelerntes Fahrzeug Nord")
        await self.command("ric", "Gelerntes", kind="add")
        self.assertEqual(self.subs()[0]["target"], "7777777")

    async def test_expired_state_after_restart(self):
        await self.command("ric", "Musterstadt", kind="add")
        self.env.context.user_data.clear()
        for data in ("add:ric:0", "page:ric:0", "pat:ric"):
            query = await self.click(data)
            self.assertIn("abgelaufen", sent_text(query.edit_message_text), data)
        self.assertEqual(self.subs(), [])

    async def test_button_for_removed_field(self):
        query = await self.click("add:gibtsnicht:0")
        self.assertIn("nicht mehr konfiguriert", sent_text(query.edit_message_text))

    async def test_empty_list_hint(self):
        env = Env(self)
        update, reply = make_update()
        env.context.args = ["Irgendwas"]
        await handlers.make_add_handler(env.entry("ric"))(update, env.context)
        self.assertIn("Die Liste ist noch leer", sent_text(reply))

    async def test_usage_without_arguments(self):
        self.assertIn("Name oder Muster", sent_text(await self.command("ric", kind="add")))


class PromptTests(CommandTestCase):
    """Befehl ohne Eingabe: Anleitung plus Rückfrage, die Antwort führt den Befehl aus."""

    async def ask(self, for_key, kind="for", user=1, chat=None, chat_type="private"):
        update, reply = make_update(user, chat_id=chat, chat_type=chat_type,
                                    title=None if chat_type == "private" else "Gruppe")
        self.env.context.args = []
        make = handlers.make_for_handler if kind == "for" else handlers.make_add_handler
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
        await handlers.prompt_reply_handler(update, self.context_of(user))
        return reply

    async def test_question_keeps_the_full_help_text(self):
        reply = await self.ask("ric")
        text = sent_text(reply)
        self.assertTrue(text.startswith(handlers.PROMPT_MARKER))
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
        self.assertIn(("➕ Rettungswagen Musterstadt A-Wehr", "add:ric:0"), buttons(reply))

    async def test_adduser_question_and_answer(self):
        update, reply = make_update(1)
        self.env.context.args = []
        await handlers.adduser_handler(update, self.env.context)
        self.assertIn("Telegram-ID", sent_text(reply))
        self.assertIn("/adduser 123456789 Max Mustermann", sent_text(reply))
        self.assertEqual(reply.call_args.kwargs["reply_markup"].input_field_placeholder, "ID und optional Name")

        replied = bot_message(sent_text(reply), reply.return_value.message_id)
        update, _ = make_update(1, text="555 Erika Test", reply_to=replied)
        await handlers.prompt_reply_handler(update, self.env.context)
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
        self.assertEqual(len(self.context_prompts()), handlers.MAX_OPEN_PROMPTS)
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
        await handlers.prompt_reply_handler(update, self.env.context)
        reply.assert_not_called()
        self.assertIsNone(db.get_user_status(self.env.db_path, 77))

    async def test_reply_to_people_is_ignored_and_triggers_no_access_request(self):
        replied = NS(from_user=NS(id=4711), text=f"{handlers.PROMPT_MARKER} sieht nur so aus", message_id=7)
        update, reply = make_update(77, text="ok", reply_to=replied)
        await handlers.prompt_reply_handler(update, self.env.context)
        reply.assert_not_called()
        self.assertIsNone(db.get_user_status(self.env.db_path, 77))
        self.assertEqual(self.env.bot.sent, [])

    async def test_message_without_reply_is_ignored(self):
        update, reply = make_update(77, text="hallo")
        await handlers.prompt_reply_handler(update, self.env.context)
        reply.assert_not_called()

    async def test_stranger_answering_a_question_goes_through_access_request(self):
        prompt = await self.ask("ric", chat=-100, chat_type="group")
        await self.answer(prompt, "9999999", user=77, chat=-100, chat_type="group")
        self.assertEqual(db.get_user_status(self.env.db_path, 77), "pending")
        self.assertEqual(self.subs(-100), [])

    async def test_start_explains_the_questions(self):
        update, reply = make_update()
        await handlers.start_handler(update, self.env.context)
        self.assertIn("fragt dich danach", sent_text(reply))


class CancelTests(CommandTestCase):
    async def test_every_page_of_the_result_list_has_a_cancel_button(self):
        reply = await self.command("ric", "Musterstadt", kind="add")
        self.assertIn(("✖️ Abbrechen", "cancel:ric"), buttons(reply))

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
        self.assertIn(("✖️ Abbrechen", "cancel:ric"), buttons(reply))
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
        await handlers.users_handler(update, self.env.context)
        self.assertIn(("✔️ Schließen", "close_menu"), buttons(reply))
        query = await self.click("close_menu")
        self.assertIn("geschlossen", sent_text(query.edit_message_text))
        self.assertEqual(db.get_user_status(self.env.db_path, 5), "allowed")

    async def test_users_without_buttons_have_no_empty_menu(self):
        update, reply = make_update(1)
        await handlers.users_handler(update, self.env.context)
        self.assertIsNone(reply.call_args.kwargs["reply_markup"])

    async def test_abo_menu_keeps_its_close_button(self):
        await self.command("message", "THL*")
        update, reply = make_update()
        await handlers.abo_handler(update, self.env.context)
        self.assertIn(("✔️ Alles passt", "close_menu"), buttons(reply))


class PagingTests(CommandTestCase):
    csv_rows = [{"for": f"100000{i}", "add": f"Fahrzeug Musterstadt {i}", "isRegex": False} for i in range(1, 8)]

    async def test_paging_and_picking_on_second_page(self):
        reply = await self.command("ric", "Musterstadt", kind="add")
        first = buttons(reply)
        self.assertEqual(len([d for _, d in first if d.startswith("add:")]), 5)
        self.assertIn(("Weiter ➡️", "page:ric:1"), first)
        self.assertNotIn("⬅️ Zurück", [t for t, _ in first])

        query = await self.click("page:ric:1")
        second = buttons(query.edit_message_text)
        self.assertEqual([d for _, d in second if d.startswith("add:")], ["add:ric:5", "add:ric:6"])
        self.assertIn(("⬅️ Zurück", "page:ric:0"), second)

        await self.click("add:ric:6")
        self.assertEqual(self.subs()[0]["target"], "1000007")


class AccessTests(CommandTestCase):
    def setUp(self):
        self.env = Env(self, admins=(1,))

    async def start(self, user):
        update, reply = make_update(user, name="Fremde Person")
        await handlers.start_handler(update, self.env.context)
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
        await handlers.users_handler(update, self.env.context)
        self.assertIn("Nur für Admins", sent_text(reply))

        update, reply = make_update(1)
        self.env.context.args = ["555", "Erika", "Test"]
        await handlers.adduser_handler(update, self.env.context)
        self.assertEqual(db.get_user_status(self.env.db_path, 555), "allowed")
        self.env.context.args = []
        await handlers.users_handler(update, self.env.context)
        self.assertIn("Erika Test", sent_text(reply))

        self.env.context.args = ["abc"]
        await handlers.adduser_handler(update, self.env.context)
        self.assertIn("Ungültige ID", sent_text(reply))


def buttons_of(sent_message):
    markup = sent_message["reply_markup"]
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


class OverviewTests(CommandTestCase):
    async def test_start_lists_all_commands(self):
        update, reply = make_update()
        await handlers.start_handler(update, self.env.context)
        text = sent_text(reply)
        for command in ("/ric", "/description", "/message", "/subrictext", "/abo", "/lastraw"):
            self.assertIn(command, text)

    async def test_start_in_group_mentions_shared_subscriptions(self):
        update, reply = make_update(chat_type="group", chat_id=-100, title="Gruppe")
        await handlers.start_handler(update, self.env.context)
        self.assertIn("für alle Mitglieder", sent_text(reply))

    async def test_abo_lists_with_labels_and_deletes(self):
        await self.command("ric", "1234567")
        await self.command("message", "THL*")
        update, reply = make_update()
        await handlers.abo_handler(update, self.env.context)
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
        await handlers.abo_handler(update, self.env.context)
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
        await handlers.abo_handler(update, self.env.context)
        self.assertIn("keine Abonnements", sent_text(reply))
        update, reply = make_update(chat_type="group", chat_id=-100, title="Gruppe")
        await handlers.abo_handler(update, self.env.context)
        self.assertIn("Diese Gruppe", sent_text(reply))

    async def test_lastraw_suggests_unconfigured_fields(self):
        self.env.context.bot_data["last_payload"] = {
            "ric": "1", "message": "x", "description": "y", "timestamp": "1", "clientName": "c",
            "ric_list": "1", "stadtteil": "Nord", "objekt": "Halle 3"}
        update, reply = make_update()
        await handlers.lastraw_handler(update, self.env.context)
        text = sent_text(reply)
        self.assertIn("EXTRA_FIELDS=stadtteil,objekt", text)
        self.assertNotIn("EXTRA_FIELDS=stadtteil,objekt,", text)

    async def test_lastraw_without_news_and_without_alarm(self):
        update, reply = make_update()
        await handlers.lastraw_handler(update, self.env.context)
        self.assertIn("noch kein Alarm", sent_text(reply))
        self.env.context.bot_data["last_payload"] = {"ric": "1", "message": "x", "description": "y"}
        await handlers.lastraw_handler(update, self.env.context)
        self.assertNotIn("EXTRA_FIELDS", sent_text(reply))


if __name__ == "__main__":
    unittest.main()
