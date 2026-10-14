"""Verdrahtung: liest die Konfiguration, richtet Datenbank und Telegram-Befehle ein und startet den Bot."""
import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bos_telegram_bot import config as cfg
from bos_telegram_bot import logs
from bos_telegram_bot.chat import admin, buttons, fields, overview, prompts, templates
from bos_telegram_bot.chat.sender import SHUTDOWN_SECONDS, Sender
from bos_telegram_bot.core.delivery import DeliveryMemory
from bos_telegram_bot.mqtt.listener import start_mqtt_listener
from bos_telegram_bot.storage import database as db
from bos_telegram_bot.storage import knowledge

log = logging.getLogger(__name__)

# load_dotenv() ist ein No-Op, wenn keine .env-Datei existiert. Docker und
# andere Umgebungen setzen die Variablen direkt, beides landet in os.environ.
load_dotenv()

# Feldschlüssel früherer Versionen -> aktueller Name (bestehende Abos werden
# beim Start umgeschrieben).
LEGACY_FIELD_RENAMES = {"subric_text": "subricText"}


def build_menu(command_map: list) -> list:
    """Befehlsliste für das Telegram-Menü (erscheint beim Tippen von '/')."""
    menu = [
        BotCommand("start", "Hilfe & Übersicht"),
        BotCommand("abo", "Abonnements anzeigen/verwalten"),
        BotCommand("lastraw", "Letztes Alarm-JSON anzeigen"),
        BotCommand("template", "Alarm-Nachricht anpassen"),
    ]
    for cmd in command_map:
        entry = cmd["entry"]
        if cmd["kind"] == "for":
            description = f"{entry['label']} abonnieren/filtern"
        else:
            description = f"{entry['add_label']} suchen"
        menu.append(BotCommand(cmd["name"], description[:256]))
    return menu


