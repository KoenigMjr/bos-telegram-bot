"""Rückfrage-Dialog: Ein Befehl ohne Eingabe fragt nach (ForceReply)."""

from telegram import ForceReply, Update
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.access import restricted


#
# Telegram sendet einen Befehl aus dem Menü sofort ohne Eingabe ab. Der Bot antwortet
# dann mit der Anleitung UND einer Rückfrage (ForceReply): Das Eingabefeld ist auf die
# Antwort eingestellt, ein Tipp mit der Eingabe führt den Befehl aus, als hätte man ihn
# mit Eingabe geschrieben. Die Anleitung bleibt vollständig erhalten.
PROMPT_MARKER = "✍️"   # jede Rückfrage beginnt damit, daran erkennt der Bot sie wieder


PROMPT_HOW = "Einfach auf diese Nachricht antworten oder den Befehl direkt mit Eingabe schreiben:"


MAX_OPEN_PROMPTS = 10  # pro Person gemerkte, noch unbeantwortete Rückfragen


async def ask(update, context, text: str, placeholder: str, handler_key: tuple) -> None:
    """Sendet Anleitung plus Rückfrage und merkt sich, welcher Befehl gemeint war."""
    chat = update.effective_chat
    in_group = chat.type != "private"
    options = {
        "parse_mode": "HTML",
        # 'selective' zeigt die Rückfrage nur der Person, die den Befehl gesendet hat. Das
        # greift in Gruppen nur, wenn die Nachricht eine Antwort auf ihren Befehl ist.
        "reply_markup": ForceReply(selective=in_group, input_field_placeholder=placeholder[:64]),
    }
    if in_group:
        options["do_quote"] = True
    sent = await update.message.reply_text(text, **options)

    prompts = context.user_data.setdefault("prompts", {})
    prompts[(chat.id, sent.message_id)] = {"handler": handler_key}
    while len(prompts) > MAX_OPEN_PROMPTS:
        prompts.pop(next(iter(prompts)))


_PROMPT_INVALID = (
    "⌛ Diese Eingabeaufforderung ist nicht mehr gültig (z.B. nach einem Neustart oder weil "
    "sie von jemand anderem stammt). Bitte den Befehl erneut senden."
)


@restricted
async def _handle_prompt_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt) -> None:
    if not prompt:
        return await update.message.reply_text(_PROMPT_INVALID)
    handler = context.bot_data.get("prompt_handlers", {}).get(prompt["handler"])
    if handler is None:
        return await update.message.reply_text("ℹ️ Dieser Befehl ist nicht mehr konfiguriert.")
    # Die Antwort wird behandelt, als wäre sie hinter den Befehl geschrieben worden.
    context.args = update.message.text.split()
    await handler(update, context)


async def prompt_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Antworten auf eine Rückfrage des Bots.

    Bewusst ohne @restricted am Anfang: Antworten auf Nachrichten anderer Personen
    dürfen keine Zugriffsanfrage auslösen. Erst wenn feststeht, dass es die Antwort auf
    eine Rückfrage des Bots ist, greift die Zugriffsprüfung."""
    message = update.message
    replied = message.reply_to_message if message else None
    if not replied or not replied.from_user or replied.from_user.id != context.bot.id:
        return
    if not (replied.text or "").startswith(PROMPT_MARKER):
        return  # Antwort auf eine andere Bot-Nachricht (Alarm, /abo, ...)

    prompts = context.user_data.get("prompts", {})
    prompt = prompts.pop((update.effective_chat.id, replied.message_id), None)
    await _handle_prompt_reply(update, context, prompt)
