import html
import re
from functools import wraps

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db
import knowledge
from matching import candidate_values, has_wildcard_syntax, match_sub, wildcard_to_regex

ITEMS_PER_PAGE = 5


def is_admin(context, user_id: int) -> bool:
    return user_id in (context.bot_data.get("admins") or [])


async def _try_send(context, chat_id: int, text: str, **kwargs) -> bool:
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
        await _try_send(context, admin_id, text, parse_mode="HTML", reply_markup=keyboard)


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


# ---------------------------------------------------------------- Rückfrage statt Fehlermeldung
#
# Telegram sendet einen Befehl aus dem Menü sofort ohne Eingabe ab. Der Bot antwortet
# dann mit der Anleitung UND einer Rückfrage (ForceReply): Das Eingabefeld ist auf die
# Antwort eingestellt, ein Tipp mit der Eingabe führt den Befehl aus, als hätte man ihn
# mit Eingabe geschrieben. Die Anleitung bleibt vollständig erhalten.

PROMPT_MARKER = "✍️"   # jede Rückfrage beginnt damit, daran erkennt der Bot sie wieder
PROMPT_HOW = "Einfach auf diese Nachricht antworten oder den Befehl direkt mit Eingabe schreiben:"
MAX_OPEN_PROMPTS = 10  # pro Person gemerkte, noch unbeantwortete Rückfragen


async def _ask(update, context, text: str, placeholder: str, handler_key: tuple) -> None:
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


def _db_path(context) -> str:
    return context.bot_data["config"]["files"]["db_path"]


def _entry_by_for(context, for_key: str):
    return next((e for e in context.bot_data["entries"] if e["for"] == for_key), None)


def _find_entry(context, field_key: str):
    """Eintrag und Seite ('for' oder 'add'), zu der ein Feldschlüssel gehört."""
    for entry in context.bot_data["entries"]:
        if entry["for"] == field_key:
            return entry, "for"
        if entry.get("add") == field_key:
            return entry, "add"
    return None, None


# ---------------------------------------------------------------- Hinweise gegen Tippfehler

def _known_hint(db_path: str, entry: dict, side: str, target: str, is_regex: bool):
    """Prüft das Abo gegen alle bekannten Werte (Liste + gelernte Paare)."""
    rows = knowledge.known_rows(db_path, entry)
    plain = [r for r in rows if not r["isRegex"]]
    if not plain:
        return None  # nichts bekannt, also auch nichts zu vergleichen

    if is_regex:
        rx = re.compile(target)
        hits = [r for r in plain if rx.match(r[side])]
        if hits:
            n = len(hits)
            names = ", ".join(html.escape(r["add"]) for r in hits[:3])
            more = f" (+{n - 3} weitere)" if n > 3 else ""
            count = "1 bekannten Eintrag" if n == 1 else f"{n} bekannte Einträge"
            return f"✅ Trifft {count}: {names}{more}"
        note = " Wache-Muster der Liste werden dabei nicht geprüft." if any(r["isRegex"] for r in rows) else ""
        return "ℹ️ Dazu ist kein passender Eintrag bekannt (weder in der Liste noch aus bisherigen Alarmen)." + note

    if side == "for":
        if knowledge.resolve_name(entry, db_path, target):
            return None
    elif any(r["add"] == target for r in plain):
        return None
    return ("ℹ️ Diesen Wert kenne ich noch nicht (weder aus der Liste noch aus bisherigen Alarmen). "
            "Prüfe die Schreibweise.")


def _last_alarm_hint(context, field_key: str, target: str, is_regex: bool):
    payload = context.bot_data.get("last_payload")
    if not payload:
        return None
    values, raw_list = candidate_values(payload, field_key)
    if not values:
        return "ℹ️ Der letzte Alarm enthält dieses Feld nicht."
    ok = match_sub({"target": target, "is_regex": is_regex, "alias": ""}, values, raw_list) is not None
    if ok:
        return "✅ Passt auf den letzten Alarm."
    return "ℹ️ Passt nicht auf den letzten Alarm (normal, wenn er etwas anderes betraf)."


def check_hints(context, field_key: str, target: str, is_regex: bool) -> list:
    """Hinweise nach dem Anlegen eines Abos. Das Abo wird in jedem Fall angelegt,
    die Hinweise sind nur dazu da, Tippfehler aufzudecken."""
    hints = []
    entry, side = _find_entry(context, field_key)
    if entry and entry.get("add"):
        hints.append(_known_hint(_db_path(context), entry, side, target, is_regex))
    hints.append(_last_alarm_hint(context, field_key, target, is_regex))
    return [h for h in hints if h]


# ---------------------------------------------------------------- Start, lastraw

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


# ---------------------------------------------------------------- Abonnieren

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
        db_path = _db_path(context)
        chat_id = update.effective_chat.id

        if not context.args:
            text, placeholder = _for_prompt(entry)
            return await _ask(update, context, text, placeholder, ("for", field))

        raw = " ".join(context.args).strip()
        name = None
        try:
            if match == "exact" and not has_wildcard_syntax(raw):
                target, is_regex = raw, False
                if entry.get("add"):
                    name = knowledge.resolve_name(entry, db_path, raw)
                alias = name or f"{label}: {raw}"
            else:
                target, is_regex = wildcard_to_regex(raw), True
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


async def _subscribe_row(reply, context, chat_id: int, entry: dict, row: dict) -> None:
    """Abonniert den 'for'-Wert (z.B. die RIC) zu einem ausgewählten Namen."""
    db.add_sub(_db_path(context), chat_id, entry["for"], row["for"], row["add"], row["isRegex"])
    kind = "Muster" if row["isRegex"] else entry["label"]
    detail = f"{html.escape(kind)}: <code>{html.escape(row['for'])}</code>"
    await reply(_confirmation("✅ Abonniert:", row["add"], [], detail), parse_mode="HTML")


async def _create_name_pattern(reply, context, chat_id: int, entry: dict, raw: str) -> None:
    """Legt ein Muster auf das 'add'-Feld an (z.B. alle Beschreibungen mit RTW)."""
    add_key, add_label = entry["add"], entry["add_label"]
    try:
        regex = wildcard_to_regex(raw)
    except re.error as e:
        return await reply(f"❌ Ungültiges Muster: {html.escape(str(e))}")
    alias = f"{add_label}: {raw}"
    db.add_sub(_db_path(context), chat_id, add_key, regex, alias, True)
    hints = check_hints(context, add_key, regex, True)
    await reply(_confirmation("✅ Filter angelegt:", alias, hints), parse_mode="HTML")


def _pattern_button(entry: dict, text: str) -> list:
    return [InlineKeyboardButton(text, callback_data=f"pat:{entry['for']}")]


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

    text = f"🔍 Treffer {start_idx+1}-{min(end_idx, len(results))} von {len(results)}:"
    await reply(text, reply_markup=InlineKeyboardMarkup(keyboard))


