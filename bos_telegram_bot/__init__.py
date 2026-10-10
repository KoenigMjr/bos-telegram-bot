"""BOS-Telegram-Bot: verteilt BOSWatch3-Alarme per Telegram."""

import logging

# Wie bei Bibliotheken üblich: ohne eingerichtetes Logging bleiben Meldungen unter WARNING stumm.
logging.getLogger(__name__).addHandler(logging.NullHandler())
