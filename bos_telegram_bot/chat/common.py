"""Kleine Hilfen, die mehrere Befehls-Module brauchen."""


def get_db_path(context) -> str:
    return context.bot_data["config"]["files"]["db_path"]


def entry_by_for(context, for_key: str):
    return next((e for e in context.bot_data["entries"] if e["for"] == for_key), None)


def find_entry(context, field_key: str):
    """Eintrag und Seite ('for' oder 'add'), zu der ein Feldschlüssel gehört."""
    for entry in context.bot_data["entries"]:
        if entry["for"] == field_key:
            return entry, "for"
        if entry.get("add") == field_key:
            return entry, "add"
    return None, None
