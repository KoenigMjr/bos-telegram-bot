import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from bos_telegram_bot import config as cfg
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE, template_from_fields, validate_template


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class ConfigTestCase(unittest.TestCase):
    """Läuft in einem leeren Arbeitsverzeichnis mit sauberer Umgebung."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous)
        self.tmp = tmp.name
        patcher = patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def load(self, override=None, **env):
        if override is not None:
            write(os.path.join("data", "config.yaml"), override)
        os.environ.update(env)
        return cfg.load_config()

    def expect_exit(self, override=None, **env):
        with self.assertRaises(SystemExit) as ctx:
            self.load(override, **env)
        return str(ctx.exception)

    @staticmethod
    def entry(config, key):
        return next(e for e in config["fields"] if e["for"] == key)


class DefaultsTests(ConfigTestCase):
    def test_defaults_without_any_file(self):
        config = self.load()
        self.assertEqual([e["for"] for e in config["fields"]], ["ric", "message", "subricText"])
        ric = self.entry(config, "ric")
        self.assertEqual((ric["match"], ric["add"], ric["command"], ric["add_command"]),
                         ("exact", "description", "ric", "description"))
        self.assertEqual(self.entry(config, "message")["match"], "contains")
        self.assertEqual(self.entry(config, "subricText")["command"], "subrictext")
        self.assertTrue(all(e["learn"] for e in config["fields"]))
        self.assertFalse(os.path.exists(os.path.join("data", "config.yaml")))

    def test_command_map(self):
        names = [c["name"] for c in cfg.build_command_map(self.load()["fields"])]
        self.assertEqual(names, ["ric", "description", "message", "subrictext"])


class MergeTests(ConfigTestCase):
    def test_add_csv_to_existing_entry_keeps_rest(self):
        config = self.load("fields:\n  - for: ric\n    csv: /x/descriptions_ric.csv\n")
        ric = self.entry(config, "ric")
        self.assertEqual(ric["csv"], "/x/descriptions_ric.csv")
        self.assertEqual((ric["match"], ric["add"]), ("exact", "description"))
        self.assertEqual(len(config["fields"]), 3)

    def test_new_entry_is_appended(self):
        config = self.load("fields:\n  - for: stadtteil\n    label: Stadtteil\n")
        self.assertEqual(config["fields"][-1]["for"], "stadtteil")
        self.assertEqual(config["fields"][-1]["label"], "Stadtteil")

    def test_remove_true(self):
        config = self.load("fields:\n  - for: subricText\n    remove: true\n")
        self.assertEqual([e["for"] for e in config["fields"]], ["ric", "message"])

    def test_remove_unknown_is_ignored(self):
        self.assertEqual(len(self.load("fields:\n  - for: gibtsnicht\n    remove: true\n")["fields"]), 3)

    def test_null_removes_a_setting(self):
        config = self.load("fields:\n  - for: ric\n    add: null\n")
        self.assertNotIn("add", self.entry(config, "ric"))
        self.assertEqual([c["name"] for c in cfg.build_command_map(config["fields"])],
                         ["ric", "message", "subrictext"])

    def test_other_sections_are_merged(self):
        config = self.load('mqtt:\n  topic: "mein/topic"\n')
        self.assertEqual(config["mqtt"]["topic"], "mein/topic")
        self.assertEqual(config["mqtt"]["port"], 1883)

    def test_empty_override_file_changes_nothing(self):
        self.assertEqual(len(self.load("")["fields"]), 3)

    def test_old_dict_format_gives_helpful_error(self):
        message = self.expect_exit("fields:\n  ric:\n    mode: lookup\n")
        self.assertIn("neues Format", message)
        self.assertIn("for: ric", message)

    def test_entry_without_for_is_rejected(self):
        self.assertIn("'for:'", self.expect_exit("fields:\n  - label: X\n"))

    def test_config_path_must_exist(self):
        self.assertIn("nicht vorhandene Datei", self.expect_exit(CONFIG_PATH="/gibt/es/nicht.yaml"))

    def test_config_path_is_used(self):
        write("woanders.yaml", 'mqtt:\n  topic: "x/y"\n')
        self.assertEqual(self.load(CONFIG_PATH="woanders.yaml")["mqtt"]["topic"], "x/y")

    def test_broken_yaml(self):
        self.assertIn("kein gültiges YAML", self.expect_exit("mqtt: [unclosed\n  x: {"))

    def test_list_instead_of_mapping(self):
        self.assertIn("Zuordnung", self.expect_exit("- a\n- b\n"))


class EnvironmentTests(ConfigTestCase):
    def test_mqtt_overrides_and_empty_values_are_ignored(self):
        config = self.load(MQTT_HOST="broker.local", MQTT_PORT="8883", MQTT_TOPIC="", MQTT_USERNAME="")
        self.assertEqual(config["mqtt"]["host"], "broker.local")
        self.assertEqual(config["mqtt"]["port"], 8883)
        self.assertEqual(config["mqtt"]["topic"], "homeassistant/boswatch/alarm/+")

    def test_env_beats_override_file(self):
        config = self.load('mqtt:\n  topic: "aus/datei"\n', MQTT_TOPIC="aus/env")
        self.assertEqual(config["mqtt"]["topic"], "aus/env")

    def test_db_path(self):
        self.assertEqual(self.load(DB_PATH="/x/y.sqlite3")["files"]["db_path"], "/x/y.sqlite3")

    def test_extra_fields(self):
        config = self.load(EXTRA_FIELDS="stadtteil, objekt;ort")
        self.assertEqual([e["for"] for e in config["fields"]][-3:], ["stadtteil", "objekt", "ort"])
        self.assertEqual(self.entry(config, "objekt")["label"], "objekt")

    def test_extra_fields_do_not_duplicate_existing(self):
        config = self.load(EXTRA_FIELDS="message,description,ric")
        self.assertEqual(len(config["fields"]), 3)

    def test_csv_path_from_env(self):
        self.assertEqual(self.entry(self.load(CSV_PATH_RIC="/x/a.csv"), "ric")["csv"], "/x/a.csv")

    def test_csv_path_from_env_via_add_name(self):
        self.assertEqual(self.entry(self.load(CSV_PATH_DESCRIPTION="/x/b.csv"), "ric")["csv"], "/x/b.csv")

    def test_csv_autodetected_in_data_folder(self):
        write(os.path.join("data", "descriptions_ric.csv"), "for,add,isRegex\n")
        self.assertEqual(self.entry(self.load(), "ric")["csv"], os.path.join("data", "descriptions_ric.csv"))

    def test_no_csv_without_file(self):
        self.assertNotIn("csv", self.entry(self.load(), "ric"))


class ValidationTests(ConfigTestCase):
    def test_all_problems_reported_at_once(self):
        message = self.expect_exit(
            "fields:\n"
            "  - for: ric\n    match: zufall\n    learn: vielleicht\n"
            "  - for: x\n    csv: a.csv\n"
            "  - for: y\n    add: y\n")
        for expected in ("match muss", "learn muss", "'csv' braucht ein 'add'", "nicht dasselbe Feld"):
            self.assertIn(expected, message)

    def test_missing_host(self):
        self.assertIn("mqtt.host", self.expect_exit('mqtt:\n  host: ""\n'))

    def test_notification_fields_must_be_list(self):
        self.assertIn("notification_fields", self.expect_exit("notification_fields: x\n"))


class NotificationTemplateTests(ConfigTestCase):
    def template(self, override=None, **env):
        return cfg.notification_template(self.load(override, **env))

    def test_default_when_nothing_is_configured(self):
        self.assertEqual(self.template(), DEFAULT_TEMPLATE)

    def test_custom_template_from_data_config(self):
        self.assertEqual(self.template("notification:\n  template: |\n    <b>{MESSAGE}</b>\n    {MATCHED}\n"),
                         "<b>{MESSAGE}</b>\n{MATCHED}\n")

    def test_old_notification_fields_still_work(self):
        template = self.template('notification_fields: ["description", "ric"]\n')
        self.assertEqual(template, template_from_fields(["description", "ric"]))
        self.assertIn("{DESCRIPTION_LIST}\n{RIC_LIST}", template)

    def test_template_wins_over_notification_fields(self):
        template = self.template('notification_fields: ["ric"]\nnotification:\n  template: "{MESSAGE}"\n')
        self.assertEqual(template, "{MESSAGE}")

    def test_null_removes_a_custom_template(self):
        self.assertEqual(self.template("notification:\n  template: null\n"), DEFAULT_TEMPLATE)

    def test_rendered_example_override_from_the_repo(self):
        os.makedirs("data", exist_ok=True)
        shutil.copy(os.path.join(os.path.dirname(cfg.BUNDLED_CONFIG), "examples", "config.override.example.yaml"),
                    os.path.join("data", "config.yaml"))
        template = cfg.notification_template(cfg.load_config())
        self.assertIn("{DESCRIPTION_LIST|RIC_LIST}", template)
        self.assertEqual(validate_template(template), [])

    def test_unquoted_placeholders_give_a_helpful_error(self):
        message = self.expect_exit("notification:\n  template: {MESSAGE}\n")
        self.assertIn("notification.template", message)
        self.assertIn("'|'", message)

    def test_invalid_template_is_reported_with_all_problems(self):
        message = self.expect_exit("notification:\n  template: |\n    <b>{MESSAGE\n    Alarm & Einsatz\n")
        for expected in ("notification.template", "außerhalb eines Platzhalters", "&amp;", "nicht geschlossen"):
            self.assertIn(expected, message)

    def test_empty_template(self):
        self.assertIn("leer", self.expect_exit('notification:\n  template: "  "\n'))

    def test_notification_must_be_a_block(self):
        self.assertIn("notification muss ein Block sein", self.expect_exit("notification: text\n"))

    def test_invalid_field_name_in_notification_fields(self):
        self.assertIn("kein gültiger Feldname", self.expect_exit('notification_fields: ["ric", "sub-ric"]\n'))

    def test_the_documented_default_in_config_yaml_matches_the_code(self):
        with open(cfg.BUNDLED_CONFIG, encoding="utf-8") as f:
            text = f.read()
        documented = "\n".join(line[len("#       "):] if line.startswith("#       ") else ""
                               for line in text.splitlines()[text.splitlines().index("#     template: |") + 1:][:6])
        self.assertEqual(documented.strip(), DEFAULT_TEMPLATE)


class NullInNewBlockTests(ConfigTestCase):
    def test_null_inside_a_block_that_has_no_default_leaves_nothing_behind(self):
        config = self.load("something_new:\n  a: 1\n  b: null\n")
        self.assertEqual(config["something_new"], {"a": 1})

    def test_null_removes_a_whole_block(self):
        self.assertNotIn("mqtt", cfg.deep_merge({"mqtt": {"a": 1}}, {"mqtt": None}))


class CommandMapTests(ConfigTestCase):
    def test_collision_with_builtin_command(self):
        self.assertIn("Befehlsname-Kollision", self._collision("fields:\n  - for: start\n"))

    def _collision(self, override):
        config = self.load(override)
        with self.assertRaises(SystemExit) as ctx:
            cfg.build_command_map(config["fields"])
        return str(ctx.exception)

    def test_collision_between_fields(self):
        message = self._collision("fields:\n  - for: Ric2\n    command: ric\n")
        self.assertIn("/ric", message)

    def test_command_option_resolves_collision(self):
        config = self.load("fields:\n  - for: start\n    command: startwert\n")
        self.assertIn("startwert", [c["name"] for c in cfg.build_command_map(config["fields"])])


class HelperTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(cfg.sanitize_command_name("subricText"), "subrictext")
        self.assertEqual(cfg.sanitize_command_name("Mein-Feld.1"), "mein_feld_1")
        self.assertEqual(cfg.sanitize_command_name("1abc"), "f_1abc")
        self.assertEqual(len(cfg.sanitize_command_name("x" * 50)), 32)

    def test_parse_admin_users(self):
        for raw, expected in [("1,2", [1, 2]), ("1, 2", [1, 2]), ("1;2;3", [1, 2, 3]), ("1\n2", [1, 2]),
                              ("5,5,6", [5, 6]), ("", []), ("  ,  ", []), ("-1001,42", [-1001, 42])]:
            self.assertEqual(cfg.parse_admin_users(raw), expected, raw)

    def test_parse_admin_users_invalid(self):
        with self.assertRaises(SystemExit) as ctx:
            cfg.parse_admin_users("1,abc")
        self.assertIn("'abc'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
