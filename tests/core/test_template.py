import unittest
from unittest.mock import patch

from bos_telegram_bot.core import template as template_module
from bos_telegram_bot.core.template import (BW3_WILDCARDS, DEFAULT_TEMPLATE, MAX_TEMPLATE_LENGTH, SAMPLE_MATCHED, SAMPLE_PAYLOAD,
                                            custom_template_problems, html_problems, render, template_from_fields,
                                            sample_payload, unknown_placeholders, validate_template)
from tests.helpers import BERLIN, time_zone

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


class SampleAlarmTests(unittest.TestCase):
    """Der erfundene Alarm für die Vorschau von /template."""

    def test_the_default_template_renders_like_a_real_alarm(self):
        text = render(DEFAULT_TEMPLATE, SAMPLE_PAYLOAD, SAMPLE_MATCHED)
        self.assertEqual(text, "🚨 <b>BOS-ALARM</b> 🚨\n\nWache Nord (Beispiel)\nFahrzeug 1 (Beispiel)\n"
                               "THL 1 - Beispieltext, bitte nicht ernst nehmen\n\n<i>abonniert über: RIC: 1000011</i>")

    def test_it_has_the_usual_fields_of_an_alarm_and_the_multicast_lists(self):
        for key in ("ric", "message", "description", "subric", "subricText", "mode", "timestamp", "ric_list",
                    "description_list", "timestamp_list", "multicastRecipientCount", "frequency", "clientName", "serverName"):
            self.assertIn(key, SAMPLE_PAYLOAD)

    def test_all_values_are_text_like_in_real_alarms(self):
        self.assertTrue(all(isinstance(value, str) for value in SAMPLE_PAYLOAD.values()))

    def test_the_values_that_look_like_addresses_come_from_the_ranges_reserved_for_examples(self):
        self.assertEqual(SAMPLE_PAYLOAD["clientIP"], "192.0.2.10")                    # Adressbereich nur für Beispiele (RFC 5737)
        self.assertEqual(SAMPLE_PAYLOAD["frequency"], "123.450M")

    def test_the_matched_name_is_a_list_like_in_real_use(self):
        self.assertEqual(SAMPLE_MATCHED, ["RIC: 1000011"])

    def test_the_lists_of_the_sample_belong_together(self):
        self.assertEqual(len(SAMPLE_PAYLOAD["ric_list"].split(", ")), len(SAMPLE_PAYLOAD["description_list"].split(", ")))


class CustomTemplateTests(unittest.TestCase):
    """Die Prüfung für ein Template, das ein User per /template setzen will."""

    def test_the_default_template_is_valid(self):
        self.assertEqual(custom_template_problems(DEFAULT_TEMPLATE), [])

    def test_the_simplest_template_is_valid(self):
        self.assertEqual(custom_template_problems("{MESSAGE}"), [])

    def test_everything_the_start_check_refuses_is_refused_here_too(self):
        for bad in ("", "   ", "<b>{MESSAGE}", "{MESSAGE} {", "<blink>{MESSAGE}</blink>", None, 5):
            self.assertTrue(custom_template_problems(bad), repr(bad))

    def test_without_a_placeholder_the_message_would_have_no_content(self):
        (problem,) = custom_template_problems("Alarm!")
        self.assertIn("enthält keinen Platzhalter", problem)

    def test_a_literal_brace_pair_is_no_placeholder(self):
        self.assertTrue(any("keinen Platzhalter" in p for p in custom_template_problems("{{MESSAGE}}")))

    def test_a_placeholder_with_a_fallback_counts(self):
        self.assertEqual(custom_template_problems("{DESCRIPTION_LIST|RIC_LIST}"), [])

    def test_the_length_limit(self):
        ok = "{MESSAGE}" + "x" * (MAX_TEMPLATE_LENGTH - len("{MESSAGE}"))
        self.assertEqual(custom_template_problems(ok), [])
        (problem,) = custom_template_problems(ok + "y")
        self.assertIn(f"ist zu lang ({MAX_TEMPLATE_LENGTH + 1} Zeichen, erlaubt sind {MAX_TEMPLATE_LENGTH})", problem)

    def test_the_limit_leaves_room_for_the_alarm_inside_telegrams_4096_characters(self):
        self.assertLessEqual(MAX_TEMPLATE_LENGTH, 2000)

    def test_several_problems_are_reported_together(self):
        problems = custom_template_problems("<b>" + "x" * (MAX_TEMPLATE_LENGTH + 5))
        self.assertGreaterEqual(len(problems), 3)                                    # nicht geschlossen, zu lang, kein Platzhalter

    def test_an_empty_template_is_not_also_reported_as_having_no_placeholder(self):
        self.assertEqual(custom_template_problems(""), ["ist leer"])


