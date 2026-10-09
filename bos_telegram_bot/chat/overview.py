"""Übersichten: /start, /lastraw und /abo."""

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.access import restricted
from bos_telegram_bot.chat.common import get_db_path
from bos_telegram_bot.storage import database as db


@restricted
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "👋 <b>BOS-Alarm-Bot aktiv!</b>",
        "",
        "Abonniere per Befehl. <code>*</code> ist ein Platzhalter: <code>301*</code> beginnt mit 301, "
        "<code>*301*</code> enthält 301.",
        "",
    ]
    for cmd in context.bot_data["command_map"]:
        entry = cmd["entry"]
        if cmd["kind"] == "for":
            lines.append(f"• <code>/{cmd['name']} &lt;Wert oder Muster&gt;</code> – {html.escape(entry['label'])}")
        else:
            lines.append(f"• <code>/{cmd['name']} &lt;Name oder Muster&gt;</code> – {html.escape(entry['add_label'])} suchen")
    lines.append("")
    lines.append("Ein Befehl ohne Eingabe fragt dich danach. Du kannst die Eingabe auch gleich mitschicken.")
    lines.append("")
    lines.append("• <code>/abo</code> – Aktive Abonnements anzeigen &amp; verwalten")
    lines.append("• <code>/lastraw</code> – Letztes empfangenes Alarm-JSON anzeigen")
    if update.effective_chat.type != "private":
        lines.append("")
        lines.append("ℹ️ In dieser Gruppe gesetzte Abos gelten für alle Mitglieder dieser Gruppe.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# Technische BOSWatch3-Felder, auf die man nicht abonnieren will. Alles andere im
# Alarm (z.B. Felder, die das Modul 'descriptor' ergänzt) wird in /lastraw als
# mögliches neues Feld vorgeschlagen.
_TECHNICAL_PREFIXES = ("client", "server", "multicast")


_TECHNICAL_KEYS = {"timestamp", "mode", "bitrate", "inputSource", "frequency", "republished", "subric"}


def unconfigured_fields(payload: dict, active_fields: set) -> list:
    """Felder des Alarms, die weder technisch noch *_list noch bereits als
    Befehl konfiguriert sind."""
    return [
        key for key in payload
        if not key.endswith("_list")
        and not key.startswith(_TECHNICAL_PREFIXES)
        and key not in _TECHNICAL_KEYS
        and key not in active_fields
    ]


@restricted
async def lastraw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json

    payload = context.bot_data.get("last_payload")
    if not payload:
        return await update.message.reply_text("Es wurde noch kein Alarm empfangen.")
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > 3500:
        text = text[:3500] + "\n… (gekürzt)"
    message = f"<pre>{html.escape(text)}</pre>"

    unused = unconfigured_fields(payload, context.bot_data["active_fields"])
    if unused:
        message += (
            "\n\nℹ️ Felder im Alarm, für die es noch keinen Befehl gibt: "
            f"<code>{html.escape(', '.join(unused))}</code>\n"
            "Anlegen über die Umgebungsvariable <code>EXTRA_FIELDS</code> "
            f"(z.B. <code>EXTRA_FIELDS={html.escape(','.join(unused))}</code>)."
        )
    await update.message.reply_text(message, parse_mode="HTML")


def _with_label(field_label: str, alias: str) -> str:
    """'Feld: Name' für die Anzeige. Muster-Abos tragen das Feld schon im Namen
    ('Alarmstichwort: THL*'), dann wird es nicht noch einmal vorangestellt."""
    prefix = f"{field_label}: "
    return alias if alias.startswith(prefix) else prefix + alias


@restricted
async def abo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    subs = db.get_chat_subs(get_db_path(context), chat.id)

    if not subs:
        if chat.type == "private":
            return await update.message.reply_text("Du hast aktuell keine Abonnements.")
        return await update.message.reply_text("Diese Gruppe hat aktuell keine Abonnements.")

    labels = context.bot_data["field_labels"]
    header = "📋 <b>Deine aktiven Abonnements:</b>" if chat.type == "private" \
        else f"📋 <b>Aktive Abonnements dieser Gruppe ({html.escape(chat.title or '')}):</b>"
    lines = [header]
    keyboard = []
    for sub in subs:
        text = _with_label(labels.get(sub["field"], sub["field"]), sub["alias"])
        lines.append(f"• {html.escape(text)}")
        keyboard.append([InlineKeyboardButton(f"🗑️ {text}"[:45], callback_data=f"del:{sub['id']}")])

    keyboard.append([InlineKeyboardButton("✔️ Alles passt", callback_data="close_menu")])
    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )
