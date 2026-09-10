# BOS-Telegram-Bot

Verteilt Alarme aus [BOSWatch3](https://github.com/KoenigMjr/BW3-Core) per
Telegram. Der Bot empfängt die Alarme (JSON) über MQTT und benachrichtigt
Personen oder Gruppen, die ein passendes Kriterium abonniert haben, z.B. eine
bestimmte RIC, ein Fahrzeug/eine Wache oder ein Stichwort wie `THL*`.

Gefiltert, dedupliziert und beschrieben wird bereits in BOSWatch3 (u.a. über
das `descriptor`- und `multicast`-Modul). Der Bot verarbeitet nur das fertige
Ergebnis.

## Schnellstart

Voraussetzung: Docker (oder Portainer) und ein MQTT-Broker, an den BOSWatch3
die Alarme als JSON sendet.

1. **Bot anlegen:** In Telegram [@BotFather](https://t.me/BotFather)
   schreiben, `/newbot` ausführen und den **Token** notieren.
2. **Eigene Telegram-ID herausfinden:** [@userinfobot](https://t.me/userinfobot)
   schreiben. Die Zahl ist deine ID, damit wirst du Admin.
3. **Starten:** Die [`docker-compose.yml`](docker-compose.yml) aus diesem
   Repo verwenden (Portainer: *Stacks → Add stack → Web editor*, Inhalt
   einfügen) und diese Variablen setzen:

   | Variable             | Pflicht | Bedeutung                                        |
   |----------------------|---------|---------------------------------------------------|
   | `TELEGRAM_BOT_TOKEN` | ja      | Token von @BotFather                               |
   | `ADMIN_USERS`        | ja      | Deine Telegram-ID (mehrere: `123,456`)              |
   | `MQTT_HOST`          | ja      | IP/Hostname deines MQTT-Brokers                     |
   | `MQTT_PORT`          | nein    | Standard `1883`                                     |
   | `MQTT_USERNAME`, `MQTT_PASSWORD` | nein | falls der Broker Zugangsdaten verlangt   |
   | `MQTT_TOPIC`         | nein    | Standard `homeassistant/boswatch/alarm/+`           |

4. **Dem Bot `/start` schreiben.** Fertig.

Es wird nichts selbst gebaut: Das Image kommt fertig von GitHub
(`ghcr.io/koenigmjr/bos-telegram-bot`), für Intel/AMD und ARM
(Raspberry Pi, NAS). **Update:** Stack neu deployen, die Compose-Datei zieht
dann automatisch das neueste Image. Datenbank und Benutzer liegen im
Datenordner auf dem Host (Standard `/opt/bos-telegram-bot/data`) und bleiben
erhalten.

Ein erster Test ohne weitere Einrichtung: `/message THL*` schickt dir ab
jetzt jeden Alarm, dessen Text mit "THL" beginnt.

Weitere Personen schaltest du später direkt im Bot frei, ohne Neustart
(siehe [Benutzer und Admins](#benutzer-und-admins)).

### Namens-Suche für RIC/Fahrzeuge einrichten (optional)

Freitext-Filter wie `/message` funktionieren sofort. Für die Suche nach
Fahrzeug- oder Wachennamen (`/description Muster`) sowie die RIC-Suche
(`/ric 1234567`) braucht der Bot eine CSV im
[BOSWatch3-Descriptor-Format](#csv-schema-kompatibel-zu-boswatch3s-descriptor-modul).

**Einfachster Weg:** Die Datei unter dem Namen `descriptions_ric.csv` in den
Datenordner legen (Standard `/opt/bos-telegram-bot/data/`) und den Bot neu
starten. Die CSV wird beim Start eingelesen, Änderungen greifen also nach einem
Neustart. Eine Vorlage liegt in
[`examples/descriptions_ric.csv`](examples/descriptions_ric.csv). Der Bot liest
die Datei nur und verändert sie nie.

**Alternative, wenn BOSWatch3 auf demselben Host läuft:** Statt zu kopieren,
dessen Ordner read-only einbinden, dann gibt es nur eine Datei als Quelle der
Wahrheit:

1. In der `docker-compose.yml` unter `volumes:` die auskommentierte
   **BOSWatch3-Zeile** (endet auf `:/boswatch3-config:ro`) aktivieren und den
   Host-Pfad anpassen, z.B. `/opt/boswatch3/config:/boswatch3-config:ro`
2. Die Variablen `CSV_PATH_RIC` und `CSV_PATH_DESCRIPTION` auf die Datei
   setzen, z.B. `/boswatch3-config/descriptions_ric.csv`

### Eigene Felder (fortgeschritten)

Welche Felder es gibt (und damit welche Befehle), steht in
[`config.yaml`](config.yaml) im Image. Für eigene Felder die Datei kopieren,
anpassen und per `- /pfad/auf/dem/host/config.yaml:/app/config.yaml:ro` unter
`volumes:` einbinden. Details zu den Feldern weiter unten.

## Datensicherung

Alles, was der Bot sich merkt, liegt im Datenordner (Standard
`/opt/bos-telegram-bot/data`):

| Datei               | Inhalt                                             |
|---------------------|-----------------------------------------------------|
| `bot_db.sqlite3`    | Abos aller Chats sowie freigeschaltete User und Anfragen |
| `*.csv`             | deine Namens-Listen, falls du sie hier abgelegt hast |

Token, Admin-IDs und MQTT-Zugang stehen nicht dort, sie kommen aus den
Umgebungsvariablen des Stacks und sollten separat notiert sein.

**Sichern:** den Ordner kopieren. Am sichersten, solange der Bot kurz steht:

```bash
docker stop bos-telegram-bot
cp -a /opt/bos-telegram-bot/data /pfad/zum/backup/data-$(date +%F)
docker start bos-telegram-bot
```

Ohne Stopp geht es im laufenden Betrieb mit dem SQLite-eigenen Backup (so
bekommst du auch bei gleichzeitigen Schreibzugriffen eine konsistente Kopie):

```bash
sqlite3 /opt/bos-telegram-bot/data/bot_db.sqlite3 ".backup '/pfad/zum/backup/bot_db.sqlite3'"
```

**Wiederherstellen:** Container stoppen, Ordner bzw. `bot_db.sqlite3` zurückkopieren,
Container starten. Ein Update des Images berührt den Datenordner nicht.

## Lesen ja, Schreiben nur im eigenen Ordner

Der Bot **liest** CSV-Dateien von überall, auch aus fremden, read-only
eingebundenen Verzeichnissen wie dem Config-Ordner von BOSWatch3. Er
**schreibt** aber niemals in einen Pfad außerhalb seines eigenen
`data/`-Ordners. Das gilt egal, ob der Pfad in `config.yaml` oder per
`CSV_PATH_<FELDNAME>` gesetzt wird:

- Liegt die CSV innerhalb von `data/` und fehlt noch, legt der Bot sie beim
  Start mit leerem Header (`for,add,isRegex`) an.
- Liegt sie außerhalb (z.B. im eingebundenen BOSWatch3-Ordner) und fehlt dort,
  passiert **nichts** außer einer Warnung im Log. Das betroffene Feld liefert
  dann keine Treffer, bis die Datei existiert.

Empfehlung: BOSWatch3s Descriptor-CSV per `:ro`-Volume einbinden, statt eine
zweite Kopie zu pflegen. Dann gibt es eine Datei als einzige Quelle der
Wahrheit.

## Felder: Was es gibt und wie sie funktionieren

Ein **Feld** ist ein Wert aus dem MQTT-JSON, den man abonnieren bzw. filtern
kann (z.B. die RIC, die Beschreibung oder der Alarmtext). Jedes Feld, das in
`config.yaml` unter `fields:` definiert ist, wird automatisch zu einem
eigenen Telegram-Befehl. Neues Feld = Config editieren, kein Python anfassen.

### Mitgelieferte Felder

| Befehl          | JSON-Schlüssel | Modus     | Wofür                                                        |
|-----------------|----------------|-----------|---------------------------------------------------------------|
| `/ric`          | `ric`          | `lookup`  | Einzelne RIC bzw. Fahrzeug/Wache aus der CSV abonnieren       |
| `/description`  | `description`  | `lookup`  | Dasselbe, aber per Fahrzeug-/Wachenname statt per RIC suchen  |
| `/message`      | `message`      | `pattern` | Alarmtext filtern, z.B. alle `THL*`- oder `RD*`-Einsätze      |
| `/subric_text`  | `subricText`   | `pattern` | Sub-RIC-Buchstabe (a/b/c/d) filtern                           |

Diese Auswahl ist nur der Startpunkt. Welche Felder es gibt, bestimmst du in
`config.yaml`. Als Faustregel taugen Felder, die einen Alarm **inhaltlich**
beschreiben (`ric`, `description`, `message`, `subricText`, bei Bedarf auch
`frequency`). Technische Metadaten wie `clientName`, `serverVersion`, `timestamp`
oder die `*_list`-Felder lohnen sich als Abo-Kriterium meist nicht. Mit
`/lastraw` siehst du das zuletzt empfangene JSON und damit die exakten
Feldnamen deines Setups.

### Der Unterschied: `lookup` vs. `pattern`

Beide Modi vergleichen am Ende dasselbe: den Wert des Feldes im ankommenden
Alarm gegen das, was du abonniert hast. Der Unterschied liegt darin, **woher
das Abo-Ziel kommt**.

|                          | `mode: lookup` ("Liste")                          | `mode: pattern` ("Freieingabe")                    |
|--------------------------|----------------------------------------------------|-----------------------------------------------------|
| Woher kommt das Ziel?    | Aus einer CSV mit bekannten Werten                  | Aus deiner Eingabe, ohne Liste                       |
| Eingabe                  | Suchbegriff, z.B. `/description Muster`                     | Muster, z.B. `/message THL*`                         |
| Rückmeldung              | Treffer werden aufgelistet, du wählst per Button    | Muster wird direkt übernommen                        |
| Tippfehler               | Fallen auf (kein Treffer)                            | Fallen nicht auf, das Abo greift dann nie            |
| Pflegeaufwand            | CSV muss die Werte enthalten                         | keiner                                                |
| Passt für                | Endliche, bekannte Mengen (RICs, Fahrzeuge, Wachen)  | Freitext oder Felder ohne gepflegte Liste (Alarmtext) |
| Vergleich beim Alarm     | Exakt, oder per Regex bei `isRegex=true`-Zeilen      | Immer als Regex (aus deinem Wildcard-Muster erzeugt) |

**`lookup` im Detail** (`/description Muster`):

1. Der Suchbegriff wird in der `search_column` der CSV gesucht
   (Teilstring, Groß-/Kleinschreibung egal).
2. Exakter Treffer in dieser Spalte: wird sofort abonniert.
3. Genau ein Treffer: wird ebenfalls sofort abonniert.
4. Mehrere Treffer: paginierte Auswahl (5 pro Seite) mit Buttons.
5. Gespeichert wird der Wert aus `target_column`, angezeigt der aus
   `display_column`. Dadurch können mehrere Felder **dieselbe** CSV nutzen
   (`/ric` sucht und matcht in `for`, `/description` in `add`).

**`pattern` im Detail** (`/message THL*`):

1. Dein Muster wird in eine Regex umgewandelt (Syntax siehe
   "Wildcard-Syntax" weiter unten) und ungültige Muster werden direkt
   abgelehnt.
2. Bei jedem Alarm wird der Feldwert (hier `message`) gegen diese Regex
   geprüft. Es gibt keine CSV und keine Auswahlliste.

**Faustregel:** Gibt es eine feste, überschaubare Menge gültiger Werte, die du
vorher kennst (Fahrzeuge, RICs)? Dann `lookup`. Ist der Wert Freitext oder
ändert sich ständig (Einsatzstichwörter)? Dann `pattern`.

> **Hinweis zu `/description`:** Der Vergleich passiert dort exakt gegen den
> Beschreibungstext im Alarm. Wache-Muster mit `\1`-Platzhaltern (`isRegex=true`
> in der CSV) lassen sich deshalb nur über `/ric` sinnvoll abonnieren, denn
> der Platzhalter-Text selbst taucht im Alarm nie wörtlich auf.

### Beispielkonfiguration

```yaml
fields:
  ric:
    label: "RIC"
    json_key: "ric"          # exakter Schlüssel im MQTT-JSON
    mode: lookup
    csv_path: "data/descriptions_ric.csv"   # BOSWatch3-Descriptor-Schema
    search_column: for        # worin /ric <suchbegriff> sucht
    target_column: for        # was als Abo-Ziel gespeichert wird
    display_column: add       # was als Name angezeigt wird
  description:
    label: "Fahrzeug / Wache"
    json_key: "description"
    mode: lookup
    csv_path: "data/descriptions_ric.csv"   # dieselbe Datei, andere Spalten
    search_column: add
    target_column: add
    display_column: add
  message:
    label: "Alarmstichwort"
    json_key: "message"
    mode: pattern              # kein CSV, freies Wildcard-Muster
```

Pflichtangaben je Modus:

- `lookup`: `label`, `json_key`, `mode`, `csv_path`, `search_column`,
  `target_column`, `display_column` (Spalten sind `for` oder `add`)
- `pattern`: `label`, `json_key`, `mode`

Der Befehlsname entspricht dem Feld-Schlüssel (`ric` wird zu `/ric`), wird aber
automatisch auf `a-z0-9_` normalisiert, da Telegram keine Großbuchstaben in
Befehlen erlaubt. `subric_text` im Beispiel mappt auf den JSON-Schlüssel
`subricText`.

## CSV-Schema (kompatibel zu BOSWatch3s `descriptor`-Modul)

```
for,add,isRegex
1234567,Rettungswagen Musterstadt A-Wehr,false
^23456([0-9]{2})$,Feuerwehr Musterstadt,true
```

- `for`: die RIC oder, bei `isRegex=true`, ein regulärer Ausdruck für mehrere RICs
- `add`: der Anzeigename (bei Regex-Zeilen optional mit Platzhaltern, siehe unten)
- `isRegex`: `true` oder `false`, die Spalte darf bei normalen Zeilen auch fehlen

Eine fertige Vorlage liegt in
[`examples/descriptions_ric.csv`](examples/descriptions_ric.csv). Alle Namen und
Nummern in den Beispielen sind frei erfunden.

Das Parsing ist bewusst identisch zu `module/descriptor.py` in BW3-Core
gehalten (inkl. Toleranz für fehlende `isRegex`-Spalte), damit dieselbe Datei
unverändert von beiden Systemen gelesen werden kann.

## Wildcard-Syntax bei `mode: pattern`

| Eingabe        | Bedeutung                                      |
|----------------|-------------------------------------------------|
| `THL`          | enthält "THL" (implizite Contains-Suche)        |
| `THL*`         | beginnt mit "THL"                               |
| `*THL*`        | enthält "THL" (explizit)                        |
| `THL%`         | wie `THL*` — `%` und `*` sind gleichbedeutend    |
| `THL?`         | "THL" + genau ein beliebiges Zeichen             |
| `re:^RD\s?\d`  | rohe Regex für Power-User, kein Auto-Anchoring   |

## Benutzer und Admins

Der Bot hat genau **einen** Token (`TELEGRAM_BOT_TOKEN`). Beim Zugriff gibt es
zwei Gruppen von Personen:

| Rolle             | Wo festgelegt                         | Rechte                                              |
|-------------------|----------------------------------------|------------------------------------------------------|
| **Admin**         | `ADMIN_USERS` (Umgebungsvariable)     | Alles, plus Benutzerverwaltung (`/users`, `/adduser`) |
| **User**          | Im Bot freigeschaltet (SQLite-DB)       | Abos anlegen und verwalten                            |

`ADMIN_USERS` ist eine Liste numerischer Telegram-User-IDs und enthält
**mindestens einen Admin** (ohne Admin startet der Bot nicht):

```
ADMIN_USERS=123456789,987654321
```

Erlaubte Trenner: Komma, Semikolon, Leerzeichen oder Zeilenumbruch. Doppelte
IDs werden entfernt, ungültige Einträge (z.B. `abc`) brechen den Start mit
einer klaren Meldung ab.

### Neue User ohne Redeploy freischalten

1. Die Person schreibt dem Bot `/start`. Sie bekommt ihre Telegram-ID genannt
   und die Info, dass eine Anfrage an die Admins ging.
2. Jeder Admin erhält eine Nachricht mit **Freischalten / Ablehnen**.
3. Nach der Freigabe wird die Person benachrichtigt und kann den Bot nutzen.

Es ist weder ein Neustart noch ein neues Deployment nötig. Die Freigaben liegen
in `bot_db.sqlite3` im Datenordner und überleben Neustarts.

| Befehl                        | Wer    | Zweck                                                       |
|-------------------------------|--------|--------------------------------------------------------------|
| `/users`                      | Admin  | Admins, freigeschaltete User und offene Anfragen anzeigen, per Button entfernen/freigeben |
| `/adduser <ID> [Name]`        | Admin  | User direkt per ID freischalten, ohne dass die Person zuerst schreibt |

Hinweise:

- **Abgelehnte** Personen werden danach still ignoriert (keine weiteren
  Anfragen an die Admins). Ein späteres `/adduser` hebt das auf.
- **Entzug** über `/users` löscht auch die privaten Abos der Person, damit sie
  keine Alarme mehr bekommt. Abos in Gruppen bleiben bestehen, sie gehören der
  Gruppe (siehe unten).
- Admins müssen dem Bot **einmal selbst geschrieben haben** (`/start`), sonst
  darf Telegram ihnen keine Anfragen zustellen.
- Admins ändern sich nur über `ADMIN_USERS`, das erfordert einen Neustart
  des Containers. Das ist beabsichtigt: Die Admin-Liste ist der feste Anker,
  der sich nicht per Chat verändern lässt.

## Mehrere Gruppen mit unterschiedlichen Configs

Abos hängen an der **Chat-ID**, nicht an der Person. Ein privater Chat hat
bei Telegram dieselbe ID wie der jeweilige User (ändert für DMs also nichts),
eine Gruppe hat eine eigene (negative) ID. Dadurch ergibt sich automatisch:

- Bot zu "Notarzt-Gruppe" hinzufügen, dort z.B. `/message RD*` ausführen →
  Abo gehört der Gruppe, alle Mitglieder bekommen die Alarme dort.
- Bot zu "Feuerwehr-Gruppe" hinzufügen, dort andere Filter setzen → komplett
  unabhängige Config, eigene `/abo`-Liste.
- Eine Person kann gleichzeitig in beliebig vielen Gruppen sein und zusätzlich
  eigene private Abos per DM haben — alles unabhängig voneinander.

Die Zugriffsprüfung (Admins aus `ADMIN_USERS` plus freigeschaltete User)
greift pro **Person**, unabhängig vom Chat: Nur wer berechtigt ist, darf
Befehle ausführen und damit Abos der Gruppe ändern. Schreibt jemand
Unberechtigtes einen Befehl in eine Gruppe, in der der Bot ist, läuft
derselbe Anfrage-Ablauf wie im privaten Chat (die Admins werden gefragt),
Abos ändern kann die Person erst nach der Freigabe.

Technisches Detail: Telegrams "Privacy Mode" (BotFather-Einstellung) muss
dafür nicht geändert werden — Bots sehen Befehle (`/...`) in Gruppen immer,
unabhängig vom Privacy Mode; der betrifft nur normale Textnachrichten ohne
Slash-Befehl.

## Grenzen der Telegram-Autovervollständigung

Telegram zeigt im "/"-Menü automatisch alle registrierten Befehle mit
Beschreibung an (`set_my_commands` in `main.py`) — das ist aber nur eine
Vervollständigung des **Befehlsnamens**. Eine Live-Vorschlagsliste *während*
du z.B. bei `/description Mus` tippst, unterstützt Telegram für normale
Nachrichten nicht. Die aktuelle Lösung (Eingabe abschicken → bei mehreren
Treffern erscheint eine Buttons-Liste) ist der pragmatische Ersatz dafür.

Echtes "Tippen und sofort Vorschläge sehen" ginge nur über Telegrams
**Inline-Mode** (`@BotName suchtext` in einem beliebigen Chat). Das ist
derzeit nicht umgesetzt, es bräuchte einen eigenen Inline-Handler und die
Aktivierung bei BotFather.

## Platzhalter in Regex-Zeilen

Bei `isRegex=true` darf der Anzeigename Gruppen aus dem Muster enthalten
(`\1`, `\2`, ...). Der Bot setzt sie beim Alarm ein:

```
for:     ^23456([0-9]{2})$
add:     Feuerwehr Musterstadt \1
Alarm-RIC: 2345625
Anzeige: Feuerwehr Musterstadt 25
```

Ohne Platzhalter (wie in der Beispiel-CSV) bleibt der Name unverändert.

## Für Maintainer: Image veröffentlichen

Der Workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)
baut das Image automatisch:

- Push auf `main` → Tag `latest`
- Git-Tag `v1.2.3` → Tags `1.2.3` und `1.2` (für Nutzer, die eine feste Version wollen)

Einmalig nach dem ersten erfolgreichen Lauf: Auf GitHub unter *Packages →
bos-telegram-bot → Package settings* die Sichtbarkeit auf **Public** stellen,
sonst können andere das Image nicht ohne Login ziehen.