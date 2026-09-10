import csv
import html
import os
import re
from functools import wraps
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db

ITEMS_PER_PAGE = 5

# Alles unterhalb dieses Ordners "gehört" dem Bot - nur dort dürfen fehlende
# CSVs automatisch angelegt werden. Alles außerhalb (z.B. ein reingemountetes
# BOSWatch3-Verzeichnis) wird ausschließlich gelesen, niemals beschrieben.
OWN_DATA_DIR = Path("data").resolve()


def ensure_csv_exists(csv_path: str) -> None:
    """Legt eine fehlende CSV im BOSWatch3-Descriptor-Schema NUR an, wenn der
    Pfad innerhalb des eigenen data/-Ordners liegt. Zeigt csv_path auf einen
    fremden Ordner (z.B. ein gemountetes BOSWatch3-Verzeichnis) und existiert
    die Datei dort nicht, wird NICHTS geschrieben - nur eine Warnung geloggt."""
    resolved = Path(csv_path).resolve()
    if resolved.exists():
        return

    if OWN_DATA_DIR == resolved.parent or OWN_DATA_DIR in resolved.parents:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8", newline="") as f:
            f.write("for,add,isRegex\n")
        print(f"[CSV] Neu angelegt (eigener Ordner): {resolved}")
    else:
        print(
            f"[CSV] WARNUNG: '{resolved}' existiert nicht und liegt außerhalb von "
            f"'{OWN_DATA_DIR}' - wird NICHT automatisch angelegt (kein Schreiben in "
            f"fremde Programmordner). Das betroffene Feld liefert bis dahin keine Treffer."
        )


