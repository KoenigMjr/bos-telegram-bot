import unittest

from bos_telegram_bot.core.template import (DEFAULT_TEMPLATE, html_problems, render, template_from_fields,
                      validate_template)

# Multicast-Alarm in der Struktur, wie BOSWatch3 ihn liefert (alle Werte erfunden).
MULTICAST = {
    "ric": "1000043", "ric_list": "1000011, 1000043",
    "description": "1000043", "description_list": "Lagedienst Musterstadt, 1000043",
    "message": "TEST Beispieltext", "message_list": ", ",
    "subricText": "a", "subricText_list": "a, a",
}
SINGLE = {"ric": "1000011", "description": "Wache Nord", "message": "THL 1"}


class DefaultLayoutTests(unittest.TestCase):
    def test_multicast(self):
        self.assertEqual(
            render(DEFAULT_TEMPLATE, MULTICAST, ["RIC: 1000011"]),
            "🚨 <b>BOS-ALARM</b> 🚨\n\nLagedienst Musterstadt\n1000043\nTEST Beispieltext\n\n"
            "<i>abonniert über: RIC: 1000011</i>")

    def test_single_alarm(self):
        self.assertEqual(
            render(DEFAULT_TEMPLATE, SINGLE, ["RIC: 1000011"]),
            "🚨 <b>BOS-ALARM</b> 🚨\n\nWache Nord\nTHL 1\n\n<i>abonniert über: RIC: 1000011</i>")

    def test_without_description_the_ric_is_used(self):
        text = render(DEFAULT_TEMPLATE, {"ric": "1000011", "message": "THL 1"}, ["x"])
        self.assertIn("\n1000011\nTHL 1\n", text)

    def test_without_description_in_multicast_all_rics_are_used(self):
        payload = {"ric": "2", "ric_list": "1, 2", "message": "X"}
        self.assertIn("\n1\n2\nX\n", render(DEFAULT_TEMPLATE, payload, ["x"]))

    def test_default_template_is_valid(self):
        self.assertEqual(validate_template(DEFAULT_TEMPLATE), [])


class PlaceholderTests(unittest.TestCase):
    def test_field_lookup_ignores_case(self):
        for placeholder in ("{SUBRICTEXT}", "{subrictext}", "{SubricText}"):
            self.assertEqual(render(placeholder, MULTICAST, []), "a", placeholder)

    def test_single_value_is_the_primary_one(self):
        self.assertEqual(render("{RIC}", MULTICAST, []), "1000043")

    def test_list_gives_one_line_per_entry(self):
        self.assertEqual(render("{RIC_LIST}", MULTICAST, []), "1000011\n1000043")

    def test_empty_and_duplicate_entries_are_dropped(self):
        payload = {"x_list": "a, , b, a, b, "}
        self.assertEqual(render("{X_LIST}", payload, []), "a\nb")

    def test_empty_list_falls_back_to_the_single_value(self):
        self.assertEqual(render("{MESSAGE_LIST}", MULTICAST, []), "TEST Beispieltext")   # message_list = ", "

    def test_missing_list_falls_back_to_the_single_value(self):
        self.assertEqual(render("{DESCRIPTION_LIST}", SINGLE, []), "Wache Nord")

    def test_list_field_that_exists_on_its_own(self):
        self.assertEqual(render("{CLIENTNAME_LIST}", {"clientName_list": "A, B"}, []), "A\nB")

    def test_matched(self):
        self.assertEqual(render("{MATCHED}", SINGLE, ["RIC: 1", "THL*"]), "RIC: 1, THL*")

    def test_fallback_chain(self):
        self.assertEqual(render("{DESCRIPTION|RIC}", {"ric": "7"}, []), "7")
        self.assertEqual(render("{DESCRIPTION|RIC}", {"ric": "7", "description": "Name"}, []), "Name")
        self.assertEqual(render("{A|B|C}", {"c": "3"}, []), "3")

    def test_fallback_between_list_and_single(self):
        self.assertEqual(render("{DESCRIPTION_LIST|RIC_LIST}", {"ric_list": "1, 2"}, []), "1\n2")

    def test_numbers_and_booleans_become_text(self):
        self.assertEqual(render("{N} {FLAG}", {"n": 5, "flag": True}, []), "5 True")

    def test_none_and_missing_are_empty(self):
        self.assertEqual(render("Text\n{X}", {"x": None}, []), "Text")

    def test_static_text_around_an_empty_placeholder_does_not_keep_the_line(self):
        self.assertEqual(render("Text\nKennung: {X}\n[{X}]", {"x": None}, []), "Text")

    def test_literal_braces(self):
        self.assertEqual(render("{{RIC}} = {RIC}", SINGLE, []), "{RIC} = 1000011")

    def test_text_without_placeholders_is_unchanged(self):
        self.assertEqual(render("Nur Text", SINGLE, []), "Nur Text")