async def post_init(app):
    menu = build_menu(app.bot_data["command_map"])
    await app.bot.set_my_commands(menu)

    # Admins sehen zusätzlich die Benutzerverwaltung im Befehlsmenü. Das klappt
    # nur, wenn der Admin dem Bot schon einmal geschrieben hat - sonst wird es
    # übersprungen (die Befehle funktionieren trotzdem).
    admin_menu = menu + [
        BotCommand("users", "Benutzer verwalten"),
        BotCommand("adduser", "User per ID freischalten"),
    ]
    for admin_id in app.bot_data["admins"]:
        try:
            await app.bot.set_my_commands(admin_menu, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            log.info("Admin-Befehlsmenü für %s nicht gesetzt (hat der Admin dem Bot schon geschrieben?): %s", admin_id, e)

    # Der Versand: Alarme gehen in eine Warteschlange, der Empfang von MQTT wartet nie auf Telegram.
    app.bot_data["sender"] = Sender.from_config(app.bot, app.bot_data["config"]["sending"])
    app.bot_data["mqtt_task"] = asyncio.create_task(start_mqtt_listener(app))


async def post_stop(app):
    """Beim Beenden (z.B. docker stop): keine neuen Alarme mehr annehmen, aber was schon in der Warteschlange
    steht, noch zustellen. Läuft vor dem Schließen der Verbindung zu Telegram."""
    task = app.bot_data.get("mqtt_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    sender = app.bot_data.get("sender")
    if sender is not None:
        if sender.pending:
            log.info("Der Bot wird beendet, %d Nachricht(en) werden noch zugestellt", sender.pending)
        await sender.close(SHUTDOWN_SECONDS)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN fehlt (Umgebungsvariable oder .env prüfen)")

    # Zuerst das Logging, damit schon das Laden der Konfiguration protokolliert wird. Das Token wird
    # in jeder Ausgabe geschwärzt. Der endgültige Pegel kommt nach dem Laden aus der Konfiguration.
    logs.setup_logging(os.getenv("LOG_LEVEL") or "INFO", secrets=[token])

    admins = cfg.parse_admin_users(os.getenv("ADMIN_USERS", ""))
    if not admins:
        hint = ""
        if os.getenv("ALLOWED_USERS"):
            hint = " Hinweis: ALLOWED_USERS wurde in ADMIN_USERS umbenannt, bitte die Variable umbenennen."
        raise SystemExit(
            "ADMIN_USERS ist leer. Mindestens ein Admin (Telegram-User-ID) wird benötigt, "
            "alle weiteren User werden per Bot freigeschaltet (/users, /adduser)." + hint
        )

    config = cfg.load_config()
    logs.set_level(config["logging"]["level"])
    log.debug("Konfiguration: MQTT %s:%s, Topic %s, QoS %s, Datenbank %s", config["mqtt"]["host"], config["mqtt"]["port"],
              config["mqtt"]["topic"], config["mqtt"]["qos"], config["files"]["db_path"])
    entries = config["fields"]
    command_map = cfg.build_command_map(entries)

    db_path = config["files"]["db_path"]
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db.init_db(db_path)
    for old, new in LEGACY_FIELD_RENAMES.items():
        renamed = db.rename_field(db_path, old, new)
        if renamed:
            log.info("%d Abo(s) von Feld '%s' auf '%s' umgestellt", renamed, old, new)

    for entry in entries:
        entry["csv_rows"] = []
        if not entry.get("add"):
            continue                       # nur Felder mit Namenssuche haben eine CSV
        if entry.get("csv"):
            entry["csv_rows"] = knowledge.load_csv(entry["csv"], entry["for"])
        else:
            searched = os.path.abspath(os.path.join(cfg.DATA_DIR, f"descriptions_{entry['for']}.csv"))
            log.info("Keine CSV für Feld %s (gesucht: %s), es werden nur gelernte Namen genutzt", entry["for"], searched)
    knowledge.find_unassigned_csvs(cfg.DATA_DIR, entries)      # liegt eine CSV im Datenordner, die niemand nutzt?

    remember_seconds = config["duplicates"]["remember_seconds"]
    delivered = DeliveryMemory(remember_seconds) if remember_seconds > 0 else None
    log.debug("Doppelte Alarme: %s",
              f"werden {remember_seconds:g} s lang erkannt" if delivered else "Erkennung abgeschaltet")

    timeout = config["sending"]["timeout_seconds"]
    log.debug("Versand: %g s Abstand je Chat, höchstens %g/s, %d Versuche, Pausen %s s, Höchstwartezeit %g s, Zeitüberschreitung %g s",
              config["sending"]["min_interval_seconds"], config["sending"]["max_per_second"], config["sending"]["attempts"],
              config["sending"]["retry_pause_seconds"], config["sending"]["max_wait_seconds"], timeout)
    app = (ApplicationBuilder().token(token).connect_timeout(timeout).read_timeout(timeout).write_timeout(timeout)
           .post_init(post_init).post_stop(post_stop).build())
    app.bot_data.update({
        "config": config,
        "admins": admins,
        "entries": entries,
        "command_map": command_map,
        "active_fields": {e["for"] for e in entries} | {e["add"] for e in entries if e.get("add")},
        "field_labels": {
            **{e["for"]: e["label"] for e in entries},
            **{e["add"]: e["add_label"] for e in entries if e.get("add")},
        },
        "last_payload": None,
        "delivered": delivered,
        "notification_template": cfg.notification_template(config),
    })

    app.add_handler(CommandHandler("start", overview.start_handler))
    app.add_handler(CommandHandler("abo", overview.abo_handler))
    app.add_handler(CommandHandler("lastraw", overview.lastraw_handler))
    app.add_handler(CommandHandler("users", admin.users_handler))
    app.add_handler(CommandHandler("adduser", admin.adduser_handler))
    app.add_handler(CommandHandler("template", templates.template_command))
    # Die Knöpfe des Templates zuerst: Der allgemeine Handler darunter nimmt sonst jeden Klick an.
    app.add_handler(CallbackQueryHandler(templates.template_buttons, pattern=r"^tpl:"))
    app.add_handler(CallbackQueryHandler(buttons.button_handler))

    # Befehle mit Eingabe fragen bei fehlender Eingabe nach (ForceReply). Damit die Antwort
    # den richtigen Befehl ausführt, merkt sich der Bot zu jeder Rückfrage dessen Handler.
    prompt_handlers = {("adduser", ""): admin.adduser_handler, templates.HANDLER_KEY: templates.template_reply}
    for cmd in command_map:
        make = fields.make_for_handler if cmd["kind"] == "for" else fields.make_add_handler
        handler = make(cmd["entry"])
        app.add_handler(CommandHandler(cmd["name"], handler))
        prompt_handlers[(cmd["kind"], cmd["entry"]["for"])] = handler
        log.debug("Befehl /%s -> Feld '%s'", cmd["name"], cmd["entry"]["for" if cmd["kind"] == "for" else "add"])
    app.bot_data["prompt_handlers"] = prompt_handlers
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, prompts.prompt_reply_handler))

    notification = app.bot_data["notification_template"]
    log.debug("Nachrichtenvorlage: %s", notification)
    log.info("BOS-Telegram-Bot gestartet: %d Admin(s), Befehle %s, Log-Level %s", len(admins),
             ", ".join("/" + c["name"] for c in command_map), config["logging"]["level"])
    app.run_polling()


if __name__ == "__main__":
    main()
