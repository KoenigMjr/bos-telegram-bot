"""Der Versand: Warteschlange pro Chat, Abstände, Wiederholungen, Obergrenzen und Beenden.
Alles mit virtueller Zeit, die Zeitpunkte werden deshalb exakt geprüft."""
import asyncio
import datetime
import unittest

from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

from bos_telegram_bot.chat.sender import MAX_QUEUE, Sender
from tests.helpers import ScriptedBot, VirtualTime

LOGGER = "bos_telegram_bot.chat.sender"


class SenderCase(unittest.IsolatedAsyncioTestCase):
    def make(self, script=None, latency=0.0, **settings):
        self.time = VirtualTime()
        self.bot = ScriptedBot(self.time.clock, script, latency, self.time.sleep)
        options = dict(min_interval=1.0, max_per_second=25.0, attempts=3, retry_pauses=[2.0, 5.0], max_wait=120.0)
        options.update(settings)
        self.sender = Sender(self.bot, clock=self.time.clock, sleep=self.time.sleep, **options)
        self.results = []
        self.t0 = self.time.now
        return self.sender

    def submit(self, chat, text="Alarm", sender=None):
        (sender or self.sender).submit(chat, text, lambda c, ok, why, text=text: self.results.append((c, text, ok, why)))

    async def drive(self):
        await self.time.run_until_idle(self.sender)

    def offsets(self, chat=None):
        """Die Sendezeitpunkte relativ zum Start."""
        return [round(t - self.t0, 6) for t in self.bot.times(chat)]


class DeliveryTests(SenderCase):
    async def test_a_message_is_sent_at_once_as_html_and_reported(self):
        self.make()
        self.submit(11, "Hallo")
        await self.drive()
        self.assertEqual(self.offsets(), [0.0])
        self.assertEqual(self.bot.calls[0]["parse_mode"], "HTML")
        self.assertEqual(self.bot.calls[0]["text"], "Hallo")
        self.assertEqual(self.results, [(11, "Hallo", True, None)])

    async def test_submit_returns_before_anything_is_sent(self):
        self.make()
        self.submit(11)
        self.assertEqual(self.bot.calls, [])                      # eingereiht, aber noch nicht gesendet
        self.assertEqual((self.sender.pending, self.sender.queued), (1, 1))
        await self.drive()
        self.assertEqual((self.sender.pending, self.sender.queued), (0, 0))

    async def test_nothing_is_left_behind_when_it_is_done(self):
        self.make()
        for chat in range(50):
            self.submit(chat)
        await self.drive()
        self.assertEqual((self.sender._workers, self.sender._queues), ({}, {}))

    async def test_a_chat_that_was_idle_gets_a_new_worker(self):
        self.make()
        self.submit(11, "eins")
        await self.drive()
        self.time.now += 100
        self.submit(11, "zwei")
        await self.drive()
        self.assertEqual(self.bot.texts(11), ["eins", "zwei"])

    async def test_every_message_is_reported_exactly_once(self):
        self.make(script={11: [TimedOut(), None], 22: [Forbidden("x")]})
        for chat in (11, 22, 33):
            self.submit(chat)
        await self.drive()
        self.assertEqual(sorted(r[0] for r in self.results), [11, 22, 33])


