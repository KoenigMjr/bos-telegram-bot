import contextlib
import io
import logging
import re
import sys
import unittest

from bos_telegram_bot import logs

TOKEN = "123456789:ABCDEFghijklmnopqrstuvwxyz_0123456"
LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4} (DEBUG|INFO|WARNING|ERROR|CRITICAL) +\S+: .+$")


class LoggingTestCase(unittest.TestCase):
    """Schreibt in einen Puffer statt auf die Konsole und räumt danach alles auf."""

    def setUp(self):
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.addCleanup(logs.reset_logging)

    def lines(self):
        return [line for line in self.out.getvalue().splitlines() if line.strip()]


class FormatTests(LoggingTestCase):
    def test_line_format_with_timezone_offset_and_short_name(self):
        logs.setup_logging("INFO")
        logging.getLogger("bos_telegram_bot.mqtt.dispatch").info("Alarm 1234567: kein passendes Abo")
        (line,) = self.lines()
        self.assertRegex(line, LINE)
        self.assertTrue(line.endswith("INFO    mqtt.dispatch: Alarm 1234567: kein passendes Abo"), line)

    def test_names_outside_the_package_stay_unchanged(self):
        logs.setup_logging("INFO")
        logging.getLogger("telegram.ext").warning("Konflikt")
        self.assertIn("WARNING telegram.ext: Konflikt", self.lines()[0])

    def test_levels_are_aligned(self):
        logs.setup_logging("DEBUG")
        logger = logging.getLogger("bos_telegram_bot.x")
        logger.debug("a"); logger.info("b"); logger.warning("c")
        self.assertEqual([line.split()[2] for line in self.lines()], ["DEBUG", "INFO", "WARNING"])
        self.assertEqual(len({line.index("x:") for line in self.lines()}), 1)


class LevelTests(LoggingTestCase):
    def emit_all(self):
        logger = logging.getLogger("bos_telegram_bot.test")
        logger.debug("d"); logger.info("i"); logger.warning("w"); logger.error("e")
        return [line.split()[2] for line in self.lines()]

    def test_info_hides_debug(self):
        logs.setup_logging("INFO")
        self.assertEqual(self.emit_all(), ["INFO", "WARNING", "ERROR"])

    def test_debug_shows_everything(self):
        logs.setup_logging("DEBUG")
        self.assertEqual(self.emit_all(), ["DEBUG", "INFO", "WARNING", "ERROR"])

    def test_warning_hides_info(self):
        logs.setup_logging("WARNING")
        self.assertEqual(self.emit_all(), ["WARNING", "ERROR"])

    def test_level_can_be_changed_later(self):
        logs.setup_logging("INFO")
        logs.set_level("debug")
        self.assertEqual(self.emit_all(), ["DEBUG", "INFO", "WARNING", "ERROR"])

    def test_invalid_level_falls_back_to_info(self):
        logs.setup_logging("laut")
        self.assertEqual(self.emit_all(), ["INFO", "WARNING", "ERROR"])

    def test_normalize_level(self):
        self.assertEqual(logs.normalize_level("debug"), "DEBUG")
        self.assertEqual(logs.normalize_level(" Warning "), "WARNING")
        for bad in ("laut", "", None, 5):
            self.assertIsNone(logs.normalize_level(bad), bad)

    def test_libraries_are_quiet_unless_debugging(self):
        logs.setup_logging("INFO")
        logging.getLogger("httpx").info("HTTP Request: POST ...")
        logging.getLogger("telegram.ext").info("Application started")
        self.assertEqual(self.lines(), [])
        logging.getLogger("httpx").warning("langsam")
        self.assertEqual(len(self.lines()), 1)

    def test_debug_shows_library_requests_but_not_connection_internals(self):
        logs.setup_logging("DEBUG")
        logging.getLogger("httpx").info("HTTP Request: POST /sendMessage 200 OK")
        logging.getLogger("httpcore.connection").debug("connect_tcp.started host='api.telegram.org'")
        logging.getLogger("asyncio").debug("Using selector: EpollSelector")
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("httpx", self.lines()[0])


