import asyncio
import os
import re

import yaml
from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

import database as db
import handlers
from services.mqtt_service import start_mqtt_listener

# load_dotenv() ist ein No-Op, wenn keine .env-Datei existiert - schadet also
# nicht, wenn Docker/systemd die Variablen bereits direkt in die Umgebung
# injiziert haben (env_file: / EnvironmentFile=). Beide Wege landen am Ende
# gleich in os.environ, das Programm merkt keinen Unterschied.
load_dotenv()


def sanitize_command_name(name: str) -> str:
    """Telegram-Befehle erlauben nur a-z, 0-9 und _ (max. 32 Zeichen,
    kein führendes Digit)."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()
    if not name or name[0].isdigit():
        name = "f_" + name
    return name[:32]


def load_config() -> dict:
    """Lädt config.yaml (strukturelle Feld-Definitionen, selten geändert)
    und überschreibt deployment-spezifische Werte (Hosts, Pfade, Zugangsdaten)
    aus Umgebungsvariablen, falls gesetzt.

    Leere Variablen (z.B. aus ${VAR:-} im docker-compose.yml) zählen als
    "nicht gesetzt" und lassen den Wert aus config.yaml unangetastet.

    Damit muss für reine Deployment-Änderungen (anderer MQTT-Host, anderer
    CSV-Pfad im Container/Service) niemals config.yaml angefasst werden -
    einfach die Env-Var setzen und neu starten.
    """
    config_path = os.getenv("CONFIG_PATH") or "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    mqtt = config["mqtt"]
    mqtt["host"] = os.getenv("MQTT_HOST") or mqtt.get("host")
    if os.getenv("MQTT_PORT"):
        mqtt["port"] = int(os.getenv("MQTT_PORT"))
    mqtt["username"] = os.getenv("MQTT_USERNAME") or mqtt.get("username")
    mqtt["password"] = os.getenv("MQTT_PASSWORD") or mqtt.get("password")
    mqtt["topic"] = os.getenv("MQTT_TOPIC") or mqtt.get("topic")

    if os.getenv("DB_PATH"):
        config["files"]["db_path"] = os.getenv("DB_PATH")

    # Pro Lookup-Feld: CSV_PATH_<FELDNAME> überschreibt csv_path, z.B.
    # CSV_PATH_RIC=/boswatch3-config/descriptions_ric.csv
    for field_key, field_cfg in config["fields"].items():
        if field_cfg.get("mode") != "lookup":
            continue
        env_name = f"CSV_PATH_{sanitize_command_name(field_key).upper()}"
        override = os.getenv(env_name)
        if override:
            field_cfg["csv_path"] = override

    return config


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


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN fehlt (Umgebungsvariable oder .env prüfen)")

ADMIN_USERS = parse_admin_users(os.getenv("ADMIN_USERS", ""))

config = load_config()
DB_PATH = config["files"]["db_path"]
FIELDS_CFG = config["fields"]

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db.init_db(DB_PATH)


def load_all_csvs(fields_cfg: dict) -> dict:
    """Lädt für jedes lookup-Feld die konfigurierte CSV. Mehrere Felder mit
    demselben csv_path (z.B. /ric und /description) teilen sich dieselbe
    eingelesene Liste statt die Datei mehrfach zu parsen."""
    path_cache = {}
    csv_data_by_field = {}
    for field_key, field_cfg in fields_cfg.items():
        if field_cfg.get("mode") != "lookup":
            continue
        csv_path = field_cfg["csv_path"]
        if csv_path not in path_cache:
            handlers.ensure_csv_exists(csv_path)
            path_cache[csv_path] = handlers.load_csv(csv_path)
        csv_data_by_field[field_key] = path_cache[csv_path]
    return csv_data_by_field


async def post_init(app):
    field_commands = app.bot_data["field_commands"]
    bot_commands = [
        BotCommand("start", "Hilfe & Übersicht"),
        BotCommand("abo", "Abonnements anzeigen/verwalten"),
        BotCommand("lastraw", "Letztes Alarm-JSON anzeigen"),
    ]
    for field_key, field_cfg in FIELDS_CFG.items():
        cmd = field_commands[field_key]
        verb = "abonnieren" if field_cfg["mode"] == "lookup" else "filtern"
        bot_commands.append(BotCommand(cmd, f"{field_cfg['label']} {verb}"))

    await app.bot.set_my_commands(bot_commands)

    # Admins sehen zusätzlich die Benutzerverwaltung im Befehlsmenü. Das klappt
    # nur, wenn der Admin dem Bot schon einmal geschrieben hat - sonst wird es
    # übersprungen (die Befehle funktionieren trotzdem).
    admin_commands = bot_commands + [
        BotCommand("users", "Benutzer verwalten"),
        BotCommand("adduser", "User per ID freischalten"),
    ]
    for admin_id in app.bot_data["admins"]:
        try:
            await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            print(f"[Setup] Admin-Befehlsmenü für {admin_id} nicht gesetzt: {e}")
    asyncio.create_task(start_mqtt_listener(app))


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.bot_data["config"] = config
    app.bot_data["admins"] = ADMIN_USERS
    app.bot_data["fields"] = FIELDS_CFG
    app.bot_data["csv_data"] = load_all_csvs(FIELDS_CFG)
    app.bot_data["last_payload"] = None

    if not ADMIN_USERS:
        hint = ""
        if os.getenv("ALLOWED_USERS"):
            hint = " Hinweis: ALLOWED_USERS wurde in ADMIN_USERS umbenannt, bitte die Variable umbenennen."
        raise SystemExit(
            "ADMIN_USERS ist leer. Mindestens ein Admin (Telegram-User-ID) wird benötigt, "
            "alle weiteren User werden per Bot freigeschaltet (/users, /adduser)." + hint
        )

    # Befehlsnamen normalisieren + Kollisionen erkennen, bevor irgendwas
    # registriert wird.
    field_commands = {}
    used_names = {"start", "abo", "lastraw", "users", "adduser"}
    for field_key in FIELDS_CFG:
        cmd = sanitize_command_name(field_key)
        if cmd in used_names:
            raise SystemExit(
                f"Befehlsname-Kollision: Feld '{field_key}' -> /{cmd} ist bereits vergeben. "
                f"Feld-Schlüssel in config.yaml anpassen."
            )
        used_names.add(cmd)
        field_commands[field_key] = cmd
    app.bot_data["field_commands"] = field_commands

    app.add_handler(CommandHandler("start", handlers.start_handler))
    app.add_handler(CommandHandler("abo", handlers.abo_handler))
    app.add_handler(CommandHandler("lastraw", handlers.lastraw_handler))
    app.add_handler(CommandHandler("users", handlers.users_handler))
    app.add_handler(CommandHandler("adduser", handlers.adduser_handler))
    app.add_handler(CallbackQueryHandler(handlers.button_handler))

    for field_key, field_cfg in FIELDS_CFG.items():
        cmd = field_commands[field_key]
        if field_cfg["mode"] == "lookup":
            handler_func = handlers.make_lookup_handler(field_key, field_cfg)
        elif field_cfg["mode"] == "pattern":
            handler_func = handlers.make_pattern_handler(field_key, field_cfg)
        else:
            raise SystemExit(f"Unbekannter mode '{field_cfg['mode']}' bei Feld '{field_key}'")
        app.add_handler(CommandHandler(cmd, handler_func))
        print(f"[Setup] /{cmd} -> Feld '{field_key}' ({field_cfg['mode']})")

    total_csv_rows = sum(len(v) for v in {id(v): v for v in app.bot_data["csv_data"].values()}.values())
    print(f"🤖 BOS-Telegram-Bot gestartet... ({total_csv_rows} CSV-Einträge über alle Lookup-Felder geladen)")
    app.run_polling()