class LineRuleTests(unittest.TestCase):
    def test_line_with_only_empty_placeholders_is_dropped(self):
        self.assertEqual(render("A\nKennung: {GIBTSNICHT}\nB", SINGLE, []), "A\nB")

    def test_unknown_placeholder_is_just_empty(self):
        self.assertEqual(render("{TIPPFEHLER}\n{RIC}", SINGLE, []), "1000011")

    def test_line_with_some_empty_placeholders_stays(self):
        self.assertEqual(render("{RIC} {GIBTSNICHT}!", SINGLE, []), "1000011 !")

    def test_a_line_without_placeholders_is_kept_even_when_blank(self):
        self.assertEqual(render("A\n\nB", SINGLE, []), "A\n\nB")

    def test_consecutive_blank_lines_collapse_when_lines_disappear(self):
        text = render("Kopf\n\n{X}\n{Y}\n\nFuß", SINGLE, [])
        self.assertEqual(text, "Kopf\n\nFuß")

    def test_no_blank_lines_at_the_start_or_end(self):
        self.assertEqual(render("{X}\n\nText\n\n{Y}", SINGLE, []), "Text")

    def test_list_line_is_repeated_per_entry(self):
        self.assertEqual(render("• {DESCRIPTION_LIST}", MULTICAST, []), "• Lagedienst Musterstadt\n• 1000043")

    def test_repeated_line_keeps_other_values(self):
        self.assertEqual(render("{RIC_LIST} ({MESSAGE})", MULTICAST, []),
                         "1000011 (TEST Beispieltext)\n1000043 (TEST Beispieltext)")

    def test_two_lists_in_one_line_are_joined_inline(self):
        self.assertEqual(render("{RIC_LIST} / {DESCRIPTION_LIST}", MULTICAST, []),
                         "1000011, 1000043 / Lagedienst Musterstadt, 1000043")

    def test_same_list_twice_in_a_line_counts_once(self):
        self.assertEqual(render("{RIC_LIST}={RIC_LIST}", {"ric_list": "1, 2"}, []), "1=1\n2=2")

    def test_empty_list_in_a_line_with_other_values(self):
        self.assertEqual(render("[{X_LIST}] {RIC}", SINGLE, []), "[] 1000011")

    def test_everything_empty_falls_back_to_the_default_layout(self):
        text = render("{GIBTSNICHT}", SINGLE, ["RIC: 1000011"])
        self.assertTrue(text.startswith("🚨 <b>BOS-ALARM</b> 🚨"))

    def test_windows_line_endings(self):
        self.assertEqual(render("A\r\n{RIC}\r\nB", SINGLE, []), "A\n1000011\nB")

    def test_yaml_block_scalar_with_trailing_newline(self):
        self.assertEqual(render("{RIC}\n", SINGLE, []), "1000011")