def make_add_handler(entry: dict):
    """Befehl für die Namenssuche (z.B. /description): sucht in der Liste und den
    gelernten Namen und abonniert den zugehörigen 'for'-Wert. Mit Wildcard wird
    direkt ein Muster auf den Namen angelegt."""
    for_key, add_label, cmd = entry["for"], entry["add_label"], entry["add_command"]

    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        db_path = _db_path(context)
        chat_id = update.effective_chat.id
        reply = update.message.reply_text

        if not context.args:
            text = (
                f"{PROMPT_MARKER} <b>{html.escape(add_label)}</b>: Name oder Muster eingeben.\n{PROMPT_HOW}\n"
                f"• <code>/{cmd} Name</code> – in der Liste suchen\n"
                f"• <code>/{cmd} *Teil*</code> – Muster (Platzhalter <code>*</code>)"
            )
            return await _ask(update, context, text, "Name oder Muster, z.B. *wagen*", ("add", for_key))

        raw = " ".join(context.args).strip()
        if has_wildcard_syntax(raw):
            return await _create_name_pattern(reply, context, chat_id, entry, raw)

        rows = knowledge.known_rows(db_path, entry)
        term = raw.lower()
        context.user_data[f"term:{for_key}"] = raw

        exact = next((r for r in rows if r["add"].lower() == term), None)
        if exact:
            return await _subscribe_row(reply, context, chat_id, entry, exact)

        results = [r for r in rows if term in r["add"].lower()]
        if not results:
            hint = "" if rows else "\nDie Liste ist noch leer, sie füllt sich mit den ersten Alarmen."
            return await reply(
                f"❌ Keine Treffer für „{html.escape(raw)}“ in der Liste.{hint}",
                reply_markup=InlineKeyboardMarkup([_pattern_button(entry, "🔎 Trotzdem als Muster anlegen")]),
                parse_mode="HTML",
            )
        if len(results) == 1:
            return await _subscribe_row(reply, context, chat_id, entry, results[0])

        context.user_data[f"search:{for_key}"] = results
        await send_page(reply, for_key, results, 0, raw)

    return handler


# ---------------------------------------------------------------- Abos verwalten

def _with_label(field_label: str, alias: str) -> str:
    """'Feld: Name' für die Anzeige. Muster-Abos tragen das Feld schon im Namen
    ('Alarmstichwort: THL*'), dann wird es nicht noch einmal vorangestellt."""
    prefix = f"{field_label}: "
    return alias if alias.startswith(prefix) else prefix + alias


@restricted
async def abo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    subs = db.get_chat_subs(_db_path(context), chat.id)

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
        admin_db_path = _db_path(context)
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
                await query.edit_message_text(f"✅ Freigeschaltet: {uid}")
                await _try_send(context, uid, "✅ Du wurdest freigeschaltet. Schreibe /start für eine Übersicht.")
            else:
                db.set_user_status(admin_db_path, uid, "denied")
                await query.edit_message_text(f"❌ Abgelehnt: {uid}")
        else:  # usr:del
            db.remove_user(admin_db_path, uid)
            db.remove_chat_subs(admin_db_path, uid)  # private Abos des Users entfernen
            await query.edit_message_text(f"🗑️ Zugriff entzogen: {uid} (inkl. seiner privaten Abos)")
        return

    await query.answer()
    data = query.data

    if data == "close_menu":
        return await query.edit_message_text("👍 Menü geschlossen.")

    if data.startswith("del:"):
        sub_id = int(data.split(":", 1)[1])
        db.remove_sub_by_id(_db_path(context), chat_id, sub_id)
        return await query.edit_message_text("🗑️ Abonnement entfernt.")

    if data.startswith(("page:", "add:", "pat:")):
        kind, for_key, *rest = data.split(":", 2)
        entry = _entry_by_for(context, for_key)
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
            return await _subscribe_row(query.edit_message_text, context, chat_id, entry, results[idx])

        # pat: Suchbegriff als Muster auf den Namen anlegen
        if not term:
            return await query.edit_message_text(_EXPIRED)
        return await _create_name_pattern(query.edit_message_text, context, chat_id, entry, term)


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
        return await _ask(update, context, text, "ID und optional Name", ("adduser", ""))
    try:
        uid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Ungültige ID, erwartet wird eine Zahl.")

    if is_admin(context, uid):
        return await update.message.reply_text("ℹ️ Diese ID ist bereits Admin.")

    name = " ".join(context.args[1:]).strip() or None
    db.set_user_status(db_path, uid, "allowed", name)
    await update.message.reply_text(f"✅ Freigeschaltet: <code>{uid}</code>", parse_mode="HTML")
    await _try_send(context, uid, "✅ Du wurdest freigeschaltet. Schreibe /start für eine Übersicht.")
