"""Schützt die Tests selbst. Zwei Fehlerarten lassen eine Testsuite still grün bleiben:

1. Eine Testklasse überschreibt eine eingebaute Methode von unittest. Beispiel: eine
   Hilfsfunktion namens 'fail' wird von assertEqual(text, text), assertIn und den
   Listenvergleichen aufgerufen, wenn sie fehlschlagen. Der Fehler geht dann verloren.
2. Eine Testmethode ist in derselben Klasse zweimal definiert. Die zweite ersetzt die erste,
   die erste läuft nie."""
import ast
import glob
import importlib
import inspect
import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
# Diese Methoden sind zum Überschreiben gedacht.
OVERRIDABLE = {"setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp", "asyncTearDown"}


def overridden_methods(cls) -> list:
    """Eingebaute unittest-Methoden, die 'cls' selbst neu definiert (außer den dafür gedachten)."""
    builtin = set(dir(unittest.IsolatedAsyncioTestCase))
    return [attr for attr, value in vars(cls).items()
            if attr in builtin and attr not in OVERRIDABLE and not attr.startswith("test") and callable(value)]


def test_modules():
    for path in sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py"))):
        yield os.path.basename(path)[:-3], path


class TestSuiteIntegrityTests(unittest.TestCase):
    def test_no_test_class_overrides_a_unittest_method(self):
        problems = []
        for name, _ in test_modules():
            module = importlib.import_module(f"tests.{name}")
            for cls_name, cls in inspect.getmembers(module, inspect.isclass):
                if not issubclass(cls, unittest.TestCase) or cls.__module__ != module.__name__:
                    continue
                problems += [f"{module.__name__}.{cls_name}.{attr}" for attr in overridden_methods(cls)]
        self.assertEqual(problems, [], "überschreibt eine eingebaute unittest-Methode")

    def test_no_test_method_is_defined_twice(self):
        problems = []
        for name, path in test_modules():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                seen = set()
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in seen:
                            problems.append(f"{name}.{node.name}.{item.name}")
                        seen.add(item.name)
        self.assertEqual(problems, [], "doppelt definiert, nur die letzte läuft")

    def test_the_guard_itself_detects_an_override(self):
        class Broken(unittest.TestCase):
            def fail(self, *args):      # genau der Fehler von früher
                return None

        class Fine(unittest.TestCase):
            def setUp(self):            # gewollt
                pass

            def helper(self):
                pass

        self.assertEqual(overridden_methods(Broken), ["fail"])
        self.assertEqual(overridden_methods(Fine), [])


if __name__ == "__main__":
    unittest.main()
