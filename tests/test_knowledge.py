import os
import tempfile
import unittest
from unittest.mock import patch

import database as db
import knowledge
from tests.helpers import make_entries

CSV_ROWS = [
    {"for": "1234567", "add": "Rettungswagen Musterstadt A-Wehr", "isRegex": False},
    {"for": r"^23456([0-9]{2})$", "add": r"Feuerwehr Musterstadt \1", "isRegex": True},
]


class TempDbCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db_path = os.path.join(tmp.name, "bot.sqlite3")
        db.init_db(self.db_path)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def entry(self, csv_rows=None, learn=True):
        entry = make_entries(csv_rows=csv_rows)[0]
        entry["learn"] = learn
        return entry


class LoadCsvTests(TempDbCase):
    def write(self, text):
        path = os.path.join(self.tmp, "x.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_loads_boswatch_format(self):
        rows = knowledge.load_csv(self.write(
            "for,add,isRegex\n1234567,Rettungswagen Musterstadt A-Wehr,false\n^23456([0-9]{2})$,Feuerwehr Musterstadt,true\n"))
        self.assertEqual(rows, [
            {"for": "1234567", "add": "Rettungswagen Musterstadt A-Wehr", "isRegex": False},
            {"for": "^23456([0-9]{2})$", "add": "Feuerwehr Musterstadt", "isRegex": True},
        ])

    def test_missing_isregex_column_and_quotes(self):
        rows = knowledge.load_csv(self.write('for,add\n"1111111",Wache\n'))
        self.assertEqual(rows, [{"for": "1111111", "add": "Wache", "isRegex": False}])

    def test_invalid_regex_row_is_skipped(self):
        rows = knowledge.load_csv(self.write("for,add,isRegex\n(,kaputt,true\n2222222,ok,false\n"))
        self.assertEqual([r["for"] for r in rows], ["2222222"])

    def test_missing_file_and_no_path(self):
        self.assertEqual(knowledge.load_csv(os.path.join(self.tmp, "gibtsnicht.csv")), [])
        self.assertEqual(knowledge.load_csv(None), [])

    def test_csv_is_never_modified(self):
        path = self.write("for,add,isRegex\n1234567,Name,false\n")

        def snapshot():
            with open(path, encoding="utf-8") as f:
                return os.path.getmtime(path), f.read()

        before = snapshot()
        knowledge.load_csv(path)
        self.assertEqual(before, snapshot())


class ResolveTests(TempDbCase):
    def test_csv_covers_prefers_exact_over_regex(self):
        rows = [{"for": r"^123.*$", "add": "Gruppe", "isRegex": True},
                {"for": "1234567", "add": "Genau", "isRegex": False}]
        self.assertEqual(knowledge.csv_covers(rows, "1234567")["add"], "Genau")
        self.assertEqual(knowledge.csv_covers(rows, "1239999")["add"], "Gruppe")
        self.assertIsNone(knowledge.csv_covers(rows, "9999999"))

    def test_resolve_name_sources(self):
        entry = self.entry(CSV_ROWS)
        db.upsert_learned(self.db_path, "ric", "5555555", "Gelernter Name")
        self.assertEqual(knowledge.resolve_name(entry, self.db_path, "1234567"), "Rettungswagen Musterstadt A-Wehr")
        self.assertEqual(knowledge.resolve_name(entry, self.db_path, "2345625"), "Feuerwehr Musterstadt 25")
        self.assertEqual(knowledge.resolve_name(entry, self.db_path, "5555555"), "Gelernter Name")
        self.assertIsNone(knowledge.resolve_name(entry, self.db_path, "9999999"))


class ExtractPairsTests(unittest.TestCase):
    def test_primary_pair(self):
        self.assertEqual(knowledge.extract_pairs({"ric": "1", "description": "A"}, "ric", "description"), [("1", "A")])

    def test_multicast_pairs_from_lists(self):
        payload = {"ric": "2", "description": "B", "ric_list": "1, 2", "description_list": "A, B"}
        self.assertEqual(knowledge.extract_pairs(payload, "ric", "description"), [("2", "B"), ("1", "A")])

    def test_lists_ignored_when_lengths_differ(self):
        payload = {"ric": "2", "description": "B", "ric_list": "1, 2", "description_list": "Wache Nord, Süd, B"}
        self.assertEqual(knowledge.extract_pairs(payload, "ric", "description"), [("2", "B")])

    def test_empty_entries_keep_alignment(self):
        payload = {"ric_list": "1, 2, 3", "description_list": "A, , C"}
        self.assertEqual(knowledge.extract_pairs(payload, "ric", "description"), [("1", "A"), ("3", "C")])

    def test_missing_description_gives_nothing(self):
        self.assertEqual(knowledge.extract_pairs({"ric": "1"}, "ric", "description"), [])
        self.assertEqual(knowledge.extract_pairs({"ric": "1", "description": ""}, "ric", "description"), [])


class LearnTests(TempDbCase):
    def learned(self):
        return {r["for_value"]: r["add_value"] for r in db.list_learned(self.db_path, "ric")}

    def test_learns_new_pairs(self):
        entry = self.entry()
        knowledge.learn_from_payload(self.db_path, [entry], {"ric": "1", "description": "A"})
        self.assertEqual(self.learned(), {"1": "A"})

    def test_learns_multicast_pairs(self):
        knowledge.learn_from_payload(self.db_path, [self.entry()], {
            "ric": "2", "description": "B", "ric_list": "1, 2", "description_list": "A, B"})
        self.assertEqual(self.learned(), {"1": "A", "2": "B"})

    def test_updates_name_when_boswatch_changes_it(self):
        entry = self.entry()
        knowledge.learn_from_payload(self.db_path, [entry], {"ric": "1", "description": "Alt"})
        knowledge.learn_from_payload(self.db_path, [entry], {"ric": "1", "description": "Neu"})
        self.assertEqual(self.learned(), {"1": "Neu"})

    def test_skips_values_covered_by_csv(self):
        entry = self.entry(CSV_ROWS)
        knowledge.learn_from_payload(self.db_path, [entry], {"ric": "1234567", "description": "Anderer Name"})
        knowledge.learn_from_payload(self.db_path, [entry], {"ric": "2345625", "description": "Feuerwehr Musterstadt 25"})
        self.assertEqual(self.learned(), {})

    def test_learn_false(self):
        knowledge.learn_from_payload(self.db_path, [self.entry(learn=False)], {"ric": "1", "description": "A"})
        self.assertEqual(self.learned(), {})

    def test_entries_without_add_are_ignored(self):
        message_entry = make_entries()[1]
        knowledge.learn_from_payload(self.db_path, [message_entry], {"message": "x", "ric": "1", "description": "A"})
        self.assertEqual(self.learned(), {})

    def test_known_rows_dedupe_learned_against_csv(self):
        entry = self.entry()                                    # zuerst ohne CSV lernen
        knowledge.learn_from_payload(self.db_path, [entry], {"ric": "1234567", "description": "Gelernt"})
        entry["csv_rows"] = CSV_ROWS                            # später kommt die CSV dazu
        rows = knowledge.known_rows(self.db_path, entry)
        self.assertEqual([r["add"] for r in rows if r["for"] == "1234567"], ["Rettungswagen Musterstadt A-Wehr"])
        self.assertEqual({r["source"] for r in rows}, {"csv"})


if __name__ == "__main__":
    unittest.main()
