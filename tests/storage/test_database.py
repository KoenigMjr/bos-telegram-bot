import os
import tempfile
import unittest

from bos_telegram_bot.storage import database as db
from bos_telegram_bot import app as bot_app


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "bot.sqlite3")
        db.init_db(self.path)

    def test_init_is_idempotent(self):
        db.add_sub(self.path, 1, "ric", "1", "A", False)
        db.init_db(self.path)
        self.assertEqual(len(db.get_chat_subs(self.path, 1)), 1)

    def test_rename_field_migrates_subscriptions(self):
        db.add_sub(self.path, 1, "subric_text", "a", "Sub-RIC: a", False)
        db.add_sub(self.path, 2, "subric_text", "b", "Sub-RIC: b", False)
        self.assertEqual(db.rename_field(self.path, "subric_text", "subricText"), 2)
        self.assertEqual([s["field"] for s in db.get_all_subs(self.path)], ["subricText", "subricText"])

    def test_rename_field_drops_duplicates_instead_of_failing(self):
        db.add_sub(self.path, 1, "subric_text", "a", "alt", False)
        db.add_sub(self.path, 1, "subricText", "a", "neu", False)
        db.rename_field(self.path, "subric_text", "subricText")
        subs = db.get_all_subs(self.path)
        self.assertEqual([(s["field"], s["alias"]) for s in subs], [("subricText", "neu")])

    def test_rename_unknown_field_does_nothing(self):
        db.add_sub(self.path, 1, "ric", "1", "A", False)
        self.assertEqual(db.rename_field(self.path, "gibtsnicht", "x"), 0)
        self.assertEqual(len(db.get_all_subs(self.path)), 1)

    def test_legacy_rename_table_is_applied_by_main(self):
        self.assertEqual(bot_app.LEGACY_FIELD_RENAMES, {"subric_text": "subricText"})

    def test_learned_pairs_upsert_and_lookup(self):
        db.upsert_learned(self.path, "ric", "1", "Alt")
        db.upsert_learned(self.path, "ric", "1", "Neu")
        self.assertEqual(db.get_learned(self.path, "ric", "1"), "Neu")
        self.assertIsNone(db.get_learned(self.path, "ric", "2"))
        self.assertEqual(len(db.list_learned(self.path, "ric")), 1)
        self.assertEqual(db.list_learned(self.path, "anderes"), [])

    def test_remove_chat_subs_only_touches_that_chat(self):
        db.add_sub(self.path, 1, "ric", "1", "A", False)
        db.add_sub(self.path, 2, "ric", "1", "A", False)
        db.remove_chat_subs(self.path, 1)
        self.assertEqual([s["chat_id"] for s in db.get_all_subs(self.path)], [2])


if __name__ == "__main__":
    unittest.main()
