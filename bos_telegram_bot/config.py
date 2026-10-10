"""Konfiguration des Bots.

Reihenfolge (später gewinnt):
  1. mitgelieferte config.yaml (Standardwerte, im Image, immer aktuell)
  2. optionale eigene Datei (CONFIG_PATH oder data/config.yaml) mit nur den Abweichungen
  3. Umgebungsvariablen (MQTT_*, DB_PATH, CSV_PATH_<FELD>, EXTRA_FIELDS, LOG_LEVEL)
"""

import logging
import os
import re

import yaml

from bos_telegram_bot.core.delivery import DEFAULT_SECONDS as DEFAULT_REMEMBER_SECONDS
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE, FIELD_NAME, template_from_fields, validate_template
from bos_telegram_bot.logs import LEVELS, normalize_level

log = logging.getLogger(__name__)

# Die mitgelieferte config.yaml liegt im Hauptordner (im Image bzw. Repo), eine Ebene über dem Paket.
BUNDLED_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
# Der Datenordner (im Container /app/data): Datenbank, eigene config.yaml und CSV-Dateien.
DATA_DIR = "data"
OPTIONAL_CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")

RESERVED_COMMANDS = {"start", "abo", "lastraw", "users", "adduser"}
MATCH_MODES = ("exact", "contains")