class SetupTests(LoggingTestCase):
    def handlers(self):
        return [h for h in logging.getLogger().handlers if getattr(h, "_bos_telegram_bot_handler", False)]

    def test_calling_setup_twice_does_not_duplicate_lines(self):
        logs.setup_logging("INFO")
        logs.setup_logging("INFO")
        logging.getLogger("bos_telegram_bot.x").info("einmal")
        self.assertEqual(len(self.handlers()), 1)
        self.assertEqual(len(self.lines()), 1)

    def test_reset_removes_handler_and_levels(self):
        logs.setup_logging("DEBUG")
        logs.reset_logging()
        self.assertEqual(self.handlers(), [])
        self.assertEqual(logging.getLogger("bos_telegram_bot").level, logging.NOTSET)
        self.assertEqual(logging.getLogger("httpx").level, logging.NOTSET)

    def test_reset_leaves_foreign_handlers_alone(self):
        foreign = logging.NullHandler()
        logging.getLogger().addHandler(foreign)
        self.addCleanup(logging.getLogger().removeHandler, foreign)
        logs.setup_logging("INFO")
        logs.reset_logging()
        self.assertIn(foreign, logging.getLogger().handlers)

    def test_output_goes_to_stdout(self):
        logs.setup_logging("INFO")
        handler = self.handlers()[0]
        self.assertIs(handler.stream, sys.stdout)

    def test_package_has_a_null_handler_so_nothing_leaks_to_stderr_unconfigured(self):
        import bos_telegram_bot
        handlers = logging.getLogger(bos_telegram_bot.__name__).handlers
        self.assertTrue(any(isinstance(h, logging.NullHandler) for h in handlers))


class RedactionTests(LoggingTestCase):
    def test_the_exact_token_is_hidden_in_messages(self):
        logs.setup_logging("INFO", secrets=[TOKEN])
        logging.getLogger("bos_telegram_bot.x").info("Token ist %s", TOKEN)
        self.assertNotIn(TOKEN, self.out.getvalue())
        self.assertIn("<TOKEN>", self.out.getvalue())

    def test_telegram_urls_are_hidden_even_without_knowing_the_token(self):
        logs.setup_logging("DEBUG")    # kein Geheimnis übergeben: das Muster allein muss reichen
        logging.getLogger("httpx").info('HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "HTTP/1.1 200 OK"', TOKEN)
        output = self.out.getvalue()
        self.assertNotIn(TOKEN, output)
        self.assertNotIn("ABCDEFghij", output)
        self.assertIn("https://api.telegram.org/bot<TOKEN>/getUpdates", output)

    def test_tracebacks_are_hidden_too(self):
        logs.setup_logging("INFO", secrets=[TOKEN])
        try:
            raise RuntimeError(f"Fehler bei https://api.telegram.org/bot{TOKEN}/sendMessage")
        except RuntimeError:
            logging.getLogger("bos_telegram_bot.x").exception("Senden gescheitert")
        output = self.out.getvalue()
        self.assertIn("Traceback", output)
        self.assertNotIn(TOKEN, output)
        self.assertNotIn("ABCDEFghij", output)

    def test_exception_text_from_libraries_is_hidden(self):
        logs.setup_logging("INFO")
        try:
            raise ConnectionError(f"bot{TOKEN} nicht erreichbar")
        except ConnectionError:
            logging.getLogger("telegram.ext").error("Update-Fehler", exc_info=True)
        self.assertNotIn(TOKEN, self.out.getvalue())

    def test_other_secrets_and_empty_secrets(self):
        logs.setup_logging("INFO", secrets=["geheim", "", None])
        logging.getLogger("bos_telegram_bot.x").info("das ist geheim und bleibt nicht")
        self.assertIn("das ist <TOKEN> und bleibt nicht", self.out.getvalue())

    def test_normal_text_is_not_touched(self):
        logs.setup_logging("INFO", secrets=[TOKEN])
        logging.getLogger("bos_telegram_bot.x").info("Alarm 1234567 (Wache Nord): 2 Abo(s) in 1 Chat(s), gesendet 1/1")
        self.assertIn("Alarm 1234567 (Wache Nord): 2 Abo(s) in 1 Chat(s), gesendet 1/1", self.out.getvalue())
        self.assertNotIn("<TOKEN>", self.out.getvalue())

    def test_bot_word_in_normal_urls_is_not_mistaken_for_a_token(self):
        logs.setup_logging("INFO")
        logging.getLogger("bos_telegram_bot.x").info("siehe https://example.org/bot123/info")
        self.assertIn("https://example.org/bot123/info", self.out.getvalue())


