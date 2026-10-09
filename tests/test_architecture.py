"""Hält die Schichtenregeln der Paketstruktur ein.

core    kennt weder Telegram noch MQTT noch die Datenbank
storage kennt kein Telegram und kein MQTT
mqtt    nutzt core und storage, kennt chat nicht
chat    nutzt core und storage, kennt mqtt nicht
config  nutzt nur core"""
import ast
import glob
import os
import unittest

PACKAGE = "bos_telegram_bot"
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), PACKAGE)

# erlaubte Pakete aus dem eigenen Projekt und verbotene Fremdpakete je Schicht
ALLOWED_INTERNAL = {
    "core": {"core"},
    "storage": {"core", "storage"},
    "mqtt": {"core", "storage", "mqtt"},
    "chat": {"core", "storage", "chat"},
}
FORBIDDEN_EXTERNAL = {
    "core": {"telegram", "aiomqtt", "sqlite3"},
    "storage": {"telegram", "aiomqtt"},
    "mqtt": {"telegram"},
    "chat": {"aiomqtt"},
}


def imports_of(path):
    """Alle importierten Modulnamen einer Datei, auch innerhalb von Funktionen."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            for alias in node.names:               # 'from bos_telegram_bot import config'
                yield f"{node.module}.{alias.name}"


def layer_of(module):
    parts = module.split(".")
    return parts[1] if parts[0] == PACKAGE and len(parts) > 1 else None


class LayerRuleTests(unittest.TestCase):
    def test_every_layer_follows_its_import_rules(self):
        problems = []
        for layer in ALLOWED_INTERNAL:
            for path in sorted(glob.glob(os.path.join(ROOT, layer, "*.py"))):
                for module in imports_of(path):
                    name = os.path.relpath(path, ROOT)
                    top = module.split(".")[0]
                    if top == PACKAGE:
                        target = layer_of(module)
                        if target in ALLOWED_INTERNAL and target not in ALLOWED_INTERNAL[layer]:
                            problems.append(f"{name}: darf {module} nicht importieren")
                        if target is None and module not in (PACKAGE, f"{PACKAGE}.config"):
                            problems.append(f"{name}: darf {module} nicht importieren")
                    elif top in FORBIDDEN_EXTERNAL[layer]:
                        problems.append(f"{name}: darf {top} nicht importieren")
        self.assertEqual(problems, [])

    def test_config_only_uses_core(self):
        problems = [m for m in imports_of(os.path.join(ROOT, "config.py"))
                    if m.startswith(PACKAGE) and layer_of(m) not in ("core", None)]
        self.assertEqual(problems, [])

    def test_the_rules_would_catch_a_violation(self):
        # core importiert die Datenbank: genau das muss auffallen
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.py")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("from bos_telegram_bot.storage import database\nimport telegram\n")
            found = list(imports_of(bad))
        self.assertIn("bos_telegram_bot.storage", found)
        self.assertIn("telegram", found)
        self.assertNotIn(layer_of("bos_telegram_bot.storage.database"), ALLOWED_INTERNAL["core"])


if __name__ == "__main__":
    unittest.main()
