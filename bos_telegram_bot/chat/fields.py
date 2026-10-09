"""Abonnieren per Feld-Befehl (/ric, /message ...) und per Namenssuche (/description)."""

import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bos_telegram_bot.chat.access import restricted
from bos_telegram_bot.chat.common import get_db_path
from bos_telegram_bot.chat.hints import check_hints
from bos_telegram_bot.chat.prompts import PROMPT_HOW, PROMPT_MARKER, ask
from bos_telegram_bot.core.matching import is_pattern_input, wildcard_to_regex
from bos_telegram_bot.storage import database as db, knowledge


ITEMS_PER_PAGE = 5


def _for_prompt(entry: dict):
    """Anleitung samt Rückfrage für einen Feld-Befehl ohne Eingabe: (Text, Platzhalter)."""
    cmd, label = entry["command"], html.escape(entry["label"])
    if entry["match"] == "exact":
        lines = [
            f"• <code>/{cmd} Wert</code> – genau dieser Wert",
            f"• <code>/{cmd} Anfang*</code> – beginnt mit …",
            f"• <code>/{cmd} *Teil*</code> – enthält …",
        ]
        placeholder = "Wert oder Muster, z.B. 301*"
    else:
        lines = [
            f"• <code>/{cmd} Text</code> – enthält Text",
            f"• <code>/{cmd} Anfang*</code> – beginnt mit …",
            f"• <code>/{cmd} B 3+</code> – „B“ mit Zahl 3 oder höher",
            f"• <code>/{cmd} re:…</code> – reguläre Ausdrücke",
        ]
        placeholder = "Text oder Muster, z.B. THL*"
    text = f"{PROMPT_MARKER} <b>{label}</b>: Wert oder Muster eingeben.\n{PROMPT_HOW}\n" + "\n".join(lines)
    return text, placeholder


def _confirmation(headline: str, alias: str, hints: list, detail: str = "") -> str:
    text = f"{headline}\n<b>{html.escape(alias)}</b>"
    if detail:
        text += f"\n{detail}"
    if hints:
        text += "\n\n" + "\n".join(hints)
    return text


def make_for_handler(entry: dict):
    """Befehl für ein Feld (z.B. /ric, /message): Freieingabe als Wert oder Muster."""
    field, label, match = entry["for"], entry["label"], entry["match"]

    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        db_path = get_db_path(context)
        chat_id = update.effective_chat.id

        if not context.args:
            text, placeholder = _for_prompt(entry)
            return await ask(update, context, text, placeholder, ("for", field))

        raw = " ".join(context.args).strip()
        name = None
        try:
            if match == "exact" and not is_pattern_input(raw):
                target, is_regex = raw, False
                if entry.get("add"):
                    name = knowledge.resolve_name(entry, db_path, raw)
                alias = name or f"{label}: {raw}"
            else:
                # Bei Feldern mit 'exact' gilt ein Muster immer für den ganzen Wert.
                target, is_regex = wildcard_to_regex(raw, anchor=True if match == "exact" else None), True
                alias = f"{label}: {raw}"
        except re.error as e:
            return await update.message.reply_text(f"❌ Ungültiges Muster: {html.escape(str(e))}")

        db.add_sub(db_path, chat_id, field, target, alias, is_regex)
        hints = check_hints(context, field, target, is_regex)
        if name:
            text = _confirmation("✅ Abonniert:", alias, hints, f"{html.escape(label)}: <code>{html.escape(raw)}</code>")
        elif is_regex:
            text = _confirmation("✅ Filter angelegt:", alias, hints)
        else:
            text = _confirmation("✅ Abonniert:", alias, hints)
        await update.message.reply_text(text, parse_mode="HTML")

    return handler


async def subscribe_row(reply, context, chat_id: int, entry: dict, row: dict) -> None:
    """Abonniert den 'for'-Wert (z.B. die RIC) zu einem ausgewählten Namen."""
    db.add_sub(get_db_path(context), chat_id, entry["for"], row["for"], row["add"], row["isRegex"])
    kind = "Muster" if row["isRegex"] else entry["label"]
    detail = f"{html.escape(kind)}: <code>{html.escape(row['for'])}</code>"
    await reply(_confirmation("✅ Abonniert:", row["add"], [], detail), parse_mode="HTML")