class UnknownPlaceholderTests(unittest.TestCase):
    def check(self, template, expected):
        self.assertEqual(unknown_placeholders(template, SAMPLE_PAYLOAD), expected, template)

    def test_known_placeholders_are_not_reported(self):
        self.check("{MESSAGE} {RIC} {DESCRIPTION} {SUBRICTEXT} {FREQUENCY}", [])

    def test_the_spelling_of_the_case_does_not_matter(self):
        self.check("{message} {Subrictext} {SubricText}", [])

    def test_lists_are_known_when_the_list_or_the_single_value_exists(self):
        self.check("{RIC_LIST} {MESSAGE_LIST} {FREQUENCY_LIST}", [])                  # frequency_list fehlt, frequency gibt es

    def test_matched_is_always_known(self):
        self.check("{MATCHED}", [])

    def test_a_typo_is_reported(self):
        self.check("{MESSGAE} {RIC}", ["MESSGAE"])

    def test_an_expression_with_a_fallback_is_known_if_any_alternative_is(self):
        self.check("{GIBTSNICHT|MESSAGE}", [])
        self.check("{GIBTSNICHT|AUCHNICHT}", ["GIBTSNICHT|AUCHNICHT"])

    def test_every_unknown_one_is_reported_once_in_order(self):
        self.check("{B} {A} {B} {MESSAGE} {A}", ["B", "A"])

    def test_literal_braces_are_ignored(self):
        self.check("{{KEIN_PLATZHALTER}} {MESSAGE}", [])

    def test_a_list_suffix_alone_is_not_a_known_field(self):
        self.check("{_LIST}", ["_LIST"])

    def test_it_works_on_any_payload(self):
        self.assertEqual(unknown_placeholders("{MESSAGE} {RIC}", {"message": "x"}), ["RIC"])

    def test_a_template_without_placeholders_has_no_unknown_ones(self):
        self.check("nur Text", [])

    def test_an_unknown_one_stays_empty_in_the_rendering(self):
        text = render("{MESSAGE} {GIBTSNICHT}", SAMPLE_PAYLOAD, SAMPLE_MATCHED)
        self.assertNotIn("GIBTSNICHT", text)
        self.assertEqual(text.strip(), SAMPLE_PAYLOAD["message"])                    # nur das Leerzeichen davor bleibt


STAMP = "1700000000.5"         # 14.11.2023 22:13:20 UTC


