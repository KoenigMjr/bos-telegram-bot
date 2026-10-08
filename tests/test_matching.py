import re
import unittest

from matching import (candidate_values, has_wildcard_syntax, match_sub, split_list,
                      split_list_aligned, wildcard_to_regex)


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
