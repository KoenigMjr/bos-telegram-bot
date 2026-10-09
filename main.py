import asyncio
import os

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import database as db
import handlers
import knowledge
import settings
from services.mqtt_service import start_mqtt_listener

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
            print(f"[Setup] Admin-Befehlsmenü für {admin_id} nicht gesetzt: {e}")

    asyncio.create_task(start_mqtt_listener(app))


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN fehlt (Umgebungsvariable oder .env prüfen)")

    admins = settings.parse_admin_users(os.getenv("ADMIN_USERS", ""))
    if not admins:
        hint = ""
        if os.getenv("ALLOWED_USERS"):
            hint = " Hinweis: ALLOWED_USERS wurde in ADMIN_USERS umbenannt, bitte die Variable umbenennen."
        raise SystemExit(
            "ADMIN_USERS ist leer. Mindestens ein Admin (Telegram-User-ID) wird benötigt, "
            "alle weiteren User werden per Bot freigeschaltet (/users, /adduser)." + hint
        )

    config = settings.load_config()
    entries = config["fields"]
    command_map = settings.build_command_map(entries)

    db_path = config["files"]["db_path"]
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db.init_db(db_path)
    for old, new in LEGACY_FIELD_RENAMES.items():
        renamed = db.rename_field(db_path, old, new)
        if renamed:
            print(f"[Migration] {renamed} Abo(s) von Feld '{old}' auf '{new}' umgestellt")

    for entry in entries:
        entry["csv_rows"] = knowledge.load_csv(entry.get("csv")) if entry.get("add") else []

    app = ApplicationBuilder().token(token).post_init(post_init).build()
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
        "notification_template": settings.notification_template(config),
    })

    app.add_handler(CommandHandler("start", handlers.start_handler))
    app.add_handler(CommandHandler("abo", handlers.abo_handler))
    app.add_handler(CommandHandler("lastraw", handlers.lastraw_handler))
    app.add_handler(CommandHandler("users", handlers.users_handler))
    app.add_handler(CommandHandler("adduser", handlers.adduser_handler))
    app.add_handler(CallbackQueryHandler(handlers.button_handler))

    # Befehle mit Eingabe fragen bei fehlender Eingabe nach (ForceReply). Damit die Antwort
    # den richtigen Befehl ausführt, merkt sich der Bot zu jeder Rückfrage dessen Handler.
    prompt_handlers = {("adduser", ""): handlers.adduser_handler}
    for cmd in command_map:
        make = handlers.make_for_handler if cmd["kind"] == "for" else handlers.make_add_handler
        handler = make(cmd["entry"])
        app.add_handler(CommandHandler(cmd["name"], handler))
        prompt_handlers[(cmd["kind"], cmd["entry"]["for"])] = handler
        print(f"[Setup] /{cmd['name']} -> Feld '{cmd['entry']['for' if cmd['kind'] == 'for' else 'add']}'")
    app.bot_data["prompt_handlers"] = prompt_handlers
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handlers.prompt_reply_handler))

    print(f"🤖 BOS-Telegram-Bot gestartet ({len(admins)} Admin(s), {len(command_map)} Feld-Befehle)")
    app.run_polling()


if __name__ == "__main__":
    main()
