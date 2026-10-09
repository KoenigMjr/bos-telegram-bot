import sqlite3


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,  -- Telegram chat_id: privater Chat ODER Gruppe/Supergruppe.
                                        -- Für private Chats ist das identisch mit der User-ID,
                                        -- für Gruppen eine eigene (negative) ID - dadurch bekommt
                                        -- jede Gruppe automatisch ihre eigenen, unabhängigen Abos.
            field TEXT NOT NULL,       -- Feld-Schlüssel aus config.yaml (z.B. 'ric', 'message')
            target TEXT NOT NULL,      -- exakter Wert, CSV-ID oder fertige Regex
            alias TEXT NOT NULL,       -- Anzeigetext, ggf. mit \\1-Platzhaltern
            is_regex BOOLEAN NOT NULL,
            UNIQUE (chat_id, field, target)
        )
        """
    )
    # Beschleunigt den Alarm-Fanout: pro Feld werden alle Subs auf einmal geholt.
    c.execute("CREATE INDEX IF NOT EXISTS idx_field ON subscriptions(field)")
    # Per Bot verwaltete User (zusätzlich zu den Admins aus ADMIN_USERS).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,      -- 'allowed' | 'pending' | 'denied'
            name TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Aus Alarmen gelernte Zuordnungen (z.B. RIC -> Beschreibung), siehe knowledge.py.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS learned_pairs (
            for_field TEXT NOT NULL,
            for_value TEXT NOT NULL,
            add_value TEXT NOT NULL,
            last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (for_field, for_value)
        )
        """
    )
    conn.commit()
    conn.close()


def add_sub(db_path: str, chat_id: int, field: str, target: str, alias: str, is_regex: bool) -> None:
    conn = get_connection(db_path)
    c = conn.cursor()
    c.execute(
        """
        INSERT OR IGNORE INTO subscriptions (chat_id, field, target, alias, is_regex)
        VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, field, target, alias, is_regex),
    )
    conn.commit()
    conn.close()


def remove_sub_by_id(db_path: str, chat_id: int, sub_id: int) -> None:
    conn = get_connection(db_path)
    c = conn.cursor()
    c.execute("DELETE FROM subscriptions WHERE id = ? AND chat_id = ?", (sub_id, chat_id))
    conn.commit()
    conn.close()


def get_chat_subs(db_path: str, chat_id: int):
    conn = get_connection(db_path)
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY field, alias", (chat_id,))
    subs = c.fetchall()
    conn.close()
    return subs


def get_all_subs(db_path: str):
    """Wird pro eintreffendem MQTT-Alarm aufgerufen -> in mqtt/dispatch via
    asyncio.to_thread ausführen, damit der Event-Loop nicht blockiert."""
    conn = get_connection(db_path)
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions")
    subs = c.fetchall()
    conn.close()
    return subs


def get_user_status(db_path: str, user_id: int):
    """Liefert 'allowed', 'pending', 'denied' oder None (unbekannt)."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["status"] if row else None


def set_user_status(db_path: str, user_id: int, status: str, name: str = None) -> None:
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO users (user_id, status, name) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            status = excluded.status,
            name = COALESCE(excluded.name, users.name),
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, status, name),
    )
    conn.commit()
    conn.close()


def list_users(db_path: str, status: str):
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM users WHERE status = ? ORDER BY name, user_id", (status,)
    ).fetchall()
    conn.close()
    return rows


def remove_user(db_path: str, user_id: int) -> None:
    conn = get_connection(db_path)
    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def remove_chat_subs(db_path: str, chat_id: int) -> None:
    """Löscht alle Abos eines Chats (z.B. beim Entzug des Zugriffs, damit der
    private Chat des Users keine Alarme mehr bekommt)."""
    conn = get_connection(db_path)
    conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def rename_field(db_path: str, old: str, new: str) -> int:
    """Benennt den Feldschlüssel bestehender Abos um (z.B. nach einer
    Umbenennung in der Konfiguration). Abos, die dadurch doppelt würden, werden
    verworfen. Liefert die Zahl der umbenannten Abos."""
    conn = get_connection(db_path)
    cur = conn.execute("UPDATE OR IGNORE subscriptions SET field = ? WHERE field = ?", (new, old))
    renamed = cur.rowcount
    conn.execute("DELETE FROM subscriptions WHERE field = ?", (old,))
    conn.commit()
    conn.close()
    return renamed


def upsert_learned(db_path: str, for_field: str, for_value: str, add_value: str) -> None:
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO learned_pairs (for_field, for_value, add_value) VALUES (?, ?, ?)
        ON CONFLICT(for_field, for_value) DO UPDATE SET
            add_value = excluded.add_value,
            last_seen = CURRENT_TIMESTAMP
        """,
        (for_field, for_value, add_value),
    )
    conn.commit()
    conn.close()


def list_learned(db_path: str, for_field: str):
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT for_value, add_value FROM learned_pairs WHERE for_field = ? ORDER BY add_value, for_value",
        (for_field,),
    ).fetchall()
    conn.close()
    return rows


def get_learned(db_path: str, for_field: str, for_value: str):
    """Liefert den gelernten Namen zu einem Wert oder None."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT add_value FROM learned_pairs WHERE for_field = ? AND for_value = ?",
        (for_field, for_value),
    ).fetchone()
    conn.close()
    return row["add_value"] if row else None
