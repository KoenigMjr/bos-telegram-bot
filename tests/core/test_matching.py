import re
import unittest

from bos_telegram_bot.core.matching import (candidate_values, has_range_syntax, has_wildcard_syntax, is_pattern_input, match_sub,
                      number_at_least_regex, split_list, split_list_aligned, wildcard_to_regex)


def sub(target, is_regex=False, alias="Alias"):
    return {"target": target, "is_regex": is_regex, "alias": alias}


class WildcardTests(unittest.TestCase):
    def matches(self, pattern, value):
        return bool(re.match(wildcard_to_regex(pattern), value))

    def test_prefix(self):
        self.assertTrue(self.matches("THL*", "THL Türöffnung"))
        self.assertFalse(self.matches("THL*", "RD THL"))

    def test_percent_is_star(self):
        self.assertTrue(self.matches("THL%", "THL Türöffnung"))

    def test_contains_without_wildcard(self):
        self.assertTrue(self.matches("THL", "Alarm THL2 Wasserschaden"))
        self.assertFalse(self.matches("THL", "RD 1"))

    def test_star_both_sides(self):
        self.assertTrue(self.matches("*301*", "0230100"))
        self.assertFalse(self.matches("*301*", "0400000"))

    def test_numeric_prefix_does_not_match_leading_zero(self):
        self.assertTrue(self.matches("301*", "3010001"))
        self.assertFalse(self.matches("301*", "0230100"))

    def test_question_mark_is_one_char(self):
        self.assertTrue(self.matches("THL?", "THL1"))
        self.assertFalse(self.matches("THL?", "THL12"))

    def test_regex_prefix(self):
        self.assertTrue(self.matches(r"re:^RD\s?\d", "RD 2 - Herz"))

    def test_regex_special_chars_are_escaped(self):
        self.assertFalse(self.matches("a.b*", "axb1"))
        self.assertTrue(self.matches("a.b*", "a.b1"))

    def test_case_insensitive_for_wildcard_patterns(self):
        self.assertTrue(self.matches("thl*", "THL Türöffnung"))
        self.assertTrue(self.matches("*wagen*", "Rettungswagen"))
        self.assertTrue(self.matches("*WAGEN*", "Rettungswagen"))

    def test_raw_regex_stays_case_sensitive(self):
        self.assertFalse(self.matches("re:^thl", "THL Tür"))
        self.assertTrue(self.matches("re:(?i)^thl", "THL Tür"))

    def test_invalid_regex_raises(self):
        with self.assertRaises(re.error):
            wildcard_to_regex("re:(")

    def test_has_wildcard_syntax(self):
        for text in ("301*", "*x", "a%", "a?", "re:x", "RE:x"):
            self.assertTrue(has_wildcard_syntax(text), text)
        for text in ("1000001", "Rettungswagen Musterstadt", ""):
            self.assertFalse(has_wildcard_syntax(text), text)


class NumberAtLeastTests(unittest.TestCase):
    """Der Zahlenvergleich wird gegen die normale Python-Rechnung geprüft."""

    @staticmethod
    def matcher(digits):
        return re.compile(number_at_least_regex(digits))

    def test_every_threshold_up_to_300_against_python(self):
        for n in range(0, 301):
            rx = self.matcher(str(n))
            for v in range(0, 1501):
                self.assertEqual(bool(rx.fullmatch(str(v))), v >= n, f"{v} >= {n}")

    def test_digit_count_boundaries(self):
        for n in (9, 10, 99, 100, 999, 1000, 9999, 10000, 123456, 999999):
            rx = self.matcher(str(n))
            for v in list(range(n - 12, n + 12)) + [10 ** 9, 10 ** 12, n * 7, n * 10]:
                if v >= 0:
                    self.assertEqual(bool(rx.fullmatch(str(v))), v >= n, f"{v} >= {n}")

    def test_leading_zero_compares_digit_by_digit_in_same_length(self):
        for s in ("0230100", "0000000", "0999999", "0009", "0123456"):
            rx = self.matcher(s)
            width = len(s)
            for v in range(max(0, int(s) - 40), min(10 ** width, int(s) + 40)):
                self.assertEqual(bool(rx.fullmatch(f"{v:0{width}d}")), v >= int(s), f"{v} >= {s}")
            self.assertFalse(rx.fullmatch("1" + s), "längere Zeichenkette darf nicht passen")
            self.assertFalse(rx.fullmatch(s[1:]), "kürzere Zeichenkette darf nicht passen")

    def test_zero_matches_any_number(self):
        self.assertTrue(self.matcher("0").fullmatch("0"))
        self.assertTrue(self.matcher("0").fullmatch("57"))