class EscapingTests(unittest.TestCase):
    NASTY = {"message": "<b>x</b> & \"y\" 'z' <script>", "ric": "1"}

    def test_values_are_escaped(self):
        text = render("{MESSAGE}", self.NASTY, [])
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp;", text)

    def test_template_tags_stay_tags(self):
        self.assertEqual(render("<b>{RIC}</b>", SINGLE, []), "<b>1000011</b>")

    def test_matched_aliases_are_escaped(self):
        self.assertEqual(render("{MATCHED}", SINGLE, ["A & B <x>"]), "A &amp; B &lt;x&gt;")

    def test_list_entries_are_escaped(self):
        self.assertEqual(render("{X_LIST}", {"x_list": "a<b, c&d"}, []), "a&lt;b\nc&amp;d")

    def test_rendered_output_is_always_valid_telegram_html(self):
        templates = [DEFAULT_TEMPLATE, "<b>{MESSAGE}</b>\n<i>{MATCHED}</i>\n<code>{RIC_LIST}</code>",
                     "• {MESSAGE_LIST}\n{{x}}"]
        nasty_values = ["<", ">", "&", "&amp;", "<b>", "</i>", "a < b > c", '"', "'", "\\", "{X}", "{{", "}}"]
        for template in templates:
            for value in nasty_values:
                payload = {"message": value, "message_list": f"{value}, {value}x", "ric": value, "ric_list": f"{value}, 2"}
                text = render(template, payload, [value])
                self.assertEqual(html_problems(text), [], f"{template!r} mit {value!r}: {text!r}")

    def test_nul_characters_in_values_are_removed(self):
        self.assertEqual(render("{X}", {"x": "a\x00b"}, []), "ab")

    def test_value_that_looks_like_a_placeholder_is_not_expanded(self):
        self.assertEqual(render("{X}", {"x": "{RIC}", "ric": "1"}, []), "{RIC}")


class FromFieldsTests(unittest.TestCase):
    def test_old_setting_produces_one_list_line_per_field(self):
        self.assertEqual(
            template_from_fields(["description", "message", "ric"]),
            "🚨 <b>BOS-ALARM</b> 🚨\n\n{DESCRIPTION_LIST}\n{MESSAGE_LIST}\n{RIC_LIST}\n\n<i>abonniert über: {MATCHED}</i>")

    def test_old_layout_output(self):
        text = render(template_from_fields(["description", "message", "ric"]), MULTICAST, ["Wache"])
        self.assertEqual(text, "🚨 <b>BOS-ALARM</b> 🚨\n\nLagedienst Musterstadt\n1000043\nTEST Beispieltext\n"
                               "1000011\n1000043\n\n<i>abonniert über: Wache</i>")

    def test_old_layout_for_a_single_alarm(self):
        text = render(template_from_fields(["description", "message", "ric"]), SINGLE, ["x"])
        self.assertEqual(text.split("\n")[2:5], ["Wache Nord", "THL 1", "1000011"])


class ValidationTests(unittest.TestCase):
    def problems(self, template):
        return " | ".join(validate_template(template))

    def test_valid_templates(self):
        for template in ("{MESSAGE}", "<b>{MESSAGE}</b>\n<i>x</i> <code>{RIC}</code>",
                         "<a href=\"https://example.org/{RIC}\">Link</a>", "<pre>{MESSAGE}</pre>",
                         "<blockquote>{MESSAGE}</blockquote>", "A &amp; B &lt; C", "{{literal}}",
                         "{A|B}", "Nur Text"):
            self.assertEqual(validate_template(template), [], template)

    def test_empty_and_wrong_type(self):
        self.assertIn("leer", self.problems("   \n"))
        self.assertIn("Text sein", self.problems({"MESSAGE": None}))      # YAML ohne '|' ergibt einen Block
        self.assertIn("Text sein", self.problems(["a"]))

    def test_unbalanced_and_unknown_tags(self):
        self.assertIn("nicht geschlossen", self.problems("<b>{MESSAGE}"))
        self.assertIn("schließt nichts Offenes", self.problems("{MESSAGE}</b>"))
        self.assertIn("falscher Reihenfolge", self.problems("<b><i>x</b></i>"))
        self.assertIn("<div>", self.problems("<div>x</div>"))

    def test_stray_angle_brackets_and_ampersand(self):
        self.assertIn("&lt;", self.problems("a < b"))
        self.assertIn("&lt;", self.problems("a > b"))
        self.assertIn("&amp;", self.problems("Alarm & Einsatz"))

    def test_broken_braces(self):
        self.assertIn("außerhalb eines Platzhalters", self.problems("{MESSAGE"))
        self.assertIn("außerhalb eines Platzhalters", self.problems("MESSAGE}"))
        self.assertIn("außerhalb eines Platzhalters", self.problems("{MES SAGE}"))
        self.assertIn("außerhalb eines Platzhalters", self.problems("{}"))
        self.assertIn("außerhalb eines Platzhalters", self.problems("{A||B}"))

    def test_all_problems_are_reported_together(self):
        problems = validate_template("<div>{OFFEN\nAlarm & Einsatz")
        self.assertGreaterEqual(len(problems), 3)


if __name__ == "__main__":
    unittest.main()
