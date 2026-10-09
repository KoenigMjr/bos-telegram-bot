import unittest


from bos_telegram_bot.chat import fields
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase, matches, reply_buttons
from tests.helpers import Env, make_update, sent_text


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
        labels = [text for text, _ in reply_buttons(reply)]
        self.assertTrue(any("Rettungswagen" in t for t in labels))
        self.assertTrue(any("als Muster" in t for t in labels))
        self.assertIn(("➕ Rettungswagen Musterstadt A-Wehr", "add:ric:0"), reply_buttons(reply))

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
        self.assertEqual(reply_buttons(reply), [("🔎 Trotzdem als Muster anlegen", "pat:ric"),
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
        await fields.make_add_handler(env.entry("ric"))(update, env.context)
        self.assertIn("Die Liste ist noch leer", sent_text(reply))

    async def test_usage_without_arguments(self):
        self.assertIn("Name oder Muster", sent_text(await self.command("ric", kind="add")))


class PagingTests(CommandTestCase):
    csv_rows = [{"for": f"100000{i}", "add": f"Fahrzeug Musterstadt {i}", "isRegex": False} for i in range(1, 8)]

    async def test_paging_and_picking_on_second_page(self):
        reply = await self.command("ric", "Musterstadt", kind="add")
        first = reply_buttons(reply)
        self.assertEqual(len([d for _, d in first if d.startswith("add:")]), 5)
        self.assertIn(("Weiter ➡️", "page:ric:1"), first)
        self.assertNotIn("⬅️ Zurück", [t for t, _ in first])

        query = await self.click("page:ric:1")
        second = reply_buttons(query.edit_message_text)
        self.assertEqual([d for _, d in second if d.startswith("add:")], ["add:ric:5", "add:ric:6"])
        self.assertIn(("⬅️ Zurück", "page:ric:0"), second)

        await self.click("add:ric:6")
        self.assertEqual(self.subs()[0]["target"], "1000007")


if __name__ == "__main__":
    unittest.main()