class RangeSyntaxTests(unittest.TestCase):
    def matches(self, pattern, text):
        return bool(re.match(wildcard_to_regex(pattern), text))

    def test_b3_and_higher(self):
        for text in ("B 3", "B 3 - Wohnungsbrand", "B 4", "B 5 - Großbrand", "B 10", "B 30", "B 120 - Lage"):
            self.assertTrue(self.matches("B 3+", text), text)
        for text in ("B 1", "B 2 - Zimmerbrand", "B 0", "RD 3", "THL 4 - Unfall"):
            self.assertFalse(self.matches("B 3+", text), text)

    def test_every_number_in_a_text_against_python(self):
        for threshold in (1, 3, 9, 10, 25):
            regex = wildcard_to_regex(f"B {threshold}+")
            for v in range(0, 200):
                self.assertEqual(bool(re.match(regex, f"B {v} - Text")), v >= threshold, f"B {v} >= {threshold}")

    def test_behaves_like_plain_text_without_wildcard(self):
        # 'B 3' (enthält) und 'B 3+' erfassen beide Text hinter der Zahl
        self.assertTrue(self.matches("B 3", "B 3 - Wohnungsbrand"))
        self.assertTrue(self.matches("B 3+", "B 3 - Wohnungsbrand"))

    def test_literal_plus_in_alarm_text_still_triggers(self):
        self.assertTrue(self.matches("B 3+", "B 3+ - Wohnungsbrand"))
        self.assertTrue(self.matches("B 3+", "B 4+"))
        self.assertFalse(self.matches("B 3+", "B 2+"))

    def test_case_insensitive(self):
        self.assertTrue(self.matches("b 3+", "B 7"))

    def test_with_star_the_whole_text_is_matched(self):
        self.assertTrue(self.matches("B 3+*", "B 12 - Lage"))
        self.assertTrue(self.matches("*B 3+*", "Einsatz B 12 läuft"))
        self.assertFalse(self.matches("B 3+*", "Einsatz B 12"))
        self.assertTrue(self.matches("B 3+", "Einsatz B 12"))   # ohne Stern: enthält

    def test_anchor_option(self):
        anchored = wildcard_to_regex("B 3+", anchor=True)
        self.assertTrue(re.match(anchored, "B 7"))
        self.assertFalse(re.match(anchored, "B 7 - Lage"))
        self.assertTrue(re.match(wildcard_to_regex("THL*", anchor=False), "THL Tür"))

    def test_other_characters_stay_literal(self):
        self.assertTrue(self.matches("(B 3+)", "(B 7)"))
        self.assertFalse(self.matches("(B 3+)", "B 7"))

    def test_two_numbers(self):
        self.assertTrue(self.matches("B 3+ RD 2+", "B 5 RD 4"))
        self.assertFalse(self.matches("B 3+ RD 2+", "B 5 RD 1"))

    def test_raw_regex_is_left_alone(self):
        self.assertFalse(has_range_syntax("re:^3+"))
        self.assertTrue(re.match(wildcard_to_regex("re:^3+"), "333"))
        self.assertFalse(re.match(wildcard_to_regex("re:^3+"), "4"))

    def test_detection(self):
        for text in ("B 3+", "3+", "13+x", "THL3+"):
            self.assertTrue(has_range_syntax(text), text)
            self.assertTrue(is_pattern_input(text), text)
        for text in ("B 3", "B +", "C++", "a+b", "B 3 +", "re:3+", "1234567"):
            self.assertFalse(has_range_syntax(text), text)
        self.assertTrue(is_pattern_input("THL*") and not is_pattern_input("THL"))

    def test_range_alone_is_not_an_explicit_wildcard(self):
        self.assertFalse(has_wildcard_syntax("B 3+"))


class ListTests(unittest.TestCase):
    def test_split_list_drops_empty(self):
        self.assertEqual(split_list("a, , b"), ["a", "b"])
        self.assertEqual(split_list(", "), [])
        self.assertEqual(split_list(None), [])

    def test_split_list_aligned_keeps_empty(self):
        self.assertEqual(split_list_aligned("a, , b"), ["a", "", "b"])
        self.assertEqual(split_list_aligned(""), [])

    def test_candidates_include_list_without_duplicates(self):
        values, raw = candidate_values({"ric": "2", "ric_list": "1, 2"}, "ric")
        self.assertEqual(values, ["2", "1"])
        self.assertEqual(raw, "1, 2")

    def test_candidates_without_list(self):
        self.assertEqual(candidate_values({"ric": "7"}, "ric"), (["7"], ""))
        self.assertEqual(candidate_values({}, "ric"), ([], ""))


class MatchSubTests(unittest.TestCase):
    def test_exact_in_list_only(self):
        values, raw = candidate_values({"ric": "2", "ric_list": "1, 2"}, "ric")
        self.assertEqual(match_sub(sub("1"), values, raw), "Alias")

    def test_exact_no_match(self):
        values, raw = candidate_values({"ric": "2", "ric_list": "1, 2"}, "ric")
        self.assertIsNone(match_sub(sub("3"), values, raw))

    def test_exact_is_not_substring(self):
        values, raw = candidate_values({"ric": "0230100"}, "ric")
        self.assertIsNone(match_sub(sub("301"), values, raw))

    def test_name_containing_comma_still_matches(self):
        payload = {"description": "Fahrzeug B", "description_list": "Wache Nord, Süd, Fahrzeug B"}
        values, raw = candidate_values(payload, "description")
        self.assertEqual(match_sub(sub("Wache Nord, Süd"), values, raw), "Alias")

    def test_comma_fallback_needs_whole_entries(self):
        values, raw = candidate_values({"description": "x", "description_list": "Wache Nord, Süd, x"}, "description")
        self.assertIsNone(match_sub(sub("Nord, Sü"), values, raw))

    def test_regex_expands_placeholder(self):
        s = sub(r"^23456([0-9]{2})$", True, r"Feuerwehr Musterstadt \1")
        self.assertEqual(match_sub(s, ["2345625"], ""), "Feuerwehr Musterstadt 25")

    def test_regex_alias_without_valid_template_falls_back(self):
        s = sub(".*", True, r"Filter: re:^\d")
        self.assertEqual(match_sub(s, ["x"], ""), r"Filter: re:^\d")

    def test_regex_checks_list_values(self):
        values, raw = candidate_values({"ric": "9", "ric_list": "2345625, 9"}, "ric")
        self.assertEqual(match_sub(sub(r"^23456([0-9]{2})$", True, r"W \1"), values, raw), "W 25")


if __name__ == "__main__":
    unittest.main()