async def create_name_pattern(reply, context, chat_id: int, entry: dict, raw: str) -> None:
    """Legt ein Muster auf das 'add'-Feld an (z.B. alle Beschreibungen mit RTW)."""
    add_key, add_label = entry["add"], entry["add_label"]
    try:
        regex = wildcard_to_regex(raw)
    except re.error as e:
        return await reply(f"❌ Ungültiges Muster: {html.escape(str(e))}")
    alias = f"{add_label}: {raw}"
    db.add_sub(get_db_path(context), chat_id, add_key, regex, alias, True)
    hints = check_hints(context, add_key, regex, True)
    await reply(_confirmation("✅ Filter angelegt:", alias, hints), parse_mode="HTML")


def _pattern_button(entry: dict, text: str) -> list:
    return [InlineKeyboardButton(text, callback_data=f"pat:{entry['for']}")]


def _cancel_row(for_key: str) -> list:
    """Eigene Zeile, damit man nicht versehentlich daneben tippt."""
    return [InlineKeyboardButton("✖️ Abbrechen", callback_data=f"cancel:{for_key}")]


async def send_page(reply, for_key: str, results: list, page: int, term: str = ""):
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE

    keyboard = [
        [InlineKeyboardButton(f"➕ {row['add']}"[:60], callback_data=f"add:{for_key}:{start_idx + i}")]
        for i, row in enumerate(results[start_idx:end_idx])
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Zurück", callback_data=f"page:{for_key}:{page-1}"))
    if end_idx < len(results):
        nav.append(InlineKeyboardButton("Weiter ➡️", callback_data=f"page:{for_key}:{page+1}"))
    if nav:
        keyboard.append(nav)
    if term:
        keyboard.append([InlineKeyboardButton(f"🔎 Alle mit „{term[:25]}“ als Muster", callback_data=f"pat:{for_key}")])
    keyboard.append(_cancel_row(for_key))

    text = f"🔍 Treffer {start_idx+1}-{min(end_idx, len(results))} von {len(results)}:"
    await reply(text, reply_markup=InlineKeyboardMarkup(keyboard))


def make_add_handler(entry: dict):
    """Befehl für die Namenssuche (z.B. /description): sucht in der Liste und den
    gelernten Namen und abonniert den zugehörigen 'for'-Wert. Mit Wildcard wird
    direkt ein Muster auf den Namen angelegt."""
    for_key, add_label, cmd = entry["for"], entry["add_label"], entry["add_command"]

    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        db_path = get_db_path(context)
        chat_id = update.effective_chat.id
        reply = update.message.reply_text

        if not context.args:
            text = (
                f"{PROMPT_MARKER} <b>{html.escape(add_label)}</b>: Name oder Muster eingeben.\n{PROMPT_HOW}\n"
                f"• <code>/{cmd} Name</code> – in der Liste suchen\n"
                f"• <code>/{cmd} *Teil*</code> – Muster (Platzhalter <code>*</code>)"
            )
            return await ask(update, context, text, "Name oder Muster, z.B. *wagen*", ("add", for_key))

        raw = " ".join(context.args).strip()
        if is_pattern_input(raw):
            return await create_name_pattern(reply, context, chat_id, entry, raw)

        rows = knowledge.known_rows(db_path, entry)
        term = raw.lower()
        context.user_data[f"term:{for_key}"] = raw

        exact = next((r for r in rows if r["add"].lower() == term), None)
        if exact:
            return await subscribe_row(reply, context, chat_id, entry, exact)

        results = [r for r in rows if term in r["add"].lower()]
        if not results:
            hint = "" if rows else "\nDie Liste ist noch leer, sie füllt sich mit den ersten Alarmen."
            return await reply(
                f"❌ Keine Treffer für „{html.escape(raw)}“ in der Liste.{hint}",
                reply_markup=InlineKeyboardMarkup([
                    _pattern_button(entry, "🔎 Trotzdem als Muster anlegen"),
                    _cancel_row(for_key),
                ]),
                parse_mode="HTML",
            )
        if len(results) == 1:
            return await subscribe_row(reply, context, chat_id, entry, results[0])

        context.user_data[f"search:{for_key}"] = results
        await send_page(reply, for_key, results, 0, raw)

    return handler
