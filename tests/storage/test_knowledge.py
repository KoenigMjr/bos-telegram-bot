import os
import tempfile
import unittest

from bos_telegram_bot.storage import database as db
from bos_telegram_bot.storage import knowledge
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


class LoadCsvLogTests(TempDbCase):
    """Was der Bot beim Einlesen einer CSV ins Log schreibt."""
    LOGGER = "bos_telegram_bot.storage.knowledge"

    def write(self, text):
        path = os.path.join(self.tmp, "descriptions_ric.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_info_names_field_count_and_the_absolute_path(self):
        path = self.write("for,add,isRegex\n1000011,Wache Nord,false\n1000043,Lagedienst,false\n")
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            knowledge.load_csv(path, "ric")
        self.assertEqual(logged.output, [
            f"INFO:{self.LOGGER}:CSV eingelesen (Feld ric): 2 Datensätze aus {os.path.abspath(path)}"])

    def test_patterns_are_counted_separately(self):
        path = self.write("for,add,isRegex\n1000011,Wache Nord,false\n^23456([0-9]{2})$,Feuerwehr,true\n^34567.*$,Rettung,true\n")
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            knowledge.load_csv(path, "ric")
        self.assertIn("3 Datensätze (2 Muster) aus", logged.output[0])

    def test_skipped_rows_are_counted_and_each_one_is_a_warning(self):
        path = self.write("for,add,isRegex\n(,kaputt,true\n[,auch kaputt,true\n2222222,ok,false\n")
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            knowledge.load_csv(path, "ric")
        self.assertEqual(sum(1 for line in logged.output if line.startswith("WARNING")), 2)
        self.assertIn("1 Datensatz (2 übersprungen) aus", logged.output[-1])
        self.assertTrue(logged.output[-1].startswith("INFO:"))

    def test_patterns_and_skipped_rows_together(self):
        path = self.write("for,add,isRegex\n(,kaputt,true\n^23456.*$,ok,true\n")
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            knowledge.load_csv(path, "ric")
        self.assertIn("1 Datensatz (1 Muster, 1 übersprungen) aus", logged.output[-1])

    def test_singular_and_plural(self):
        for text, expected in (("for,add\n", "0 Datensätze aus"), ("for,add\n1,A\n", "1 Datensatz aus"),
                               ("for,add\n1,A\n2,B\n", "2 Datensätze aus")):
            with self.assertLogs(self.LOGGER, level="INFO") as logged:
                knowledge.load_csv(self.write(text), "ric")
            self.assertIn(expected, logged.output[0], text)

    def test_an_empty_csv_reports_zero(self):
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            knowledge.load_csv(self.write("for,add,isRegex\n"), "ric")
        self.assertIn("0 Datensätze aus", logged.output[0])

    def test_without_a_field_name_the_label_is_left_out(self):
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            knowledge.load_csv(self.write("for,add\n1,A\n"))
        self.assertTrue(logged.output[0].startswith(f"INFO:{self.LOGGER}:CSV eingelesen: 1 Datensatz aus"))

    def test_a_missing_file_is_a_warning_with_the_field(self):
        missing = os.path.join(self.tmp, "gibtsnicht.csv")
        with self.assertLogs(self.LOGGER, level="INFO") as logged:
            self.assertEqual(knowledge.load_csv(missing, "ric"), [])
        self.assertEqual(logged.output, [
            f"WARNING:{self.LOGGER}:CSV nicht gefunden (Feld ric), es werden nur gelernte Werte genutzt: {missing}"])

    def test_no_path_logs_nothing(self):
        with self.assertRaises(AssertionError):            # assertLogs schlägt fehl, wenn nichts geloggt wurde
            with self.assertLogs(self.LOGGER, level="DEBUG"):
                knowledge.load_csv(None, "ric")

    def test_the_csv_content_is_not_written_to_the_log(self):
        path = self.write("for,add,isRegex\n1000011,Geheimer Name,false\n")
        with self.assertLogs(self.LOGGER, level="DEBUG") as logged:
            knowledge.load_csv(path, "ric")
        self.assertNotIn("Geheimer Name", "\n".join(logged.output))


class UnassignedCsvTests(TempDbCase):
    """Eine CSV im Datenordner, die kein Feld verwendet, wird beim Start gemeldet statt still ignoriert."""
    LOGGER = "bos_telegram_bot.storage.knowledge"
    CONTENT = "for,add,isRegex\n1000011,Geheimer Name,false\n"

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)

    def touch(self, name, text=None, folder=None):
        path = os.path.join(folder or self.data, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.CONTENT if text is None else text)
        return path

    @staticmethod
    def entries(csv=None, extra=()):
        entries = [{"for": "ric", "add": "description"}, {"for": "message"}, *extra]
        if csv:
            entries[0]["csv"] = csv
        return entries

    def scan(self, entries, folder=None):
        """Gibt (Ergebnis, Warnungen) zurück. Ohne Warnung ist die Liste leer."""
        with self.assertLogs(self.LOGGER, level="DEBUG") as logged:
            result = knowledge.find_unassigned_csvs(folder or self.data, entries)
        return result, [line for line in logged.output if line.startswith("WARNING")]

    def test_an_empty_folder_is_fine(self):
        result, warnings = self.scan(self.entries())
        self.assertEqual((result, warnings), ([], []))

    def test_an_assigned_csv_is_not_reported(self):
        path = self.touch("descriptions_ric.csv")
        self.assertEqual(self.scan(self.entries(csv=path)), ([], []))

    def test_relative_and_absolute_paths_are_the_same_file(self):
        self.touch("descriptions_ric.csv")
        previous = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(self.scan(self.entries(csv="data/descriptions_ric.csv"), folder="data"), ([], []))

    def test_a_symlink_to_the_assigned_file_counts_as_assigned(self):
        real = self.touch("echt.csv", folder=os.path.join(self.tmp, "woanders"))
        try:
            os.symlink(real, os.path.join(self.data, "descriptions_ric.csv"))
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks nicht verfügbar")
        self.assertEqual(self.scan(self.entries(csv=real)), ([], []))

    def test_a_csv_that_nobody_uses_is_reported_with_the_way_to_assign_it(self):
        path = self.touch("wachen.csv")
        result, warnings = self.scan(self.entries())
        self.assertEqual(result, [os.path.abspath(path)])
        (warning,) = warnings
        self.assertIn(f"CSV {os.path.abspath(path)} liegt im Datenordner, wird aber von keinem Feld verwendet", warning)
        self.assertIn("nicht gelesen", warning)
        self.assertIn("'descriptions_<Feld>.csv' umbenennen (vorhanden: ric)", warning)
        self.assertIn("'csv:' eintragen oder CSV_PATH_<FELD> setzen", warning)

    def test_a_name_that_points_to_a_field_which_does_not_exist(self):
        self.touch("descriptions_stadtteil.csv")
        _, (warning,) = self.scan(self.entries())
        self.assertIn("Es gibt kein Feld 'stadtteil' mit Namenssuche (vorhanden: ric)", warning)

    def test_a_field_without_name_search_does_not_count(self):
        self.touch("descriptions_message.csv")                        # 'message' hat kein 'add'
        _, (warning,) = self.scan(self.entries())
        self.assertIn("Es gibt kein Feld 'message' mit Namenssuche (vorhanden: ric)", warning)

    def test_wrong_upper_and_lower_case_is_pointed_out(self):
        for name in ("descriptions_RIC.csv", "Descriptions_ric.csv", "DESCRIPTIONS_RIC.CSV", "descriptions_ric.CSV"):
            with self.subTest(name=name):
                folder = os.path.join(self.tmp, name)
                path = self.touch(name, folder=folder)
                _, (warning,) = self.scan(self.entries(), folder=folder)
                self.assertIn("Der Name muss genau 'descriptions_ric.csv' lauten (Kleinschreibung, Endung .csv)", warning)
                self.assertIn(os.path.abspath(path), warning)

    def test_the_case_of_the_field_itself_is_kept(self):
        self.touch("descriptions_subrictext.csv")
        entries = self.entries(extra=({"for": "subricText", "add": "x"},))
        _, (warning,) = self.scan(entries)
        self.assertIn("muss genau 'descriptions_subricText.csv' lauten", warning)

    def test_a_csv_that_is_overridden_by_another_one_is_explained(self):
        other = self.touch("andere.csv", folder=os.path.join(self.tmp, "extern"))
        self.touch("descriptions_ric.csv")                           # richtig benannt, aber ein anderer Pfad hat Vorrang
        _, (warning,) = self.scan(self.entries(csv=other))
        self.assertIn(f"Für Feld 'ric' ist stattdessen {other} eingestellt (CSV_PATH_RIC oder 'csv:'", warning)

    def test_only_csv_files_are_considered(self):
        for name in ("config.yaml", "bot_db.sqlite3", "notizen.txt", "wachen.csv.bak", "csv", "x.csvx"):
            self.touch(name, "egal")
        self.assertEqual(self.scan(self.entries()), ([], []))

    def test_subfolders_and_directories_named_like_csv_are_not_scanned(self):
        self.touch("alt.csv", folder=os.path.join(self.data, "archiv"))
        os.makedirs(os.path.join(self.data, "ordner.csv"))
        self.assertEqual(self.scan(self.entries()), ([], []))

    def test_every_unassigned_file_gets_its_own_warning_in_alphabetical_order(self):
        used = self.touch("descriptions_ric.csv")
        for name in ("b.csv", "a.csv", "c.csv"):
            self.touch(name)
        result, warnings = self.scan(self.entries(csv=used))
        self.assertEqual([os.path.basename(p) for p in result], ["a.csv", "b.csv", "c.csv"])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all("liegt im Datenordner" in w for w in warnings))

    def test_a_file_used_by_a_second_field_is_assigned(self):
        first, second = self.touch("descriptions_ric.csv"), self.touch("namen.csv")
        entries = self.entries(csv=first, extra=({"for": "stadtteil", "add": "stadtteil_name", "csv": second},))
        self.assertEqual(self.scan(entries), ([], []))

    def test_without_any_name_search_field_the_hint_says_so(self):
        self.touch("wachen.csv")
        _, (warning,) = self.scan([{"for": "message"}])
        self.assertIn("kein Feld hat eine Namenssuche ('add:')", warning)

    def test_a_missing_folder_is_not_an_error(self):
        with self.assertRaises(AssertionError):                      # nichts zu loggen
            with self.assertLogs(self.LOGGER, level="DEBUG"):
                self.assertEqual(knowledge.find_unassigned_csvs(os.path.join(self.tmp, "gibtsnicht"), self.entries()), [])

    def test_debug_confirms_a_clean_folder(self):
        self.touch("descriptions_ric.csv")
        with self.assertLogs(self.LOGGER, level="DEBUG") as logged:
            knowledge.find_unassigned_csvs(self.data, self.entries(csv=os.path.join(self.data, "descriptions_ric.csv")))
        self.assertEqual(len(logged.output), 1)
        self.assertIn("keine nicht zugeordnete CSV", logged.output[0])

    def test_the_content_of_the_file_is_never_logged(self):
        self.touch("wachen.csv")
        _, (warning,) = self.scan(self.entries())
        self.assertNotIn("Geheimer Name", warning)
        self.assertNotIn("1000011", warning)

    def test_the_file_is_neither_read_nor_changed(self):
        path = self.touch("wachen.csv")
        before = os.stat(path)
        self.scan(self.entries())
        after = os.stat(path)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))


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
