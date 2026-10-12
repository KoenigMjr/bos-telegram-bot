import unittest

from bos_telegram_bot.core import sending


class DefaultsTests(unittest.TestCase):
    def test_the_agreed_defaults(self):
        self.assertEqual(sending.DEFAULTS, {
            "min_interval_seconds": 1.0, "max_per_second": 25.0, "attempts": 3,
            "retry_pause_seconds": [2.0, 5.0], "max_wait_seconds": 120.0, "timeout_seconds": 15.0})

    def test_the_defaults_themselves_are_valid(self):
        self.assertEqual(sending.problems(dict(sending.DEFAULTS)), [])

    def test_an_empty_block_is_valid_and_gives_the_defaults(self):
        self.assertEqual(sending.problems({}), [])
        self.assertEqual(sending.normalized({}), sending.DEFAULTS)


class ValidationTests(unittest.TestCase):
    def check(self, key, good, bad):
        for value in good:
            self.assertEqual(sending.problems({key: value}), [], f"{key}={value!r} sollte gültig sein")
        for value in bad:
            (problem,) = sending.problems({key: value})
            self.assertIn(f"sending.{key} muss", problem, f"{key}={value!r}")
            self.assertIn(f"(ist '{value}')", problem)

    def test_min_interval(self):
        self.check("min_interval_seconds", [0, 0.5, 1, 3.5], [-1, "viel", True, None, [1]])

    def test_max_per_second(self):
        self.check("max_per_second", [1, 25, 30.5], [0, -5, "schnell", False])

    def test_attempts(self):
        self.check("attempts", [1, 3, 10], [0, 11, -1, 2.5, "3", True])

    def test_max_wait(self):
        self.check("max_wait_seconds", [0, 60, 120.5], [-1, "lang", True])

    def test_timeout(self):
        self.check("timeout_seconds", [0.5, 15, 60], [0, -1, "x", False])

    def test_pauses_are_a_list_of_numbers_or_one_number(self):
        self.check("retry_pause_seconds", [[2, 5], [0], [1.5, 3, 10], 3, 0.5], [[], [1, -1], ["a"], [True], -2, "x", [[1]]])

    def test_unknown_keys_are_named_with_the_valid_ones(self):
        (problem,) = sending.problems({"versuche": 5})
        self.assertIn("sending.versuche ist unbekannt", problem)
        for key in sending.DEFAULTS:
            self.assertIn(key, problem)

    def test_everything_wrong_is_reported_at_once(self):
        problems = sending.problems({"attempts": 0, "max_per_second": -1, "foo": 1})
        self.assertEqual(len(problems), 3)

    def test_the_block_itself_must_be_a_block(self):
        for bad in (5, "x", [1], None, True):
            (problem,) = sending.problems(bad)
            self.assertIn("sending muss ein Block sein", problem)


class NormalizedTests(unittest.TestCase):
    def test_missing_values_come_from_the_defaults(self):
        result = sending.normalized({"attempts": 5})
        self.assertEqual(result["attempts"], 5)
        self.assertEqual(result["min_interval_seconds"], 1.0)
        self.assertEqual(result["retry_pause_seconds"], [2.0, 5.0])

    def test_numbers_become_floats_and_attempts_an_int(self):
        result = sending.normalized({"min_interval_seconds": 2, "max_wait_seconds": 60, "timeout_seconds": 20, "max_per_second": 10})
        for key in ("min_interval_seconds", "max_wait_seconds", "timeout_seconds", "max_per_second"):
            self.assertIsInstance(result[key], float, key)
        self.assertIsInstance(result["attempts"], int)

    def test_a_single_pause_becomes_a_list(self):
        self.assertEqual(sending.normalized({"retry_pause_seconds": 4})["retry_pause_seconds"], [4.0])

    def test_a_list_of_pauses_is_kept_in_order(self):
        self.assertEqual(sending.normalized({"retry_pause_seconds": [1, 7, 30]})["retry_pause_seconds"], [1.0, 7.0, 30.0])

    def test_the_defaults_are_not_changed_by_normalizing(self):
        before = {k: (list(v) if isinstance(v, list) else v) for k, v in sending.DEFAULTS.items()}
        result = sending.normalized({})
        result["retry_pause_seconds"].append(99.0)
        self.assertEqual(sending.DEFAULTS, before)

    def test_a_non_block_gives_the_defaults(self):
        self.assertEqual(sending.normalized(None), sending.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