class SpacingTests(SenderCase):
    async def test_messages_to_the_same_chat_are_one_interval_apart_in_order(self):
        self.make()
        for text in ("a", "b", "c"):
            self.submit(11, text)
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 1.0, 2.0])
        self.assertEqual(self.bot.texts(11), ["a", "b", "c"])

    async def test_the_interval_counts_from_the_actual_start_of_the_last_send(self):
        self.make()
        self.submit(11, "a")
        await self.drive()
        self.time.now += 0.4
        self.submit(11, "b")
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 1.0])            # nicht 1.4

    async def test_after_a_long_pause_the_next_message_goes_at_once(self):
        self.make()
        self.submit(11, "a")
        await self.drive()
        self.time.now += 10
        self.submit(11, "b")
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 10.0])

    async def test_the_interval_is_configurable(self):
        self.make(min_interval=2.5)
        self.submit(11, "a")
        self.submit(11, "b")
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 2.5])

    async def test_an_interval_of_zero_sends_back_to_back(self):
        self.make(min_interval=0, max_per_second=1000)
        for text in "abc":
            self.submit(11, text)
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 0.001, 0.002])

    async def test_a_busy_chat_does_not_delay_other_chats(self):
        self.make()
        for text in ("a", "b", "c"):
            self.submit(11, text)
        self.submit(22, "x")
        await self.drive()
        self.assertLess(self.offsets(22)[0], 0.1)                  # sofort, nicht erst nach den Nachrichten an Chat 11
        self.assertEqual(self.offsets(11), [0.0, 1.0, 2.0])


class GlobalLimitTests(SenderCase):
    async def test_all_chats_together_stay_below_the_limit(self):
        self.make(max_per_second=5)
        for chat in range(6):
            self.submit(chat)
        await self.drive()
        self.assertEqual(self.offsets(), [0.0, 0.2, 0.4, 0.6, 0.8, 1.0])

    async def test_the_default_limit_allows_25_per_second(self):
        self.make()
        for chat in range(26):
            self.submit(chat)
        await self.drive()
        gaps = {round(b - a, 6) for a, b in zip(self.offsets(), self.offsets()[1:])}
        self.assertEqual(gaps, {0.04})
        self.assertEqual(len(self.bot.calls), 26)

    async def test_a_single_chat_is_not_slowed_down_by_a_low_global_limit(self):
        self.make(max_per_second=2)                               # 0,5 s Abstand insgesamt, der Chat braucht 1 s
        self.submit(11, "a")
        self.submit(11, "b")
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 1.0])

    async def test_a_slot_is_taken_before_waiting_so_chats_never_share_one(self):
        self.make(max_per_second=1)
        for chat in (1, 2, 3):
            self.submit(chat)
        await self.drive()
        self.assertEqual(self.offsets(), [0.0, 1.0, 2.0])

    async def test_a_hundred_chats_all_get_their_message(self):
        self.make()
        for chat in range(100):
            self.submit(chat)
        await self.drive()
        self.assertEqual(len({c["chat_id"] for c in self.bot.calls}), 100)
        self.assertAlmostEqual(self.bot.calls[-1]["at"] - self.t0, 99 * 0.04, places=6)
        self.assertEqual(len(self.results), 100)
        self.assertTrue(all(ok for _, _, ok, _ in self.results))


class HangingChatTests(SenderCase):
    async def test_a_chat_where_telegram_hangs_does_not_block_the_others(self):
        gate = asyncio.get_running_loop().create_future()
        self.make(script={11: [gate]})
        self.submit(11, "haengt")
        self.submit(22, "laeuft")
        await self.time.run_until(lambda: any(r[0] == 22 for r in self.results))
        self.assertEqual([r[:3] for r in self.results], [(22, "laeuft", True)])
        self.assertEqual(self.sender.pending, 1)                    # Chat 11 hängt noch
        gate.set_result(None)
        await self.drive()
        self.assertEqual(sorted(r[0] for r in self.results), [11, 22])

    async def test_messages_behind_a_hanging_one_wait_in_their_own_chat_only(self):
        gate = asyncio.get_running_loop().create_future()
        self.make(script={11: [gate]})
        self.submit(11, "haengt")
        self.submit(11, "wartet")
        self.submit(22, "a")
        self.submit(22, "b")
        await self.time.run_until(lambda: len([r for r in self.results if r[0] == 22]) == 2)
        self.assertEqual(self.bot.texts(11), ["haengt"])            # die zweite wartet hinter der ersten
        gate.set_result(None)
        await self.drive()
        self.assertEqual(self.bot.texts(11), ["haengt", "wartet"])


