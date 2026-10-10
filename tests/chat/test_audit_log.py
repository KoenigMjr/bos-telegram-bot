"""Was ein Admin später im Log nachlesen können soll: wer hat welches Abo angelegt, wer wurde freigeschaltet."""
from unittest.mock import AsyncMock

from telegram.error import TimedOut

from bos_telegram_bot.chat import access, admin, overview
from bos_telegram_bot.storage import database as db
from tests.chat.base import CommandTestCase
from tests.helpers import make_update

FIELDS = "bos_telegram_bot.chat.fields"
BUTTONS = "bos_telegram_bot.chat.buttons"
ACCESS = "bos_telegram_bot.chat.access"
ADMIN = "bos_telegram_bot.chat.admin"


class SubscriptionLogTests(CommandTestCase):
    async def test_an_exact_value_is_logged(self):
        with self.assertLogs(FIELDS, level="INFO") as logged:
            await self.command("ric", "7654321")
        self.assertEqual(logged.output, [f"INFO:{FIELDS}:Abo gesetzt: Chat 1, Feld ric, Wert '7654321'"])

    async def test_a_pattern_is_logged_as_pattern(self):
        with self.assertLogs(FIELDS, level="INFO") as logged:
            await self.command("ric", "76543*")
        self.assertTrue(logged.output[0].startswith(f"INFO:{FIELDS}:Abo gesetzt: Chat 1, Feld ric, Muster "), logged.output)

    async def test_the_chat_not_the_user_is_logged_for_groups(self):
        with self.assertLogs(FIELDS, level="INFO") as logged:
            await self.command("ric", "7654321", chat=-100500)
        self.assertIn("Chat -100500", logged.output[0])

    async def test_picking_a_name_from_the_results_is_logged(self):
        await self.command("ric", "Musterstadt", kind="add")
        with self.assertLogs(FIELDS, level="INFO") as logged:
            await self.click("add:ric:0")
        self.assertEqual(logged.output, [f"INFO:{FIELDS}:Abo gesetzt: Chat 1, Feld ric, Wert '1234567'"])

    async def test_a_name_pattern_is_logged(self):
        await self.command("ric", "Musterstadt", kind="add")
        with self.assertLogs(FIELDS, level="INFO") as logged:
            await self.click("pat:ric")
        self.assertTrue(logged.output[0].startswith(f"INFO:{FIELDS}:Abo gesetzt: Chat 1, Feld description, Muster "), logged.output)

    async def test_removing_a_subscription_is_logged(self):
        await self.command("ric", "7654321")
        sub_id = self.subs()[0]["id"]
        with self.assertLogs(BUTTONS, level="INFO") as logged:
            await self.click(f"del:{sub_id}")
        self.assertEqual(logged.output, [f"INFO:{BUTTONS}:Abo entfernt: Chat 1, Abo {sub_id}"])

    async def test_a_rejected_input_logs_nothing_at_info(self):
        with self.assertRaises(AssertionError):            # assertLogs schlägt fehl, wenn nichts geloggt wurde
            with self.assertLogs(FIELDS, level="INFO"):
                await self.command("message", "re:(")           # ungültiger regulärer Ausdruck


class AccessLogTests(CommandTestCase):
    async def start(self, user):
        update, reply = make_update(user, name="Fremde Person")
        await overview.start_handler(update, self.env.context)
        return reply

    async def test_an_access_request_is_logged_once(self):
        with self.assertLogs(ACCESS, level="INFO") as logged:
            await self.start(77)
        self.assertEqual(logged.output, [f"INFO:{ACCESS}:Zugriffsanfrage von 77 (Chat 77)"])
        with self.assertRaises(AssertionError):            # weitere Versuche melden sich nicht erneut
            with self.assertLogs(ACCESS, level="INFO"):
                await self.start(77)

    async def test_the_name_of_the_requester_is_not_logged(self):
        with self.assertLogs(ACCESS, level="DEBUG") as logged:
            await self.start(77)
        self.assertNotIn("Fremde Person", "\n".join(logged.output))

    async def test_a_denied_sender_is_only_a_debug_line(self):
        db.set_user_status(self.env.db_path, 77, "denied")
        with self.assertLogs(ACCESS, level="DEBUG") as logged:
            await self.start(77)
        self.assertEqual(logged.output, [f"DEBUG:{ACCESS}:Abgelehnter oder unbekannter Absender 77 ignoriert"])

    async def test_approving_is_logged_with_the_admin(self):
        await self.start(77)
        with self.assertLogs(BUTTONS, level="INFO") as logged:
            await self.click("acc:ok:77", user=1)
        self.assertEqual(logged.output, [f"INFO:{BUTTONS}:Admin 1 hat 77 freigeschaltet"])

    async def test_denying_is_logged_with_the_admin(self):
        await self.start(77)
        with self.assertLogs(BUTTONS, level="INFO") as logged:
            await self.click("acc:no:77", user=1)
        self.assertEqual(logged.output, [f"INFO:{BUTTONS}:Admin 1 hat die Anfrage von 77 abgelehnt"])

    async def test_revoking_access_is_logged(self):
        db.set_user_status(self.env.db_path, 77, "allowed")
        with self.assertLogs(BUTTONS, level="INFO") as logged:
            await self.click("usr:del:77", user=1)
        self.assertEqual(logged.output, [f"INFO:{BUTTONS}:Admin 1 hat 77 den Zugriff entzogen (inkl. privater Abos)"])

    async def test_an_already_handled_request_logs_nothing(self):
        db.set_user_status(self.env.db_path, 77, "allowed")
        with self.assertRaises(AssertionError):
            with self.assertLogs(BUTTONS, level="INFO"):
                await self.click("acc:ok:77", user=1)

    async def test_adduser_is_logged_with_the_admin(self):
        update, _ = make_update(1)
        self.env.context.args = ["55", "Max", "Mustermann"]
        with self.assertLogs(ADMIN, level="INFO") as logged:
            await admin.adduser_handler(update, self.env.context)
        self.assertEqual(logged.output, [f"INFO:{ADMIN}:Admin 1 hat 55 freigeschaltet"])

    async def test_an_undeliverable_message_is_a_warning_with_the_reason(self):
        self.env.bot.send_message = AsyncMock(side_effect=TimedOut())
        with self.assertLogs(ACCESS, level="WARNING") as logged:
            delivered = await access.try_send(self.env.context, 5, "Test")
        self.assertFalse(delivered)
        self.assertEqual(logged.output, [f"WARNING:{ACCESS}:Nachricht an 5 nicht zustellbar (TimedOut: Timed out)"])