def sanitize_command_name(name: str) -> str:
    """Telegram-Befehle erlauben nur a-z, 0-9 und _ (max. 32 Zeichen,
    kein führendes Digit)."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", str(name)).lower()
    if not name or name[0].isdigit():
        name = "f_" + name
    return name[:32]


def parse_admin_users(raw: str) -> list:
    """Liest die Admin-Liste aus ADMIN_USERS. Trenner: Komma, Semikolon,
    Leerzeichen oder Zeilenumbruch (beliebig gemischt). Doppelte IDs werden
    entfernt. Ungültige Einträge brechen den Start mit einer klaren Meldung
    ab, statt mit einem Traceback oder - schlimmer - still ignoriert zu werden."""
    users = []
    for entry in re.split(r"[,;\s]+", raw.strip()):
        if not entry:
            continue
        try:
            user_id = int(entry)
        except ValueError:
            raise SystemExit(
                f"ADMIN_USERS enthält einen ungültigen Eintrag: '{entry}' "
                f"(erwartet: numerische Telegram-User-IDs, z.B. 123456789,987654321)"
            )
        if user_id not in users:
            users.append(user_id)
    return users


def read_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SystemExit(f"{path} ist kein gültiges YAML: {e}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: erwartet wird eine Zuordnung (Schlüssel: Wert), keine Liste oder Einzelwert.")
    return data


def deep_merge(base: dict, override: dict) -> dict:
    """Legt 'override' über 'base'. Verschachtelte Blöcke werden zusammengeführt,
    sonstige Werte ersetzt. Ein Eintrag mit dem Wert null entfernt den
    Standardeintrag."""
    result = dict(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            # Auch neue Blöcke durchlaufen die Zusammenführung, damit ein 'null' darin nichts hinterlässt.
            result[key] = deep_merge(result[key] if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = value
    return result


def merge_fields(base, override, source: str) -> list:
    """Führt die Feldlisten zusammen. Einträge werden über 'for' zugeordnet:
      - gleiches 'for'  -> Angaben ergänzen/ändern den Standardeintrag
                           (ein Wert null entfernt die Angabe)
      - neues 'for'     -> Eintrag wird angehängt
      - 'remove: true'  -> Standardeintrag wird entfernt"""
    result = [dict(entry) for entry in (base or [])]
    if override is None:
        return result

    if isinstance(override, dict):
        raise SystemExit(
            f"{source}: 'fields' hat ein neues Format. Statt einer Zuordnung wird eine Liste "
            f"erwartet, z.B.\n  fields:\n    - for: ric\n      csv: /pfad/zur/descriptions_ric.csv\n"
            f"Die alte Datei bitte anpassen oder löschen (siehe README, 'Konfiguration anpassen')."
        )
    if not isinstance(override, list):
        raise SystemExit(f"{source}: 'fields' muss eine Liste sein.")

    for item in override:
        if not isinstance(item, dict) or not item.get("for"):
            raise SystemExit(f"{source}: jeder Eintrag unter 'fields' braucht ein 'for:' (Name des JSON-Feldes).")
        key = str(item["for"])
        idx = next((i for i, e in enumerate(result) if str(e.get("for")) == key), None)

        if item.get("remove") is True:
            if idx is not None:
                result.pop(idx)
            continue

        changes = {k: v for k, v in item.items() if k != "remove"}
        if idx is None:
            result.append({k: v for k, v in changes.items() if v is not None})
        else:
            merged = dict(result[idx])
            for k, v in changes.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            result[idx] = merged
    return result


def find_override_path():
    """CONFIG_PATH (muss existieren) oder, falls vorhanden, data/config.yaml.
    Es wird nichts angelegt oder kopiert."""
    explicit = os.getenv("CONFIG_PATH")
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit(f"CONFIG_PATH zeigt auf eine nicht vorhandene Datei: {explicit}")
        return explicit
    if os.path.isfile(OPTIONAL_CONFIG_PATH):
        return OPTIONAL_CONFIG_PATH
    return None


def check_config_sections(config: dict, path: str) -> None:
    missing = [s for s in ("mqtt", "files") if not isinstance(config.get(s), dict)]
    if not isinstance(config.get("fields"), list):
        missing.append("fields")
    if missing:
        raise SystemExit(f"{path}: Abschnitt fehlt oder ist leer: {', '.join(missing)}")


def validate_config(config: dict, path: str) -> None:
    """Prüft die fertig zusammengesetzte Konfiguration und nennt alle Probleme
    auf einmal, statt mit einem KeyError abzubrechen."""
    problems = []
    log_block = config.get("logging")
    if "logging" in config and not isinstance(log_block, dict):
        problems.append("logging muss ein Block sein, z.B. 'logging:' mit 'level: INFO' darunter")
    elif isinstance(log_block, dict) and "level" in log_block and normalize_level(log_block["level"]) is None:
        problems.append(f"logging.level muss eines von {', '.join(LEVELS)} sein (ist '{log_block['level']}')")
    duplicates = config.get("duplicates")
    if "duplicates" in config and not isinstance(duplicates, dict):
        problems.append("duplicates muss ein Block sein, z.B. 'duplicates:' mit 'remember_seconds: 300' darunter")
    elif isinstance(duplicates, dict) and "remember_seconds" in duplicates:
        seconds = duplicates["remember_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            problems.append(f"duplicates.remember_seconds muss eine Zahl von 0 an sein (0 = abgeschaltet), ist '{seconds}'")
    if not config["files"].get("db_path"):
        problems.append("files.db_path fehlt")
    if not config["mqtt"].get("host"):
        problems.append("mqtt.host fehlt (oder Umgebungsvariable MQTT_HOST setzen)")
    block = config.get("notification")
    if "notification" in config and not isinstance(block, dict):
        problems.append("notification muss ein Block sein, z.B. 'notification:' mit 'template: |' darunter")
    elif isinstance(block, dict) and "template" in block:
        problems += [f"notification.template: {p}" for p in validate_template(block["template"])]
    if "notification_fields" in config:
        fields = config["notification_fields"]
        if not isinstance(fields, list):
            problems.append("notification_fields muss eine Liste sein")
        else:
            problems += [f"notification_fields: '{f}' ist kein gültiger Feldname (Buchstaben, Ziffern und _)"
                         for f in fields if not isinstance(f, str) or not FIELD_NAME.match(f)]
    if not config["fields"]:
        problems.append("fields ist leer, es gäbe keine Befehle zum Abonnieren")

    seen = set()
    for entry in config["fields"]:
        name = entry.get("for")
        where = f"fields[for: {name}]"
        if not name:
            problems.append("fields: ein Eintrag hat kein 'for'")
            continue
        if str(name) in seen:
            problems.append(f"{where}: 'for' kommt mehrfach vor")
        seen.add(str(name))

        match = entry.get("match")
        if match is not None and match not in MATCH_MODES:
            problems.append(f"{where}: match muss 'exact' oder 'contains' sein (ist '{match}')")
        if "learn" in entry and not isinstance(entry["learn"], bool):
            problems.append(f"{where}: learn muss true oder false sein")
        if entry.get("csv") and not entry.get("add"):
            problems.append(f"{where}: 'csv' braucht ein 'add' (Name des Feldes, das die CSV ergänzt)")
        if entry.get("add") and str(entry["add"]) == str(name):
            problems.append(f"{where}: 'add' darf nicht dasselbe Feld wie 'for' sein")

    if problems:
        raise SystemExit(f"Fehler in {path}:\n  - " + "\n  - ".join(problems))


def normalize_entries(config: dict) -> None:
    """Ergänzt Standardwerte, damit der Rest des Programms nichts mehr prüfen muss."""
    for entry in config["fields"]:
        entry["for"] = str(entry["for"])
        entry.setdefault("label", entry["for"])
        entry["match"] = entry.get("match") or "contains"
        entry["learn"] = entry.get("learn", True)
        entry["command"] = sanitize_command_name(entry.get("command") or entry["for"])
        if entry.get("add"):
            entry["add"] = str(entry["add"])
            entry.setdefault("add_label", entry["add"])
            entry["add_command"] = sanitize_command_name(entry.get("add_command") or entry["add"])


def resolve_csv_paths(config: dict) -> None:
    """Bestimmt pro Eintrag mit 'add' die CSV: Umgebungsvariable
    CSV_PATH_<FELD>, sonst die Angabe 'csv:', sonst automatisch
    data/descriptions_<for>.csv, falls diese Datei existiert."""
    for entry in config["fields"]:
        if not entry.get("add"):
            continue
        for name in (entry["for"], entry["add"]):
            override = os.getenv(f"CSV_PATH_{sanitize_command_name(name).upper()}")
            if override:
                entry["csv"] = override
                break
        else:
            if not entry.get("csv"):
                auto = os.path.join(DATA_DIR, f"descriptions_{entry['for']}.csv")
                if os.path.isfile(auto):
                    entry["csv"] = auto


def load_config() -> dict:
    config = read_yaml(BUNDLED_CONFIG)
    base_fields = config.pop("fields", [])

    override_path = find_override_path()
    override_fields = None
    if override_path:
        override = read_yaml(override_path)
        override_fields = override.pop("fields", None)
        config = deep_merge(config, override)
        log.info("Eigene Anpassungen geladen: %s", override_path)
    source = override_path or BUNDLED_CONFIG

    config["fields"] = merge_fields(base_fields, override_fields, source)
    check_config_sections(config, source)

    # Umgebungsvariablen haben Vorrang. Leere Werte (z.B. aus ${VAR:-} im
    # docker-compose.yml) zählen als "nicht gesetzt".
    mqtt = config["mqtt"]
    mqtt["host"] = os.getenv("MQTT_HOST") or mqtt.get("host")
    if os.getenv("MQTT_PORT"):
        mqtt["port"] = int(os.getenv("MQTT_PORT"))
    mqtt["username"] = os.getenv("MQTT_USERNAME") or mqtt.get("username")
    mqtt["password"] = os.getenv("MQTT_PASSWORD") or mqtt.get("password")
    mqtt["topic"] = os.getenv("MQTT_TOPIC") or mqtt.get("topic")
    if os.getenv("DB_PATH"):
        config["files"]["db_path"] = os.getenv("DB_PATH")
    if os.getenv("LOG_LEVEL"):
        if not isinstance(config.get("logging"), dict):
            config["logging"] = {}
        config["logging"]["level"] = os.getenv("LOG_LEVEL")

    # EXTRA_FIELDS=feld1,feld2 legt für jedes Feld aus dem MQTT-JSON einen Befehl
    # an (z.B. Felder, die das BOSWatch3-Modul 'descriptor' ergänzt).
    known = {str(e.get("for")) for e in config["fields"]} | {str(e["add"]) for e in config["fields"] if e.get("add")}
    for name in re.split(r"[,;\s]+", os.getenv("EXTRA_FIELDS", "").strip()):
        if name and name not in known:
            config["fields"].append({"for": name})
            known.add(name)

    validate_config(config, source)
    log_settings = config.setdefault("logging", {})
    log_settings["level"] = normalize_level(log_settings.get("level", "INFO"))
    duplicate_settings = config.setdefault("duplicates", {})
    duplicate_settings.setdefault("remember_seconds", DEFAULT_REMEMBER_SECONDS)
    normalize_entries(config)
    resolve_csv_paths(config)
    return config


def notification_template(config: dict) -> str:
    """Die Vorlage der Alarm-Nachricht: 'notification.template', sonst - wie in früheren
    Versionen - aus 'notification_fields' gebildet, sonst die Standard-Vorlage."""
    block = config.get("notification")
    if isinstance(block, dict) and block.get("template"):
        return str(block["template"])
    if config.get("notification_fields"):
        return template_from_fields(config["notification_fields"])
    return DEFAULT_TEMPLATE


def build_command_map(entries: list) -> list:
    """Liste aller Telegram-Befehle: {'name', 'kind' ('for'|'add'), 'entry'}.
    Bricht mit klarer Meldung ab, wenn zwei Felder denselben Befehl ergeben."""
    commands, used = [], {}

    def register(name: str, kind: str, entry: dict) -> None:
        if name in RESERVED_COMMANDS or name in used:
            other = "einen eingebauten Befehl" if name in RESERVED_COMMANDS else f"das Feld '{used[name]}'"
            raise SystemExit(
                f"Befehlsname-Kollision: /{name} (Feld '{entry['for']}') ist bereits durch {other} belegt. "
                f"Mit 'command:' (bzw. 'add_command:') einen anderen Namen vergeben."
            )
        used[name] = entry["for"]
        commands.append({"name": name, "kind": kind, "entry": entry})

    for entry in entries:
        register(entry["command"], "for", entry)
        if entry.get("add"):
            register(entry["add_command"], "add", entry)
    return commands