class TimeWildcardTests(unittest.TestCase):
    """{TIME}: der Zeitpunkt des Alarms, Format wie bei BOSWatch3 (%d.%m.%Y %H:%M:%S)."""

    def setUp(self):
        zone = time_zone("UTC")
        zone.__enter__()
        self.addCleanup(zone.__exit__, None, None, None)

    def time(self, payload, template="{TIME}"):
        return render(template, payload, [])

    def test_it_is_the_time_of_the_alarm_in_the_format_of_boswatch(self):
        self.assertEqual(self.time({"timestamp": STAMP}), "14.11.2023 22:13:20")

    def test_the_time_zone_of_the_container_applies_in_winter_and_summer(self):
        with time_zone(BERLIN):
            self.assertEqual(self.time({"timestamp": STAMP}), "14.11.2023 23:13:20")          # MEZ, +1
            self.assertEqual(self.time({"timestamp": "1689332000"}), "14.07.2023 12:53:20")   # MESZ, +2

    def test_a_multicast_uses_the_first_timestamp_of_the_list(self):
        payload = {"timestamp": "1700000900.0", "timestamp_list": f"{STAMP}, 1700000900.0"}
        self.assertEqual(self.time(payload), "14.11.2023 22:13:20")

    def test_every_copy_of_a_multicast_shows_the_same_time(self):
        lists = f"{STAMP}, 1700000900.0"
        first = self.time({"timestamp": STAMP, "timestamp_list": lists})
        second = self.time({"timestamp": "1700000900.0", "timestamp_list": lists})
        self.assertEqual(first, second)

    def test_without_a_list_the_single_timestamp_is_used(self):
        self.assertEqual(self.time({"timestamp": STAMP, "timestamp_list": ""}), "14.11.2023 22:13:20")

    def test_a_missing_or_unreadable_timestamp_gives_the_current_time(self):
        with patch.object(template_module, "_now", return_value=1700000000.0):
            for payload in ({}, {"timestamp": ""}, {"timestamp": "abc"}, {"timestamp": "nan"}, {"timestamp": "inf"},
                            {"timestamp": "1e999"}, {"timestamp": "-1e30"}, {"timestamp": None}, {"timestamp_list": ", ,"}):
                self.assertEqual(self.time(payload), "14.11.2023 22:13:20", payload)

    def test_a_field_called_time_in_the_alarm_has_priority(self):
        self.assertEqual(self.time({"time": "12:00 Uhr", "timestamp": STAMP}), "12:00 Uhr")

    def test_the_spelling_of_the_case_does_not_matter(self):
        self.assertEqual(self.time({"timestamp": STAMP}, "{time} {Time}"), "14.11.2023 22:13:20 14.11.2023 22:13:20")

    def test_it_works_inside_a_line_with_other_text_and_in_a_fallback(self):
        payload = {"timestamp": STAMP, "message": "TEST"}
        self.assertEqual(self.time(payload, "Alarm {TIME}: {MESSAGE}"), "Alarm 14.11.2023 22:13:20: TEST")
        self.assertEqual(self.time(payload, "{GIBTSNICHT|TIME}"), "14.11.2023 22:13:20")

    def test_it_is_not_computed_when_the_template_does_not_use_it(self):
        with patch.object(template_module, "_alarm_time", side_effect=AssertionError("nicht nötig")):
            self.assertEqual(self.time({"message": "x"}, "{MESSAGE}"), "x")

    def test_it_is_not_a_field_of_the_payload_and_does_not_change_it(self):
        payload = {"timestamp": STAMP}
        self.time(payload)
        self.assertEqual(payload, {"timestamp": STAMP})

    def test_the_sample_alarm_gets_the_current_time(self):
        with patch.object(template_module, "_now", return_value=1700000000.0):
            sample = sample_payload()
        self.assertEqual(self.time(sample), "14.11.2023 22:13:20")
        self.assertEqual(self.time(sample_payload(now=1700000900.0)), "14.11.2023 22:28:20")

    def test_the_sample_alarm_keeps_all_its_other_fields(self):
        sample = sample_payload(now=1.0)
        self.assertEqual({k: v for k, v in sample.items() if not k.startswith("timestamp")},
                         {k: v for k, v in SAMPLE_PAYLOAD.items() if not k.startswith("timestamp")})
        self.assertEqual(SAMPLE_PAYLOAD["timestamp"], "1700000000.5")                  # die Konstante bleibt unverändert