def load_csv(csv_path: str):
    """Lädt eine CSV im BOSWatch3-Descriptor-Schema (for,add,isRegex).
    Parsing bewusst identisch zu module/descriptor.py in BW3-Core gehalten,
    damit dieselbe Datei ohne Anpassung von beiden Systemen gelesen werden
    kann (inkl. Toleranz für fehlende isRegex-Spalte, siehe BW3-Doku)."""
    if not os.path.isfile(csv_path):
        print(f"[CSV] Datei nicht gefunden, Feld liefert bis dahin keine Treffer: {csv_path}")
        return []

    csv_data = []
    with open(csv_path, "r", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row_num, row in enumerate(reader, start=2):  # Zeile 1 = Header
            raw_for = str(row.get("for", "")).strip()
            clean_for = raw_for.strip().strip('"').strip("'")
            if not clean_for:
                continue

            is_regex = (row.get("isRegex") or "false").strip().lower() == "true"
            if is_regex:
                try:
                    re.compile(clean_for)
                except re.error as e:
                    print(f"[CSV] {csv_path} Zeile {row_num} übersprungen (ungültiger Regex '{clean_for}'): {e}")
                    continue

            csv_data.append({
                "for": clean_for,
                "add": (row.get("add") or "").strip(),
                "isRegex": is_regex,
            })
    return csv_data


def wildcard_to_regex(pattern: str) -> str:
    """Wandelt ein Glob/SQL-Wildcard-Muster in eine anchored Regex um.

    - '*' und '%'  -> beliebig viele Zeichen
    - '?' und '_'  -> genau ein Zeichen
    - 're:'-Präfix -> Rest wird als rohe Regex verwendet (Poweruser, kein
      automatisches Anchoring - der User steuert ^/$ selbst)
    - kein Wildcard-Zeichen vorhanden -> implizite Contains-Suche
      (z.B. '/message THL' matcht jede Nachricht, die 'THL' enthält)

    Wirft re.error, wenn das Ergebnis kein gültiger regulärer Ausdruck ist.
    """
    pattern = pattern.strip()
    if pattern.lower().startswith("re:"):
        raw = pattern[3:]
        re.compile(raw)
        return raw

    has_wildcard = any(ch in pattern for ch in "*%?_")
    star_token, dot_token = "WCSTARTOKEN", "WCDOTTOKEN"
    tmp = pattern.replace("*", star_token).replace("%", star_token)
    tmp = tmp.replace("?", dot_token).replace("_", dot_token)
    escaped = re.escape(tmp).replace(re.escape(star_token), ".*").replace(re.escape(dot_token), ".")
    regex = f"^{escaped}$" if has_wildcard else f".*{escaped}.*"
    re.compile(regex)
    return regex


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


@restricted
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["👋 <b>BOS-Alarm-Bot aktiv!</b>", ""]
    for field_key, field_cfg in context.bot_data["fields"].items():
        cmd = context.bot_data["field_commands"][field_key]
        if field_cfg["mode"] == "lookup":
            lines.append(f"• <code>/{cmd} &lt;suchbegriff&gt;</code> – {html.escape(field_cfg['label'])} abonnieren")
        else:
            lines.append(f"• <code>/{cmd} &lt;muster&gt;</code> – {html.escape(field_cfg['label'])} filtern (z.B. THL*)")
    lines.append("")
    lines.append("• <code>/abo</code> – Aktive Abonnements anzeigen &amp; verwalten")
    lines.append("• <code>/lastraw</code> – Letztes empfangenes Alarm-JSON anzeigen")
    if update.effective_chat.type != "private":
        lines.append("")
        lines.append("ℹ️ In dieser Gruppe gesetzte Abos gelten für alle Mitglieder dieser Gruppe.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@restricted
async def lastraw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json

    payload = context.bot_data.get("last_payload")
    if not payload:
        return await update.message.reply_text("Es wurde noch kein Alarm empfangen.")
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(text) > 3500:
        text = text[:3500] + "\n… (gekürzt)"
    await update.message.reply_text(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")


async def send_page(reply_method, field_key, results, page, display_column):
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_items = results[start_idx:end_idx]

    keyboard = [
        [InlineKeyboardButton(f"➕ {item[display_column]}", callback_data=f"add:{field_key}:{item['for']}")]
        for item in page_items
    ]

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Zurück", callback_data=f"page:{field_key}:{page-1}"))
    if end_idx < len(results):
        nav_buttons.append(InlineKeyboardButton("Weiter ➡️", callback_data=f"page:{field_key}:{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    text = f"🔍 Treffer {start_idx+1}-{min(end_idx, len(results))} von {len(results)}:"
    await reply_method(text, reply_markup=InlineKeyboardMarkup(keyboard))


def make_lookup_handler(field_key: str, field_cfg: dict):
    """Erzeugt einen Handler, der die konfigurierte CSV in search_column
    durchsucht und bei Auswahl target_column als Abo-Ziel speichert."""

    search_column = field_cfg["search_column"]
    target_column = field_cfg["target_column"]
    display_column = field_cfg["display_column"]
    label = field_cfg["label"]

    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        config = context.bot_data["config"]
        db_path = config["files"]["db_path"]
        csv_data = context.bot_data["csv_data"].get(field_key, [])
        chat_id = update.effective_chat.id

        if not context.args:
            return await update.message.reply_text(
                f"Bitte Suchbegriff für <b>{html.escape(label)}</b> angeben: "
                f"<code>/{context.bot_data['field_commands'][field_key]} &lt;Suchbegriff&gt;</code>",
                parse_mode="HTML",
            )

        if not csv_data:
            return await update.message.reply_text(
                f"❌ Für <b>{html.escape(label)}</b> ist aktuell keine Datenquelle verfügbar "
                f"(CSV fehlt oder ist leer).",
                parse_mode="HTML",
            )

        query = " ".join(context.args).strip()

        # 1. Direkt-Eingabe (exakter Wert in der Such-Spalte)
        exact_match = next((item for item in csv_data if item[search_column].lower() == query.lower()), None)
        if exact_match:
            db.add_sub(
                db_path, chat_id, field_key, exact_match[target_column], exact_match[display_column],
                exact_match["isRegex"] if target_column == "for" else False,
            )
            return await update.message.reply_text(
                f"✅ Direkt abonniert:\n<b>{html.escape(exact_match[display_column])}</b>",
                parse_mode="HTML",
            )

        # 2. Substring-Suche in der Such-Spalte
        results = [item for item in csv_data if query.lower() in item[search_column].lower()]

        if not results:
            return await update.message.reply_text(f"❌ Keine Treffer für „{html.escape(query)}“.", parse_mode="HTML")

        # 3. Smart Single-Match
        if len(results) == 1:
            item = results[0]
            db.add_sub(
                db_path, chat_id, field_key, item[target_column], item[display_column],
                item["isRegex"] if target_column == "for" else False,
            )
            return await update.message.reply_text(
                f"🎯 Eindeutiger Treffer, automatisch abonniert:\n<b>{html.escape(item[display_column])}</b>",
                parse_mode="HTML",
            )

        # 4. Multi-Match-Auswahl
        context.user_data[f"search_results:{field_key}"] = results
        await send_page(update.message.reply_text, field_key, results, 0, display_column)

    return handler


def make_pattern_handler(field_key: str, field_cfg: dict):
    """Erzeugt einen Handler für freie Muster-Filter (z.B. /message THL*),
    ohne CSV-Lookup - matcht direkt gegen den ankommenden Feldwert."""

    label = field_cfg["label"]

    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        config = context.bot_data["config"]
        db_path = config["files"]["db_path"]
        chat_id = update.effective_chat.id
        cmd = context.bot_data["field_commands"][field_key]

        if not context.args:
            return await update.message.reply_text(
                f"Bitte Muster für <b>{html.escape(label)}</b> angeben.\n"
                f"Beispiele: <code>/{cmd} THL*</code>, <code>/{cmd} *Muster*</code>, "
                f"<code>/{cmd} re:^RD\\s?\\d</code>",
                parse_mode="HTML",
            )

        raw_pattern = " ".join(context.args).strip()
        try:
            regex = wildcard_to_regex(raw_pattern)
        except re.error as e:
            return await update.message.reply_text(f"❌ Ungültiges Muster: {html.escape(str(e))}")

        alias = f"{label}: {raw_pattern}"
        db.add_sub(db_path, chat_id, field_key, regex, alias, True)
        await update.message.reply_text(
            f"✅ Filter angelegt:\n<b>{html.escape(alias)}</b>", parse_mode="HTML"
        )

    return handler


@restricted
async def abo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = context.bot_data["config"]
    chat = update.effective_chat
    subs = db.get_chat_subs(config["files"]["db_path"], chat.id)

    if not subs:
        if chat.type == "private":
            return await update.message.reply_text("Du hast aktuell keine Abonnements.")
        return await update.message.reply_text("Diese Gruppe hat aktuell keine Abonnements.")

    fields_cfg = context.bot_data["fields"]
    header = "📋 <b>Deine aktiven Abonnements:</b>" if chat.type == "private" \
        else f"📋 <b>Aktive Abonnements dieser Gruppe ({html.escape(chat.title or '')}):</b>"
    lines = [header]
    keyboard = []
    for sub in subs:
        field_label = fields_cfg.get(sub["field"], {}).get("label", sub["field"])
        lines.append(f"• [{html.escape(field_label)}] {html.escape(sub['alias'])}")
        keyboard.append(
            [InlineKeyboardButton(f"🗑️ {field_label}: {sub['alias'][:22]}", callback_data=f"del:{sub['id']}")]
        )

    keyboard.append([InlineKeyboardButton("✔️ Alles passt", callback_data="close_menu")])
    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )


@restricted
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id

    # Admin-Aktionen: Anfragen freigeben/ablehnen, User entfernen.
    if query.data.startswith(("acc:", "usr:")):
        if not is_admin(context, query.from_user.id):
            return await query.answer("⛔ Nur für Admins.", show_alert=True)
        await query.answer()
        admin_db_path = context.bot_data["config"]["files"]["db_path"]
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
    config = context.bot_data["config"]
    db_path = config["files"]["db_path"]
    fields_cfg = context.bot_data["fields"]

    if data == "close_menu":
        return await query.edit_message_text("👍 Menü geschlossen.")

    if data.startswith("page:"):
        _, field_key, page_str = data.split(":", 2)
        page = int(page_str)
        results = context.user_data.get(f"search_results:{field_key}", [])
        if results:
            display_column = fields_cfg[field_key]["display_column"]
            await send_page(query.edit_message_text, field_key, results, page, display_column)
        return

    if data.startswith("add:"):
        _, field_key, item_key = data.split(":", 2)
        field_cfg = fields_cfg[field_key]
        csv_data = context.bot_data["csv_data"].get(field_key, [])
        item = next((i for i in csv_data if i["for"] == item_key), None)
        if item:
            target_column = field_cfg["target_column"]
            display_column = field_cfg["display_column"]
            db.add_sub(
                db_path, chat_id, field_key, item[target_column], item[display_column],
                item["isRegex"] if target_column == "for" else False,
            )
            await query.edit_message_text(
                f"✅ Erfolgreich abonniert:\n<b>{html.escape(item[display_column])}</b>", parse_mode="HTML"
            )
        return

    if data.startswith("del:"):
        sub_id = int(data.split(":", 1)[1])
        db.remove_sub_by_id(db_path, chat_id, sub_id)
        await query.edit_message_text("🗑️ Abonnement entfernt.")


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
        return await update.message.reply_text(
            "Bitte Telegram-ID angeben, z.B. <code>/adduser 123456789 Max Mustermann</code>",
            parse_mode="HTML",
        )
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
