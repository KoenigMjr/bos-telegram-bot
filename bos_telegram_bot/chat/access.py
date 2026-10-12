"""Zugriffsprüfung: Admins, freigeschaltete User und Zugriffsanfragen."""

import html
import logging
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.common import get_db_path
from bos_telegram_bot.storage import database as db

log = logging.getLogger(__name__)


def is_admin(context, user_id: int) -> bool:
    return user_id in (context.bot_data.get("admins") or [])


async def try_send(context, chat_id: int, text: str, **kwargs) -> bool:
    """Sendet eine Nachricht und schluckt Fehler (z.B. wenn der Empfänger dem
    Bot noch nie geschrieben hat und Telegram die Zustellung verweigert)."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as e:
        log.warning("Nachricht an %s nicht zustellbar (%s: %s)", chat_id, type(e).__name__, e)
        return False


APPROVED_TEXT = "✅ Du wurdest freigeschaltet. Schreibe /start für eine Übersicht."


async def notify_approved(context, user_id: int) -> str:
    """Teilt einem freigeschalteten User mit, dass er nun Zugriff hat.

    Zuerst privat. Telegram erlaubt einem Bot aber nicht, jemanden privat anzuschreiben, der ihm nie privat
    geschrieben hat (typisch, wenn die Anfrage aus einer Gruppe kam). Dann geht die Meldung in den Chat, aus dem
    die Anfrage kam, mit Erwähnung des Users. Liefert 'private', 'chat' oder 'none'."""
    if await try_send(context, user_id, APPROVED_TEXT):
        return "private"
    user = db.get_user(get_db_path(context), user_id)
    chat_id = user["request_chat_id"] if user else None
    if chat_id is not None and chat_id != user_id:
        name = html.escape((user["name"] or "").strip() or str(user_id))
        mention = f'<a href="tg://user?id={user_id}">{name}</a>'
        if await try_send(context, chat_id, f"✅ {mention} wurde freigeschaltet. Schreibe /start für eine Übersicht.",
                          parse_mode="HTML"):
            log.info("Freigabe-Meldung für %s ging in den Chat %s, privat war sie nicht zustellbar", user_id, chat_id)
            return "chat"
    return "none"


def approval_note(delivery: str) -> str:
    """Zusatzzeile für den Admin, wenn die Meldung an den freigeschalteten User nicht privat ankam."""
    if delivery == "chat":
        return "\nℹ️ Privat war er nicht erreichbar, die Meldung ging in den Chat seiner Anfrage."
    if delivery == "none":
        return ("\n⚠️ Der User konnte nicht benachrichtigt werden (er hat dem Bot noch nie privat geschrieben). "
                "Bitte gib ihm Bescheid, er muss dem Bot einmal privat /start schreiben.")
    return ""


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
            log.debug("Abgelehnter oder unbekannter Absender %s ignoriert", user.id)
            return
        if status == "pending":
            return await update.message.reply_text(
                "⏳ Deine Zugriffsanfrage wurde bereits gesendet und wartet auf Freigabe."
            )

        db.set_user_status(db_path, user.id, "pending", user.full_name, update.effective_chat.id)
        log.info("Zugriffsanfrage von %s (Chat %s)", user.id, update.effective_chat.id)
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