class RetryTests(SenderCase):
    async def test_a_timeout_is_retried_after_the_first_pause(self):
        self.make(script={11: [TimedOut(), None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING") as logged:
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 2.0])
        self.assertEqual(self.results, [(11, "Alarm", True, None)])
        self.assertEqual(logged.output, [
            f"WARNING:{LOGGER}:Senden an Chat 11 fehlgeschlagen (TimedOut: Timed out), Versuch 1/3, neuer Versuch in 2 s"])

    async def test_the_pauses_follow_the_list_and_the_last_one_repeats(self):
        self.make(script={11: [TimedOut(), TimedOut(), TimedOut(), None]}, attempts=4)
        self.submit(11)
        await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 2.0, 7.0, 12.0])

    async def test_when_all_attempts_fail_the_failure_is_reported_and_logged_as_error(self):
        self.make(script={11: [TimedOut(), TimedOut(), TimedOut()]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 2.0, 7.0])
        self.assertEqual(self.results, [(11, "Alarm", False, "TimedOut: Timed out")])
        self.assertEqual(logged.output, [f"ERROR:{LOGGER}:Senden an Chat 11 aufgegeben nach 3 Versuchen (TimedOut: Timed out)"])

    async def test_the_number_of_attempts_is_configurable(self):
        self.make(script={11: [TimedOut()] * 10}, attempts=1)
        self.submit(11)
        with self.assertLogs(LOGGER, level="ERROR"):
            await self.drive()
        self.assertEqual(len(self.bot.calls), 1)                    # ein Versuch heißt: keine Wiederholung

    async def test_a_network_error_is_retried(self):
        self.make(script={11: [NetworkError("Verbindung getrennt"), None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING"):
            await self.drive()
        self.assertEqual(len(self.bot.calls), 2)

    async def test_an_unexpected_exception_is_retried_too(self):
        self.make(script={11: [RuntimeError("unerwartet"), None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING"):
            await self.drive()
        self.assertEqual(self.results[0][2], True)

    async def test_retry_after_waits_exactly_what_telegram_asks(self):
        self.make(script={11: [RetryAfter(7), None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING") as logged:
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 7.0])
        self.assertIn("Versuch 1/3, neuer Versuch in 7 s", logged.output[0])

    async def test_retry_after_as_a_timedelta_is_understood(self):
        error = RetryAfter(1)
        error.retry_after = datetime.timedelta(seconds=9)           # neuere Versionen der Bibliothek
        self.make(script={11: [error, None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING"):
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 9.0])

    async def test_retry_after_longer_than_the_limit_gives_up_without_waiting(self):
        self.make(script={11: [RetryAfter(200)]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0])
        self.assertEqual(self.time.now, self.t0)                    # es wurde gar nicht erst gewartet
        self.assertIn("Höchstwartezeit von 120 s wäre überschritten", logged.output[0])
        self.assertEqual(self.results[0][2], False)

    async def test_retry_after_counts_as_an_attempt(self):
        self.make(script={11: [RetryAfter(3), RetryAfter(3), RetryAfter(3), None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="ERROR"):
            await self.drive()
        self.assertEqual(len(self.bot.calls), 3)
        self.assertEqual(self.results[0][2], False)

    async def test_the_total_waiting_time_is_bounded(self):
        self.make(script={11: [TimedOut()] * 10}, attempts=5, retry_pauses=[60.0], max_wait=100.0)
        self.submit(11)
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 60.0])             # die dritte Pause (bis 120) hätte das Limit gesprengt
        self.assertIn("Höchstwartezeit von 100 s", logged.output[0])

    async def test_a_wait_that_just_fits_is_still_taken(self):
        self.make(script={11: [TimedOut(), TimedOut(), None]}, attempts=3, retry_pauses=[50.0], max_wait=100.0)
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING"):
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 50.0, 100.0])       # 50 + 50 = genau die Grenze

    async def test_a_retry_also_respects_the_interval(self):
        self.make(script={11: [TimedOut(), None]}, retry_pauses=[0.0])
        self.submit(11)
        with self.assertLogs(LOGGER, level="WARNING"):
            await self.drive()
        self.assertEqual(self.offsets(11), [0.0, 1.0])

    async def test_a_retry_does_not_hold_up_other_chats(self):
        self.make(script={11: [TimedOut(), None]})
        self.submit(11, "wiederholt")
        self.submit(22, "sofort")
        with self.assertLogs(LOGGER, level="WARNING"):
            await self.drive()
        self.assertLess(self.offsets(22)[0], 0.1)

    async def test_the_message_after_a_failed_one_is_still_sent(self):
        self.make(script={11: [TimedOut(), TimedOut(), TimedOut()]})
        self.submit(11, "kaputt")
        self.submit(11, "danach")
        with self.assertLogs(LOGGER, level="ERROR"):
            await self.drive()
        self.assertEqual([(r[1], r[2]) for r in self.results], [("kaputt", False), ("danach", True)])


class PermanentErrorTests(SenderCase):
    async def one(self, error):
        self.make(script={11: [error, None]})
        self.submit(11)
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            await self.drive()
        return logged.output[0]

    async def test_forbidden_is_not_retried_and_the_hint_names_abo(self):
        line = await self.one(Forbidden("bot was kicked from the group chat"))
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.results, [(11, "Alarm", False, "Forbidden: bot was kicked from the group chat")])
        self.assertIn("nicht möglich (Forbidden: bot was kicked from the group chat)", line)
        self.assertIn("/abo", line)

    async def test_bad_request_is_not_retried_although_the_library_calls_it_a_network_error(self):
        self.assertTrue(issubclass(BadRequest, NetworkError))        # genau darum muss die Reihenfolge stimmen
        await self.one(BadRequest("Chat not found"))
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.results[0][2], False)

    async def test_a_migrated_group_names_the_new_id(self):
        line = await self.one(ChatMigrated(-1009876543210))
        self.assertEqual(len(self.bot.calls), 1)
        self.assertIn("-1009876543210", line)
        self.assertIn("Supergruppe", line)

    async def test_an_invalid_token_is_not_retried(self):
        await self.one(InvalidToken())
        self.assertEqual(len(self.bot.calls), 1)

    async def test_a_permanent_failure_does_not_block_the_next_message_to_that_chat(self):
        self.make(script={11: [Forbidden("x")]})
        self.submit(11, "erste")
        self.submit(11, "zweite")
        with self.assertLogs(LOGGER, level="ERROR"):
            await self.drive()
        self.assertEqual([(r[1], r[2]) for r in self.results], [("erste", False), ("zweite", True)])
        self.assertEqual(self.offsets(11), [0.0, 1.0])


class QueueLimitTests(SenderCase):
    async def fill(self, max_queue):
        """Chat 1 hängt mit einer Nachricht 'in Arbeit', danach wartet nichts."""
        self.gate = asyncio.get_running_loop().create_future()
        self.make(script={1: [self.gate]}, max_queue=max_queue)
        self.submit(1, "in-arbeit")
        await self.time.settle()                                    # der Worker hat sie übernommen und hängt

    async def test_a_full_queue_drops_the_oldest_waiting_message_and_logs_an_error(self):
        await self.fill(max_queue=2)
        self.submit(1, "B")
        self.submit(1, "C")
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            self.submit(2, "D")                                     # Platz ist voll: B ist die älteste wartende
        self.assertIn("Warteschlange voll (2 Nachrichten): die älteste, an Chat 1, wird verworfen", logged.output[0])
        self.assertEqual(self.results, [(1, "B", False, "Warteschlange voll")])
        self.gate.set_result(None)
        await self.drive()
        self.assertEqual(sorted(r[1] for r in self.results if r[2]), ["C", "D", "in-arbeit"])

    async def test_a_message_that_is_already_being_sent_is_never_dropped(self):
        await self.fill(max_queue=1)
        self.submit(1, "B")
        with self.assertLogs(LOGGER, level="ERROR"):
            self.submit(1, "C")
        self.assertNotIn("in-arbeit", [r[1] for r in self.results])
        self.gate.set_result(None)
        await self.drive()
        self.assertIn(("in-arbeit", True), [(r[1], r[2]) for r in self.results])

    async def test_the_oldest_across_all_chats_is_the_one_that_goes(self):
        await self.fill(max_queue=2)
        self.submit(2, "alt")
        self.submit(1, "neu")
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            self.submit(3, "neuer")
        self.assertIn("an Chat 2", logged.output[0])
        self.assertEqual(self.results[0][:2], (2, "alt"))
        self.gate.set_result(None)
        await self.drive()

    async def test_the_limit_counts_waiting_messages_not_those_in_progress(self):
        await self.fill(max_queue=2)
        self.submit(1, "B")
        self.submit(1, "C")                                         # zwei wartende = genau am Limit, noch nichts verworfen
        self.assertEqual(self.results, [])
        self.gate.set_result(None)
        await self.drive()
        self.assertEqual(len(self.results), 3)

    async def test_the_default_limit_is_500(self):
        self.assertEqual(MAX_QUEUE, 500)


class ShutdownTests(SenderCase):
    async def test_close_delivers_what_is_waiting_and_reports_zero(self):
        self.make()
        for text in ("a", "b", "c"):
            self.submit(11, text)
        task = asyncio.ensure_future(self.sender.close(10))
        await self.time.run_until(task.done)
        self.assertEqual(task.result(), 0)
        self.assertEqual(self.bot.texts(11), ["a", "b", "c"])
        self.assertTrue(all(r[2] for r in self.results))

    async def test_close_gives_up_after_the_timeout_and_reports_the_rest(self):
        gate = asyncio.get_running_loop().create_future()
        self.make(script={11: [gate]})
        self.submit(11, "haengt")
        self.submit(11, "wartet")
        self.submit(22, "geht")
        await self.time.settle()
        task = asyncio.ensure_future(self.sender.close(5))
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            await self.time.run_until(task.done)
        self.assertEqual(task.result(), 2)
        self.assertEqual(self.time.now - self.t0, 5.0)
        self.assertIn("Beim Beenden nicht zugestellt: 2 Nachricht(en)", logged.output[-1])
        by_text = {r[1]: (r[2], r[3]) for r in self.results}
        self.assertEqual(by_text["geht"], (True, None))
        self.assertFalse(by_text["haengt"][0])
        self.assertFalse(by_text["wartet"][0])

    async def test_after_close_new_messages_fail_at_once(self):
        self.make()
        await self.sender.close(1)
        self.submit(11)
        self.assertEqual(self.results, [(11, "Alarm", False, "Versand ist beendet")])
        self.assertEqual((self.sender.pending, self.bot.calls), (0, []))

    async def test_close_without_anything_pending_returns_at_once(self):
        self.make()
        self.assertEqual(await self.sender.close(10), 0)
        self.assertEqual(self.time.now, self.t0)

    async def test_close_can_be_called_twice(self):
        self.make()
        self.submit(11)
        task = asyncio.ensure_future(self.sender.close(10))
        await self.time.run_until(task.done)
        self.assertEqual(await self.sender.close(10), 0)

    async def test_every_message_is_reported_once_even_when_closing_mid_flight(self):
        gate = asyncio.get_running_loop().create_future()
        self.make(script={11: [gate]})
        for text in ("a", "b", "c"):
            self.submit(11, text)
        await self.time.settle()
        task = asyncio.ensure_future(self.sender.close(3))
        with self.assertLogs(LOGGER, level="ERROR"):
            await self.time.run_until(task.done)
        self.assertEqual(sorted(r[1] for r in self.results), ["a", "b", "c"])
        self.assertEqual(self.sender.pending, 0)

    async def test_the_default_shutdown_time_leaves_room_before_docker_kills_the_container(self):
        from bos_telegram_bot.chat.sender import SHUTDOWN_SECONDS
        self.assertLessEqual(SHUTDOWN_SECONDS, 10)


class CallbackAndInfoTests(SenderCase):
    async def test_a_failing_callback_does_not_stop_the_sender(self):
        self.make()
        calls = []

        def bad(chat, ok, why):
            calls.append(chat)
            raise RuntimeError("Fehler im Callback")

        self.sender.submit(11, "a", bad)
        self.submit(11, "b")
        with self.assertLogs(LOGGER, level="ERROR") as logged:
            await self.drive()
        self.assertEqual(calls, [11])
        self.assertEqual(self.bot.texts(11), ["a", "b"])
        self.assertIn("Auswertung des Sendeergebnisses für Chat 11 fehlgeschlagen", logged.output[0])
        self.assertEqual(self.sender.pending, 0)

    async def test_a_result_is_never_reported_twice_and_the_count_never_goes_negative(self):
        """Die Absicherung 'genau einmal': Im normalen Ablauf kommt eine Doppelmeldung nicht vor, die Absicherung
        darf aber nicht unbemerkt wegfallen. Sonst würde das Gedächtnis zweimal umgebucht und die Zählung falsch."""
        self.make()
        self.submit(11, "a")
        item = self.sender._queues[11][0]
        self.sender._finish(item, False, "von außen")
        self.sender._finish(item, True, None)                       # zweiter Versuch, die Meldung zu wiederholen
        self.assertEqual(self.results, [(11, "a", False, "von außen")])
        self.assertEqual(self.sender.pending, 0)                    # nicht -1
        await self.drive()
        self.assertEqual(self.results, [(11, "a", False, "von außen")])
        self.assertEqual(self.sender.pending, 0)

    async def test_drain_returns_when_everything_is_done(self):
        self.make()
        for chat in (1, 2, 3):
            self.submit(chat)
        task = asyncio.ensure_future(self.sender.drain())
        await self.time.run_until(task.done)
        self.assertEqual(len(self.results), 3)

    async def test_drain_with_nothing_pending_returns_immediately(self):
        self.make()
        await asyncio.wait_for(self.sender.drain(), 1)

    async def test_debug_log_shows_send_time_attempt_and_queue_wait(self):
        self.make(latency=0.5)
        with self.assertLogs(LOGGER, level="DEBUG") as logged:
            self.submit(11, "a")
            self.submit(11, "b")
            await self.drive()
        text = "\n".join(logged.output)
        self.assertIn("Chat 11: gesendet in 0.50 s (Versuch 1, 0.0 s in der Warteschlange)", text)
        self.assertIn("Chat 11: gesendet in 0.50 s (Versuch 1, 1.0 s in der Warteschlange)", text)   # die zweite wartete 1 s
        self.assertIn("Chat 11: in der Warteschlange (2 wartend)", text)

    async def test_the_time_is_available_for_latency_measurement(self):
        self.make()
        self.assertEqual(self.sender.now(), self.t0)


class FromConfigTests(unittest.TestCase):
    def test_defaults_when_nothing_is_set(self):
        sender = Sender.from_config(ScriptedBot(), {})
        self.assertEqual((sender.min_interval, sender.attempts, sender.retry_pauses, sender.max_wait),
                         (1.0, 3, [2.0, 5.0], 120.0))
        self.assertAlmostEqual(sender.gap, 0.04)

    def test_values_from_the_config_block(self):
        sender = Sender.from_config(ScriptedBot(), {"min_interval_seconds": 2, "max_per_second": 10, "attempts": 5,
                                                     "retry_pause_seconds": [1, 3], "max_wait_seconds": 60})
        self.assertEqual((sender.min_interval, sender.attempts, sender.retry_pauses, sender.max_wait), (2.0, 5, [1.0, 3.0], 60.0))
        self.assertAlmostEqual(sender.gap, 0.1)

    def test_overrides_for_clock_and_sleep(self):
        marker = lambda: 42.0
        self.assertEqual(Sender.from_config(ScriptedBot(), {}, clock=marker).now(), 42.0)


if __name__ == "__main__":
    unittest.main()
