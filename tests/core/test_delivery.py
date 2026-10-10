import unittest

from bos_telegram_bot.core.delivery import DEFAULT_SECONDS, MAX_ALARMS, DeliveryMemory, alarm_key
from tests.helpers import Clock


class AlarmKeyTests(unittest.TestCase):
    def test_multicast_key_is_the_timestamp_list(self):
        payload = {"timestamp": "1700000000.5", "timestamp_list": "1700000000.5, 1700000000.9"}
        self.assertEqual(alarm_key(payload), "1700000000.5,1700000000.9")

    def test_single_alarm_without_list_uses_the_timestamp(self):
        self.assertEqual(alarm_key({"timestamp": "1700000000.5"}), "1700000000.5")

    def test_single_alarm_with_a_one_entry_list_matches_the_plain_timestamp(self):
        self.assertEqual(alarm_key({"timestamp": "1.5", "timestamp_list": "1.5"}), alarm_key({"timestamp": "1.5"}))

    def test_extra_spaces_around_the_entries_do_not_matter(self):
        self.assertEqual(alarm_key({"timestamp_list": "1.5,  2.5 "}), "1.5,2.5")
        self.assertEqual(alarm_key({"timestamp_list": "1.5, 2.5"}), "1.5,2.5")

    def test_original_and_copy_from_home_assistant_have_the_same_key(self):
        original = {"timestamp": "1.5", "ric": "1000011", "timestamp_list": "1.5, 2.5", "message": "x"}
        copy = dict(original, republished=True)
        self.assertEqual(alarm_key(original), alarm_key(copy))

    def test_both_recipients_of_a_multicast_share_the_key(self):
        merged = {"timestamp_list": "1.5, 2.5"}
        first = dict(merged, ric="1000011", timestamp="1.5", multicastRecipientIndex="1")
        second = dict(merged, ric="1000043", timestamp="2.5", multicastRecipientIndex="2")
        self.assertEqual(alarm_key(first), alarm_key(second))

    def test_different_alarms_have_different_keys(self):
        self.assertNotEqual(alarm_key({"timestamp": "1.5"}), alarm_key({"timestamp": "1.6"}))

    def test_no_timestamp_means_no_key(self):
        for payload in ({}, {"ric": "1"}, {"timestamp": ""}, {"timestamp": None, "timestamp_list": ""}):
            self.assertIsNone(alarm_key(payload), payload)

    def test_the_key_is_a_string_even_for_numbers(self):
        self.assertEqual(alarm_key({"timestamp": 1700000000.5}), "1700000000.5")


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.memory = DeliveryMemory(ttl_seconds=300, clock=self.clock)

    def test_first_time_is_new_and_repeats_are_not(self):
        self.assertTrue(self.memory.register("a"))
        self.assertFalse(self.memory.register("a"))
        self.assertFalse(self.memory.register("a"))
        self.assertTrue(self.memory.register("b"))

    def test_knows_does_not_register(self):
        self.assertFalse(self.memory.knows("a"))
        self.assertFalse(self.memory.knows("a"))
        self.memory.register("a")
        self.assertTrue(self.memory.knows("a"))

    def test_without_a_key_everything_is_new_and_nothing_is_known(self):
        self.assertTrue(self.memory.register(None))
        self.assertTrue(self.memory.register(None))
        self.assertFalse(self.memory.knows(None))
        self.memory.mark_delivered(None, 1)
        self.assertFalse(self.memory.is_delivered(None, 1))

    def test_an_entry_expires_after_the_time_is_up(self):
        self.memory.register("a")
        self.clock.advance(299)
        self.assertTrue(self.memory.knows("a"))
        self.clock.advance(1)
        self.assertFalse(self.memory.knows("a"))
        self.assertTrue(self.memory.register("a"))         # nach Ablauf wieder neu

    def test_a_repeat_does_not_extend_the_time(self):
        self.memory.register("a")
        self.clock.advance(200)
        self.assertFalse(self.memory.register("a"))
        self.clock.advance(101)                            # 301 s nach dem ersten Eintreffen
        self.assertTrue(self.memory.register("a"))

    def test_only_expired_entries_disappear(self):
        self.memory.register("alt")
        self.clock.advance(200)
        self.memory.register("neu")
        self.clock.advance(150)                            # alt: 350 s, neu: 150 s
        self.assertFalse(self.memory.knows("alt"))
        self.assertTrue(self.memory.knows("neu"))

    def test_the_oldest_alarm_is_dropped_first_when_full(self):
        memory = DeliveryMemory(ttl_seconds=300, max_alarms=3, clock=self.clock)
        for key in ("a", "b", "c", "d"):
            memory.register(key)
            self.clock.advance(1)
        self.assertEqual([memory.knows(k) for k in ("a", "b", "c", "d")], [False, True, True, True])

    def test_defaults(self):
        self.assertEqual(DEFAULT_SECONDS, 300)
        self.assertGreaterEqual(MAX_ALARMS, 100)
        self.assertEqual(DeliveryMemory().ttl, 300)


class DeliveredTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.memory = DeliveryMemory(ttl_seconds=300, clock=self.clock)
        self.memory.register("a")

    def test_delivery_is_tracked_per_chat(self):
        self.memory.mark_delivered("a", 11)
        self.assertTrue(self.memory.is_delivered("a", 11))
        self.assertFalse(self.memory.is_delivered("a", 22))

    def test_delivery_is_tracked_per_alarm(self):
        self.memory.register("b")
        self.memory.mark_delivered("a", 11)
        self.assertFalse(self.memory.is_delivered("b", 11))

    def test_unknown_alarms_are_not_delivered_and_marking_them_does_nothing(self):
        self.memory.mark_delivered("unbekannt", 11)
        self.assertFalse(self.memory.is_delivered("unbekannt", 11))
        self.assertFalse(self.memory.knows("unbekannt"))

    def test_delivery_is_forgotten_with_the_alarm(self):
        self.memory.mark_delivered("a", 11)
        self.clock.advance(300)
        self.assertFalse(self.memory.is_delivered("a", 11))
        self.memory.register("a")
        self.assertFalse(self.memory.is_delivered("a", 11))

    def test_group_chats_with_negative_ids(self):
        self.memory.mark_delivered("a", -1001234567890)
        self.assertTrue(self.memory.is_delivered("a", -1001234567890))


if __name__ == "__main__":
    unittest.main()