class LiteralWildcardTests(unittest.TestCase):
    """{BR}, {LPAR} und {RPAR}: reiner Text wie bei BOSWatch3, kein Datenfeld."""
    PAYLOAD = {"ric": "1000011", "message": "TEST"}

    def render(self, template):
        return render(template, self.PAYLOAD, [])

    def test_br_is_a_line_break(self):
        self.assertEqual(self.render("{RIC}{BR}{MESSAGE}"), "1000011\nTEST")

    def test_the_case_does_not_matter(self):
        self.assertEqual(self.render("{ric}{br}{Message}"), "1000011\nTEST")

    def test_two_in_a_row_make_one_blank_line(self):
        self.assertEqual(self.render("{RIC}{BR}{BR}{MESSAGE}"), "1000011\n\nTEST")

    def test_many_in_a_row_are_tidied_like_blank_lines_in_the_template(self):
        self.assertEqual(self.render("{RIC}{BR}{BR}{BR}{BR}{MESSAGE}"), "1000011\n\nTEST")

    def test_parentheses(self):
        self.assertEqual(self.render("{LPAR}{RIC}{RPAR} {MESSAGE}"), "(1000011) TEST")

    def test_a_line_whose_value_is_missing_is_dropped_despite_the_parentheses(self):
        self.assertEqual(self.render("{LPAR}{GIBTSNICHT}{RPAR}\n{MESSAGE}"), "TEST")

    def test_a_template_of_only_line_breaks_gives_the_default(self):
        self.assertIn("BOS-ALARM", self.render("{BR}{BR}"))

    def test_parentheses_alone_are_just_text(self):
        self.assertEqual(self.render("{LPAR}{RPAR}"), "()")                           # ein eigenes Template so lehnt der Bot ab

    def test_a_line_break_inside_a_line_splits_it_so_a_missing_value_only_drops_its_own_part(self):
        self.assertEqual(self.render("{GIBTSNICHT}{BR}{MESSAGE}"), "TEST")

    def test_doubled_braces_keep_them_as_text(self):
        self.assertEqual(self.render("{{BR}} {{LPAR}} {MESSAGE}"), "{BR} {LPAR} TEST")

    def test_a_list_line_is_repeated_but_the_part_after_a_break_is_not(self):
        payload = {"description_list": "Wache A, Wache B", "message": "TEST"}
        self.assertEqual(render("• {DESCRIPTION_LIST}{BR}Ende", payload, []), "• Wache A\n• Wache B\nEnde")

    def test_the_start_check_accepts_them(self):
        self.assertEqual(validate_template("{RIC}{BR}{LPAR}{MESSAGE}{RPAR}"), [])

    def test_for_a_custom_template_they_alone_are_no_content(self):
        for text in ("{BR}", "{LPAR}{RPAR}", "{BR}{br}"):
            self.assertTrue(any("keinen Platzhalter" in p for p in custom_template_problems(text)), text)
        self.assertEqual(custom_template_problems("{BR}{MESSAGE}"), [])

    def test_the_time_alone_is_accepted_as_a_custom_template(self):
        self.assertEqual(custom_template_problems("{TIME}"), [])

    def test_an_alarm_field_called_br_is_not_used_instead_of_the_line_break(self):
        self.assertEqual(render("a{BR}b", {"br": "X"}, []), "a\nb")


