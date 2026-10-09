"""Zugriffsprüfung: Admins, freigeschaltete User und Zugriffsanfragen."""

import html
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bos_telegram_bot.storage import database as db


def is_admin(context, user_id: int) -> bool:
    return user_id in (context.bot_data.get("admins") or [])


async def try_send(context, chat_id: int, text: str, **kwargs) -> bool:
    """Sendet eine Nachricht und schluckt Fehler (z.B. wenn der Empfänger dem
    Bot noch nie geschrieben hat und Telegram die Zustellung verweigert)."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as e:
        print(f"[Telegram] Nachricht an {chat_id} nicht zustellbar: {e}")
        return False


async def _notify_admins_of_request(context, user, chat) -> None:
    where = "privat" if chat.type == "private" else f"Gruppe „{chat.title or chat.id}“"
    username = f" (@{user.username})" if user.username else ""
    text = (
        "🔔 <b>Zugriffsanfrage</b>\n"
        f"Name: {html.escape(user.full_name)}{html.escape(username)}\n"
        f"ID: <code>{user.id}</code>\n"
        f"Angefragt: {html.escape(where)}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Freischalten", callback_data=f"acc:ok:{user.id}"),
        InlineKeyboardButton("❌ Ablehnen", callback_data=f"acc:no:{user.id}"),
    ]])
    for admin_id in context.bot_data.get("admins") or []:
        await try_send(context, admin_id, text, parse_mode="HTML", reply_markup=keyboard)


def restricted(func):
    """Zugriffsprüfung für Befehle und Inline-Buttons.

    Berechtigt sind
      - Admins (ADMIN_USERS aus der Umgebung) und
      - User mit Status 'allowed' in der Datenbank (per Bot freigeschaltet).

    Unbekannte User bekommen ihre Telegram-ID genannt, und die Admins erhalten
    eine Anfrage mit Freigabe-Buttons (einmalig pro User). Abgelehnte User
    werden still ignoriert.
    """

    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if is_admin(context, user.id):
            return await func(update, context, *args, **kwargs)

        db_path = context.bot_data["config"]["files"]["db_path"]
        status = db.get_user_status(db_path, user.id)
        if status == "allowed":
            return await func(update, context, *args, **kwargs)

        if update.callback_query:
            if status == "denied":
                await update.callback_query.answer()
            else:
                await update.callback_query.answer(
                    f"⛔ Keine Berechtigung. Deine Telegram-ID: {user.id}", show_alert=True
                )
            return

        if not update.message or status == "denied":
            return
        if status == "pending":
            return await update.message.reply_text(
                "⏳ Deine Zugriffsanfrage wurde bereits gesendet und wartet auf Freigabe."
            )

        db.set_user_status(db_path, user.id, "pending", user.full_name)
        await update.message.reply_text(
            f"⛔ Keine Berechtigung. Deine Telegram-ID: {user.id}\n"
            f"Eine Zugriffsanfrage wurde an die Admins gesendet."
        )
        await _notify_admins_of_request(context, user, update.effective_chat)

    return wrapped


def admin_only(func):
    """Nur für Admins (ADMIN_USERS). Wird innerhalb von @restricted verwendet."""

    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_admin(context, update.effective_user.id):
            if update.message:
                await update.message.reply_text("⛔ Nur für Admins.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapped
