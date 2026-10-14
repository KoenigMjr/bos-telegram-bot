"""Das Alarm-Template eines Chats per Befehl ändern: /template.

Gilt pro Chat: ein privater Chat hat sein eigenes Template, jede Gruppe ihres. Wer nichts einstellt, bekommt das
Standard-Template aus der Konfiguration. In einer Gruppe darf jeder freigeschaltete User ändern.

Ablauf: Telegram erlaubt Bots nicht, das Eingabefeld eines Users vorzubefüllen. Deshalb zeigt /template das aktuelle
Template als Kopierblock (Antippen kopiert ihn) und öffnet das Antwortfeld. Die Antwort wird geprüft und als Vorschau mit
einem erfundenen Beispiel-Alarm gezeigt. Erst ein Klick auf "Übernehmen" speichert sie. Echte Alarme kommen nie in
eine Vorschau: Sie könnten eine Wache betreffen, die der Chat gar nicht abonniert hat."""

import html
import logging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.access import restricted
from bos_telegram_bot.chat.common import get_db_path
from bos_telegram_bot.chat.prompts import PROMPT_MARKER, ask
from bos_telegram_bot.core.template import (MAX_TEMPLATE_LENGTH, SAMPLE_MATCHED, SAMPLE_PAYLOAD,
                                            custom_template_problems, render, sample_payload, unknown_placeholders)
from bos_telegram_bot.storage import database as db

log = logging.getLogger(__name__)

HANDLER_KEY = ("template", "")         # unter diesem Schlüssel merkt sich der Antwort-Dialog den Handler
RESET_WORDS = ("standard", "reset")    # /template standard stellt das Standard-Template wieder her
MAX_DRAFTS = 100                       # so viele Entwürfe (Vorschauen) werden höchstens gemerkt
DRAFT_SECONDS = 30 * 60                # und so lange, danach ist ein Klick auf "Übernehmen" abgelaufen

_EXPIRED = "⌛ Diese Vorschau ist abgelaufen (z.B. nach einem Neustart). Bitte /template erneut senden."


# ---------------------------------------------------------------- Entwürfe

def _drafts(context) -> dict:
    """Die gemerkten Entwürfe, abgelaufene sind bereits entfernt. Nur im Speicher: Ein Neustart verwirft sie."""
    drafts = context.bot_data.setdefault("template_drafts", {})
    now = time.monotonic()
    for token in [token for token, draft in drafts.items() if now - draft["created"] > DRAFT_SECONDS]:
        del drafts[token]
    return drafts


def _new_draft(context, chat_id: int, user_id: int, template) -> str:
    """template=None bedeutet: das Standard-Template wiederherstellen."""
    drafts = _drafts(context)
    while len(drafts) >= MAX_DRAFTS:
        drafts.pop(next(iter(drafts)))
    token = secrets.token_hex(4)                       # kurz genug für die 64 Byte der Knopf-Daten
    drafts[token] = {"chat_id": chat_id, "user_id": user_id, "template": template, "created": time.monotonic()}
    return token


# ---------------------------------------------------------------- Texte

def _where(chat) -> str:
    return "diesen Chat" if chat.type == "private" else "diese Gruppe"


def _effective(context, chat_id: int):
    """(Template, ist_eigenes): das eigene des Chats, sonst das Standard-Template aus der Konfiguration."""
    own = db.get_chat_template(get_db_path(context), chat_id)
    return (own, True) if own else (context.bot_data["notification_template"], False)


def _sentence(problem: str) -> str:
    """Die Prüfung liefert Satzteile ('ist leer'): mit Subjekt davor lesen sie sich besser."""
    return f"Das Template {problem}" if problem[:1].islower() else problem


_HOW = ("Tippe auf den Block, um ihn zu kopieren. Füge ihn ein, ändere ihn und sende ihn als "
        "<b>Antwort auf diese Nachricht</b>. Danach siehst du eine Vorschau und bestätigst.")
_PLACEHOLDERS = ("Platzhalter: <code>{MESSAGE}</code> Alarmtext, <code>{DESCRIPTION_LIST|RIC_LIST}</code> Name oder RIC, "
                 "<code>{MATCHED}</code> passendes Abo, <code>{TIME}</code> Zeit des Alarms, <code>{BR}</code> Zeilenumbruch. "
                 "Jedes Feld des Alarms geht in Großbuchstaben (<code>{RIC}</code>), die Kurznamen von BOSWatch auch (<code>{MSG}</code>).")
_TAGS = ("Erlaubt: <code>&lt;b&gt;</code> <code>&lt;i&gt;</code> <code>&lt;u&gt;</code> <code>&lt;s&gt;</code> "
         "<code>&lt;code&gt;</code> <code>&lt;pre&gt;</code> <code>&lt;a href=\"…\"&gt;</code>. Zeilenumbrüche bleiben.")


def _prompt_text(chat, template: str, is_own: bool, draft: bool = False, problems=None) -> str:
    """Die Nachricht mit dem Kopierblock. Sie beginnt mit dem Marker des Antwort-Dialogs."""
    if draft:
        title = f"Dein Entwurf für {_where(chat)}"
    else:
        title = f"Template für {_where(chat)} ({'eigenes' if is_own else 'Standard'})"
    shown = template if len(template) <= MAX_TEMPLATE_LENGTH + 100 else template[:MAX_TEMPLATE_LENGTH] + "…"
    lines = [f"{PROMPT_MARKER} <b>{title}</b>"]
    if problems:
        lines += ["", "⚠️ <b>So geht es noch nicht:</b>"] + [f"• {html.escape(_sentence(p), quote=False)}" for p in problems]
    lines += ["", f"<pre>{html.escape(shown, quote=False)}</pre>", "", _HOW, "", _PLACEHOLDERS, _TAGS]
    if is_own:
        lines += ["", "Zurück zum Standard: <code>/template standard</code>"]
    return "\n".join(lines)


def _keyboard(token: str, reset: bool = False) -> InlineKeyboardMarkup:
    confirm = InlineKeyboardButton("✅ Wiederherstellen" if reset else "✅ Übernehmen", callback_data=f"tpl:ok:{token}")
    discard = InlineKeyboardButton("✖️ Verwerfen", callback_data=f"tpl:no:{token}")
    if reset:
        return InlineKeyboardMarkup([[confirm, discard]])
    return InlineKeyboardMarkup([[confirm, InlineKeyboardButton("✏️ Ändern", callback_data=f"tpl:again:{token}")], [discard]])


# ---------------------------------------------------------------- Ablauf

async def _start_edit(update, context, template=None, problems=None, message=None) -> None:
    """Zeigt das Template im Kopierblock und fragt nach dem neuen. Mit template=... steht dort der Entwurf des Users."""
    chat = update.effective_chat
    current, is_own = _effective(context, chat.id)
    text = _prompt_text(chat, current if template is None else template, is_own, draft=template is not None, problems=problems)
    await ask(update, context, text, "Neues Template einfügen", HANDLER_KEY, message=message)


async def _propose_reset(update, context) -> None:
    chat = update.effective_chat
    _, is_own = _effective(context, chat.id)
    if not is_own:
        return await update.message.reply_text("ℹ️ Dieser Chat nutzt bereits das Standard-Template.")
    preview = render(context.bot_data["notification_template"], sample_payload(), SAMPLE_MATCHED)
    token = _new_draft(context, chat.id, update.effective_user.id, None)
    await update.message.reply_text(
        f"↩️ <b>Standard-Template wiederherstellen?</b>\n\nSo sähe ein Beispiel-Alarm dann aus:\n\n{preview}",
        parse_mode="HTML", reply_markup=_keyboard(token, reset=True), **({"do_quote": True} if chat.type != "private" else {}))


async def _propose(update, context, text: str) -> None:
    """Prüft einen Vorschlag und zeigt ihn als Vorschau. Gespeichert wird erst nach dem Klick auf Übernehmen."""
    chat, user = update.effective_chat, update.effective_user
    if text.strip().lower() in RESET_WORDS:
        return await _propose_reset(update, context)

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")        # Umbrüche am Rand weg, innen bleiben sie
    problems = custom_template_problems(text)
    if problems:
        return await _start_edit(update, context, template=text, problems=problems)
    current, _ = _effective(context, chat.id)
    if text == current:
        return await update.message.reply_text("ℹ️ Das ist schon das aktuelle Template, es gibt nichts zu ändern.")

    body = "👁 <b>Vorschau mit einem Beispiel-Alarm</b>\n\n" + render(text, sample_payload(), SAMPLE_MATCHED)
    unknown = unknown_placeholders(text, SAMPLE_PAYLOAD)
    if unknown:
        names = ", ".join(f"<code>{{{html.escape(name, quote=False)}}}</code>" for name in unknown)
        body += f"\n\n⚠️ Unbekannte Platzhalter, sie bleiben leer: {names}"
    body += "\n\n<b>Übernehmen?</b>"

    token = _new_draft(context, chat.id, user.id, text)
    try:
        # Mit do_quote: In Gruppen hängt die Vorschau so an der Nachricht des Users, "Ändern" kann ihm gezielt antworten.
        await update.message.reply_text(body, parse_mode="HTML", reply_markup=_keyboard(token),
                                        **({"do_quote": True} if chat.type != "private" else {}))
    except BadRequest as error:                       # die Vorschau ist zugleich der Test, ob Telegram es versteht
        _drafts(context).pop(token, None)
        log.info("Template-Entwurf von %s in Chat %s abgelehnt: %s", user.id, chat.id, error)
        await _start_edit(update, context, template=text, problems=[f"wird von Telegram abgelehnt ({error})"])


@restricted
async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/template zeigt und ändert das Template. Mit Text dahinter gilt der Text als Vorschlag, 'standard' stellt zurück."""
    parts = (update.message.text or "").split(None, 1)
    argument = parts[1].strip() if len(parts) > 1 else ""
    if not argument:
        return await _start_edit(update, context)
    await _propose(update, context, argument)


async def template_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Die Antwort auf die Rückfrage von /template. Gelesen wird der Rohtext, nicht die in Wörter zerlegten Argumente:
    Sonst gingen die Zeilenumbrüche des Templates verloren."""
    await _propose(update, context, update.message.text or "")


@restricted
async def template_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":", 2)
    if len(parts) != 3:                                  # kaputte oder veraltete Knopf-Daten: wie abgelaufen behandeln
        await query.answer()
        return await query.edit_message_text(_EXPIRED)
    _, action, token = parts
    draft = _drafts(context).get(token)
    if draft is None or draft["chat_id"] != update.effective_chat.id:
        await query.answer()
        return await query.edit_message_text(_EXPIRED)
    if query.from_user.id != draft["user_id"]:
        # Sonst könnte jemand anderes in der Gruppe über den Vorschlag eines Users entscheiden.
        return await query.answer("Nur wer die Änderung vorgeschlagen hat, kann darüber entscheiden.", show_alert=True)
    await query.answer()
    _drafts(context).pop(token, None)

    chat_id, user_id, template = draft["chat_id"], draft["user_id"], draft["template"]
    where = _where(update.effective_chat)
    if action == "no":
        return await query.edit_message_text("✖️ Verworfen, das Template bleibt wie es war.")
    if action == "ok":
        if template is None:
            db.delete_chat_template(get_db_path(context), chat_id)
            log.info("Template für Chat %s auf den Standard zurückgesetzt (User %s)", chat_id, user_id)
            return await query.edit_message_text(f"✅ Das Standard-Template gilt wieder für {where}.")
        db.set_chat_template(get_db_path(context), chat_id, template, user_id)
        log.info("Template für Chat %s geändert (User %s)", chat_id, user_id)
        log.debug("Neues Template für Chat %s: %s", chat_id, template)
        return await query.edit_message_text(
            f"✅ Das Template für {where} wurde geändert.\nMit /template siehst du es jederzeit und kannst es erneut ändern.")
    if action == "again" and template is not None:
        await query.edit_message_text("✏️ Dein Entwurf steht in der nächsten Nachricht, ändere ihn dort.")
        anchor = getattr(query.message, "reply_to_message", None) or query.message
        return await _start_edit(update, context, template=template, message=anchor)
    await query.edit_message_text(_EXPIRED)