class ShortNameTests(unittest.TestCase):
    """Die Kurznamen von BOSWatch3 ({MSG}, {SRICT} ...) stehen für die Felder des Pakets."""

    def test_every_short_name_gives_the_value_of_its_field(self):
        for short, field in BW3_WILDCARDS.items():
            self.assertEqual(render(f"{{{short.upper()}}}", {field: "Wert<&>"}, []), "Wert&lt;&amp;&gt;", short)

    def test_the_table_matches_the_documentation_of_boswatch3(self):
        for short, field in (("msg", "message"), ("srict", "subricText"), ("sric", "subric"), ("freq", "frequency"),
                             ("bit", "bitrate"), ("times", "timestamp"), ("cip", "clientIP"), ("sname", "serverName"),
                             ("insrc", "inputSource"), ("loc", "location"), ("dirt", "directionText"),
                             ("taci", "tacticalInfo")):
            self.assertEqual(BW3_WILDCARDS[short], field)

    def test_the_names_of_the_fields_still_work(self):
        self.assertEqual(render("{MESSAGE} {SUBRICTEXT} {FREQUENCY}", {"message": "A", "subricText": "b", "frequency": "1"}, []),
                         "A b 1")

    def test_both_spellings_give_the_same(self):
        payload = {"message": "TEST", "subricText": "a"}
        self.assertEqual(render("{MSG} {SRICT}", payload, []), render("{MESSAGE} {SUBRICTEXT}", payload, []))

    def test_a_field_with_exactly_that_name_has_priority_over_the_short_name(self):
        payload = {"msg": "kurz", "message": "lang"}
        self.assertEqual(render("{MSG} {MESSAGE}", payload, []), "kurz lang")

    def test_lists_work_with_short_names_too(self):
        payload = {"message_list": "A, B", "description_list": "Wache A, Wache B"}
        self.assertEqual(render("{MSG_LIST}", payload, []), "A\nB")

    def test_a_real_list_field_with_the_short_name_has_priority(self):
        self.assertEqual(render("{MSG_LIST}", {"msg_list": "X, Y", "message_list": "A, B"}, []), "X\nY")

    def test_lists_that_exist_under_their_own_name_are_unaffected(self):
        self.assertEqual(render("{RIC_LIST}", {"ric_list": "1, 2"}, []), "1\n2")

    def test_a_missing_field_leaves_the_line_empty_like_before(self):
        self.assertEqual(render("Ort: {LOC}\n{MESSAGE}", {"message": "TEST"}, []), "TEST")

    def test_they_work_in_a_fallback(self):
        self.assertEqual(render("{LOC|MSG}", {"message": "TEST"}, []), "TEST")

    def test_the_spelling_of_the_case_does_not_matter(self):
        self.assertEqual(render("{Msg} {msg} {MSG}", {"message": "T"}, []), "T T T")

    def test_a_name_that_is_neither_a_field_nor_a_short_name_stays_empty(self):
        self.assertEqual(render("{FOO}\n{MESSAGE}", {"foo_bar": "x", "message": "T"}, []), "T")

    def test_the_time_stamp_short_name_gives_the_raw_timestamp_and_not_the_formatted_time(self):
        self.assertEqual(render("{TIMES}", {"timestamp": STAMP}, []), STAMP)

    def test_matched_is_not_a_short_name(self):
        self.assertEqual(render("{MATCHED}", {"matched": "feld"}, ["RIC: 1"]), "RIC: 1")

    def test_the_unknown_check_knows_the_short_names_only_when_the_field_exists(self):
        self.assertEqual(unknown_placeholders("{MSG} {SRICT}", {"message": "x"}), ["SRICT"])
        self.assertEqual(unknown_placeholders("{MSG_LIST}", {"message": "x"}), [])
        self.assertEqual(unknown_placeholders("{MSG}", {"ric": "1"}), ["MSG"])

    def test_the_unknown_check_knows_time_and_the_text_wildcards_always(self):
        self.assertEqual(unknown_placeholders("{TIME} {BR} {LPAR} {RPAR} {time}", {}), [])

    def test_the_unknown_check_still_reports_typos(self):
        self.assertEqual(unknown_placeholders("{TIEM} {MSGG}", SAMPLE_PAYLOAD), ["TIEM", "MSGG"])

    def test_the_sample_alarm_knows_all_short_names_of_a_pocsag_alarm(self):
        pocsag = "{SNAME} {SVERS} {SBRCH} {CNAME} {CIP} {CVERS} {CBRCH} {INSRC} {TIMES} {FREQ} {MODE} {BIT} {RIC} {SRIC} {SRICT} {MSG}"
        self.assertEqual(unknown_placeholders(pocsag, SAMPLE_PAYLOAD), [])


if __name__ == "__main__":
    unittest.main()
