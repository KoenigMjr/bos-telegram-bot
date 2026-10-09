"""Gemeinsame Hilfen der Chat-Tests."""
import unittest


from bos_telegram_bot.chat import buttons, fields
from bos_telegram_bot.core.matching import candidate_values, match_sub
from tests.helpers import Env, make_callback, make_update


CSV_ROWS = [
    {"for": "1234567", "add": "Rettungswagen Musterstadt A-Wehr", "isRegex": False},
    {"for": r"^23456([0-9]{2})$", "add": r"Feuerwehr Musterstadt \1", "isRegex": True},
]


def reply_buttons(mock):
    """(Text, callback_data) aller Inline-Buttons des letzten Aufrufs."""
    markup = mock.call_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def matches(sub, payload, key):
    values, raw = candidate_values(payload, key)
    return match_sub(sub, values, raw) is not None


class CommandTestCase(unittest.IsolatedAsyncioTestCase):
    csv_rows = CSV_ROWS

    def setUp(self):
        self.env = Env(self, csv_rows=self.csv_rows)

    async def command(self, for_key, *args, kind="for", user=1, chat=None):
        update, reply = make_update(user, chat_id=chat)
        self.env.context.args = list(args)
        make = fields.make_for_handler if kind == "for" else fields.make_add_handler
        await make(self.env.entry(for_key))(update, self.env.context)
        return reply

    async def click(self, data, user=1):
        update, query = make_callback(data, user)
        await buttons.button_handler(update, self.env.context)
        return query

    def subs(self, chat=1):
        return [dict(s) for s in self.env.subs(chat)]


def buttons_of(sent_message):
    markup = sent_message["reply_markup"]
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
