"""Inline-Buttons: Antworten auf Klicks in den Menüs."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.access import is_admin, restricted, try_send
from bos_telegram_bot.chat.common import entry_by_for, get_db_path
from bos_telegram_bot.chat.fields import create_name_pattern, send_page, subscribe_row
from bos_telegram_bot.storage import database as db

log = logging.getLogger(__name__)


_EXPIRED = "⌛ Diese Auswahl ist abgelaufen (z.B. nach einem Neustart). Bitte die Suche erneut ausführen."


@restricted
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id

    # Admin-Aktionen: Anfragen freigeben/ablehnen, User entfernen.
    if query.data.startswith(("acc:", "usr:")):
        if not is_admin(context, query.from_user.id):
            return await query.answer("⛔ Nur für Admins.", show_alert=True)
        await query.answer()
        admin_db_path = get_db_path(context)
        prefix, action, uid_str = query.data.split(":", 2)
        uid = int(uid_str)

        if prefix == "acc":
            current = db.get_user_status(admin_db_path, uid)
            if current != "pending":
                return await query.edit_message_text(
                    f"ℹ️ Anfrage von {uid} ist bereits bearbeitet (Status: {current or 'gelöscht'})."
                )
            if action == "ok":
                db.set_user_status(admin_db_path, uid, "allowed")
                log.info("Admin %s hat %s freigeschaltet", query.from_user.id, uid)
                await query.edit_message_text(f"✅ Freigeschaltet: {uid}")
                await try_send(context, uid, "✅ Du wurdest freigeschaltet. Schreibe /start für eine Übersicht.")
            else:
                db.set_user_status(admin_db_path, uid, "denied")
                log.info("Admin %s hat die Anfrage von %s abgelehnt", query.from_user.id, uid)
                await query.edit_message_text(f"❌ Abgelehnt: {uid}")
        else:  # usr:del
            db.remove_user(admin_db_path, uid)
            db.remove_chat_subs(admin_db_path, uid)  # private Abos des Users entfernen
            log.info("Admin %s hat %s den Zugriff entzogen (inkl. privater Abos)", query.from_user.id, uid)
            await query.edit_message_text(f"🗑️ Zugriff entzogen: {uid} (inkl. seiner privaten Abos)")
        return

    await query.answer()
    data = query.data

    if data == "close_menu":
        return await query.edit_message_text("👍 Menü geschlossen.")

    if data.startswith("del:"):
        sub_id = int(data.split(":", 1)[1])
        db.remove_sub_by_id(get_db_path(context), chat_id, sub_id)
        log.info("Abo entfernt: Chat %s, Abo %s", chat_id, sub_id)
        return await query.edit_message_text("🗑️ Abonnement entfernt.")

    if data.startswith("cancel:"):
        for_key = data.split(":", 1)[1]
        context.user_data.pop(f"search:{for_key}", None)
        context.user_data.pop(f"term:{for_key}", None)
        return await query.edit_message_text("✖️ Abgebrochen, es wurde nichts abonniert.")

    if data.startswith(("page:", "add:", "pat:")):
        kind, for_key, *rest = data.split(":", 2)
        entry = entry_by_for(context, for_key)
        if not entry or not entry.get("add"):
            return await query.edit_message_text("ℹ️ Dieses Feld ist nicht mehr konfiguriert.")
        results = context.user_data.get(f"search:{for_key}", [])
        term = context.user_data.get(f"term:{for_key}", "")

        if kind == "page":
            if not results:
                return await query.edit_message_text(_EXPIRED)
            return await send_page(query.edit_message_text, for_key, results, int(rest[0]), term)

        if kind == "add":
            idx = int(rest[0])
            if idx >= len(results):
                return await query.edit_message_text(_EXPIRED)
            return await subscribe_row(query.edit_message_text, context, chat_id, entry, results[idx])

        # pat: Suchbegriff als Muster auf den Namen anlegen
        if not term:
            return await query.edit_message_text(_EXPIRED)
        return await create_name_pattern(query.edit_message_text, context, chat_id, entry, term)