class SingleLineTests(LoggingTestCase):
    """Docker zerlegt mehrzeilige Einträge und Portainer kann dabei Zeichen verfälschen: jeder Eintrag bleibt eine Zeile."""

    def emit(self, message, *args, level="INFO"):
        logs.setup_logging("DEBUG")
        logging.getLogger("bos_telegram_bot.x").log(getattr(logging, level), message, *args)

    def test_line_breaks_become_one_visible_marker(self):
        self.emit("Nachricht an Chat %s: %s", 4711, "Zeile 1\nZeile 2\nZeile 3")
        (line,) = self.lines()
        self.assertTrue(line.endswith("x: Nachricht an Chat 4711: Zeile 1 ⏎ Zeile 2 ⏎ Zeile 3"), line)

    def test_blank_lines_and_indentation_collapse_into_one_marker(self):
        self.emit("a\n\n\n   b  \n\tc")
        (line,) = self.lines()
        self.assertTrue(line.endswith("x: a ⏎ b ⏎ c"), line)

    def test_windows_and_old_mac_line_breaks(self):
        self.emit("a\r\nb\rc")
        self.assertTrue(self.lines()[0].endswith("x: a ⏎ b ⏎ c"))

    def test_a_message_without_line_breaks_is_unchanged(self):
        self.emit("Alarm 1000011 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 1/1")
        (line,) = self.lines()
        self.assertTrue(line.endswith("x: Alarm 1000011 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 1/1"))
        self.assertNotIn("⏎", line)

    def test_every_entry_starts_with_a_timestamp(self):
        logs.setup_logging("DEBUG")
        log = logging.getLogger("bos_telegram_bot.x")
        for text in ("eins", "zwei\nzeilen", "drei\n\nmit\nvielen\nzeilen"):
            log.info(text)
        self.assertEqual(len(self.lines()), 3)
        self.assertTrue(all(LINE.match(line) for line in self.lines()), self.lines())

    def test_emoji_and_html_survive(self):
        self.emit("🚨 <b>BOS-ALARM</b> 🚨\n\nWache Nord\n<i>abonniert über: RIC</i>")
        self.assertIn("🚨 <b>BOS-ALARM</b> 🚨 ⏎ Wache Nord ⏎ <i>abonniert über: RIC</i>", self.lines()[0])

    def test_a_traceback_stays_readable_on_several_lines(self):
        logs.setup_logging("INFO")
        try:
            raise RuntimeError("kaputt")
        except RuntimeError:
            logging.getLogger("bos_telegram_bot.x").exception("Fehler\nmit Umbruch")
        lines = self.lines()
        self.assertTrue(lines[0].endswith("x: Fehler ⏎ mit Umbruch"), lines[0])     # die Meldung ist eine Zeile
        self.assertEqual(lines[1], "Traceback (most recent call last):")             # der Traceback bleibt mehrzeilig
        self.assertTrue(lines[-1].startswith("RuntimeError: kaputt"))

    def test_redaction_still_applies_to_multiline_messages(self):
        logs.setup_logging("INFO", secrets=[TOKEN])
        logging.getLogger("bos_telegram_bot.x").info("zeile 1\nToken %s", TOKEN)
        self.assertNotIn(TOKEN, self.out.getvalue())
        self.assertIn("zeile 1 ⏎ Token <TOKEN>", self.out.getvalue())

    def test_the_record_seen_by_other_handlers_keeps_its_original_text(self):
        """assertLogs & Co. sehen den Text ohne Marker, nur unsere Ausgabe wird umgeformt."""
        logs.setup_logging("INFO")
        with self.assertLogs("bos_telegram_bot.x", level="INFO") as logged:
            logging.getLogger("bos_telegram_bot.x").info("a\nb")
        self.assertEqual(logged.records[0].getMessage(), "a\nb")


if __name__ == "__main__":
    unittest.main()
