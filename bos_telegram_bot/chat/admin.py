"""Benutzerverwaltung für Admins: /users und /adduser."""

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.access import admin_only, is_admin, restricted, try_send
from bos_telegram_bot.chat.prompts import PROMPT_HOW, PROMPT_MARKER, ask
from bos_telegram_bot.storage import database as db


@restricted
@admin_only
async def users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_path = context.bot_data["config"]["files"]["db_path"]
    allowed = db.list_users(db_path, "allowed")
    pending = db.list_users(db_path, "pending")

    lines = ["👥 <b>Benutzer</b>", "", "<b>Admins</b> (ADMIN_USERS, nur per Umgebungsvariable änderbar):"]
    lines += [f"• <code>{uid}</code>" for uid in context.bot_data["admins"]]

    keyboard = []
    lines += ["", "<b>Freigeschaltet:</b>"]
    if allowed:
        for row in allowed:
            label = row["name"] or "ohne Namen"
            lines.append(f"• {html.escape(label)} (<code>{row['user_id']}</code>)")
            keyboard.append([InlineKeyboardButton(f"🗑️ Entfernen: {label[:25]}", callback_data=f"usr:del:{row['user_id']}")])
    else:
        lines.append("• keine")

    if pending:
        lines += ["", "<b>Offene Anfragen:</b>"]
        for row in pending:
            label = row["name"] or "ohne Namen"
            lines.append(f"• {html.escape(label)} (<code>{row['user_id']}</code>)")
            keyboard.append([
                InlineKeyboardButton(f"✅ {label[:18]}", callback_data=f"acc:ok:{row['user_id']}"),
                InlineKeyboardButton("❌", callback_data=f"acc:no:{row['user_id']}"),
            ])

    if keyboard:
        keyboard.append([InlineKeyboardButton("✔️ Schließen", callback_data="close_menu")])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


@restricted
@admin_only
async def adduser_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_path = context.bot_data["config"]["files"]["db_path"]
    if not context.args:
        text = (
            f"{PROMPT_MARKER} <b>Benutzer freischalten</b>: Telegram-ID und optional einen Namen eingeben.\n"
            f"{PROMPT_HOW}\n"
            "• <code>/adduser 123456789 Max Mustermann</code>"
        )
        return await ask(update, context, text, "ID und optional Name", ("adduser", ""))
    try:
        uid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Ungültige ID, erwartet wird eine Zahl.")

    if is_admin(context, uid):
        return await update.message.reply_text("ℹ️ Diese ID ist bereits Admin.")

    name = " ".join(context.args[1:]).strip() or None
    db.set_user_status(db_path, uid, "allowed", name)
    await update.message.reply_text(f"✅ Freigeschaltet: <code>{uid}</code>", parse_mode="HTML")
    await try_send(context, uid, "✅ Du wurdest freigeschaltet. Schreibe /start für eine Übersicht.")
