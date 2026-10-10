# BOS-Telegram-Bot

Verteilt Alarme aus [BOSWatch3](https://github.com/KoenigMjr/BW3-Core) per
Telegram. Der Bot empfängt die Alarme (JSON) über MQTT und benachrichtigt
Personen oder Gruppen, die ein passendes Kriterium abonniert haben, z.B. eine
bestimmte RIC, ein Fahrzeug oder ein Stichwort wie `THL*`.

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
   | `CSV_PATH_RIC`       | nein    | CSV mit RIC → Name, siehe [Namen für RIC und Fahrzeuge](#namen-für-ric-und-fahrzeuge) |
   | `EXTRA_FIELDS`       | nein    | Weitere Felder aus dem Alarm als Befehl, z.B. `stadtteil,objekt` |
   | `LOG_LEVEL`          | nein    | Standard `INFO`, für die Fehlersuche `DEBUG`, siehe [Logs](#logs) |
   | `TZ`                 | nein    | Zeitzone der Logzeiten, z.B. `Europe/Berlin` (Standard: UTC)       |

4. **Dem Bot `/start` schreiben.** Fertig.

Es wird nichts selbst gebaut: Das Image kommt fertig von GitHub
(`ghcr.io/koenigmjr/bos-telegram-bot`), für Intel/AMD und ARM
(Raspberry Pi, NAS). **Update:** Stack neu deployen, die Compose-Datei zieht
dann automatisch das neueste Image. Datenbank und Benutzer liegen im
Datenordner auf dem Host (Standard `/opt/bos-telegram-bot/data`) und bleiben
erhalten.

Ein erster Test ohne weitere Einrichtung: `/message THL*` schickt dir ab
jetzt jeden Alarm, dessen Text mit "THL" beginnt. Weitere Personen schaltest du
direkt im Bot frei, ohne Neustart (siehe [Benutzer und Admins](#benutzer-und-admins)).

## Befehle und Eingabe

Jeder Befehl nimmt eine **Freieingabe**. Du musst nichts auswählen und nichts
vorher einrichten.

| Befehl                         | Wofür                                                          |
|--------------------------------|-----------------------------------------------------------------|
| `/ric <Wert oder Muster>`      | eine RIC (genau) oder mehrere per Muster                        |
| `/description <Name oder Muster>` | Fahrzeug/Wache nach Namen suchen oder per Muster filtern     |
| `/message <Text oder Muster>`  | Alarmstichwort filtern                                           |
| `/subrictext <Wert>`           | Sub-RIC (a bis d)                                                |
| `/abo`                         | Abos anzeigen und per Klick entfernen                            |
| `/lastraw`                     | zuletzt empfangenes Alarm-JSON anzeigen                          |
| `/users`, `/adduser <ID>`      | nur Admins, siehe [Benutzer und Admins](#benutzer-und-admins)    |

### Befehl ohne Eingabe: der Bot fragt nach

Wählst du einen Befehl im Telegram-Menü, sendet Telegram ihn sofort ab, noch
bevor du etwas eintippen kannst. Das lässt sich nicht ändern. Deshalb antwortet
der Bot mit der Anleitung **und** einer Rückfrage: Dein Eingabefeld ist auf die
Antwort eingestellt und zeigt ein Beispiel als Platzhalter. Du tippst nur noch
die Eingabe, das wirkt wie `/description Wache`.

```
Du:   /description
Bot:  ✍️ Fahrzeug / Wache: Name oder Muster eingeben.
      Einfach auf diese Nachricht antworten oder den Befehl direkt mit Eingabe schreiben:
      • /description Name – in der Liste suchen
      • /description *Teil* – Muster (Platzhalter *)
Du:   Wache
Bot:  ✅ Abonniert: …
```

Die Anleitung bleibt vollständig erhalten und zeigt weiterhin die direkte
Schreibweise, so lernst du beide Wege kennen. Das gilt für alle Befehle, die
eine Eingabe brauchen: `/ric`, `/description`, `/message`, `/subrictext`,
eigene Felder und `/adduser`.

- In **Gruppen** erscheint das Antwortfeld nur bei der Person, die den Befehl
  gesendet hat. Antworten anderer Personen auf die Rückfrage zählen nicht.
- Jede Rückfrage lässt sich **einmal** beantworten. Pro Person merkt sich der
  Bot bis zu zehn offene. Nach einem Neustart sind sie ungültig, dann meldet
  der Bot das und du sendest den Befehl erneut.
- Du brauchst dafür **nichts bei BotFather** einzustellen, auch nicht bei
  aktivem Privacy Mode: Antworten auf Nachrichten des Bots kommen immer an.

### Wie Eingaben ausgewertet werden

`*` (oder `%`) steht für beliebig viele Zeichen, `?` (oder `_`) für genau eines.
Wer ein Muster eingibt, bestimmt selbst, was gelten soll:

| Eingabe              | Bedeutung                              |
|----------------------|-----------------------------------------|
| `/ric 1234567`       | genau diese RIC                         |
| `/ric 301*`          | alle RICs, die mit 301 **beginnen**     |
| `/ric *301*`         | alle RICs, die 301 **enthalten**        |
| `/message THL*`      | Text beginnt mit "THL"                  |
| `/message THL`       | Text enthält "THL"                      |
| `/message B 3+`      | "B" mit Zahl 3 **oder höher** (B 3, B 4, B 10 …, nicht B 1 und B 2) |
| `/message re:^RD\s?\d` | rohe Regex für Fortgeschrittene     |

Ohne Platzhalter gilt bei **RIC und Sub-RIC genau dieser Wert**, bei allen
anderen Feldern "enthält". `/ric 301` trifft also nicht jede RIC mit 301
darin, das geht nur mit `*301*`. Welches Feld wie reagiert, legt die Option
`match` fest (siehe [Konfiguration](#konfiguration-anpassen-optional)).

Eine Zahl mit angehängtem `+` heißt "diese Zahl oder jede größere". Verglichen
wird der Zahlenwert, `B 3+` trifft also auch "B 12". Wie bei `B 3` darf hinter der
Zahl weiterer Text stehen (z.B. "B 3 - Wohnungsbrand"). Ein Alarmtext, der selbst
"B 3+" enthält, löst ebenfalls aus.

Platzhalter-Muster ignorieren Groß-/Kleinschreibung (`thl*` trifft "THL Tür").
Rohe `re:`-Ausdrücke gelten genau wie eingegeben.

### Schutz vor Tippfehlern

Nach jedem Anlegen sagt der Bot kurz, was er zu deiner Eingabe weiß. Das Abo
wird in jedem Fall angelegt, die Hinweise sind nur Information:

- ✅ *Passt auf den letzten Alarm* bzw. ℹ️ *Passt nicht auf den letzten Alarm*
- ✅ *Trifft 3 bekannte Einträge: …* bzw. ℹ️ *Diesen Wert kenne ich noch nicht*
  (nur bei Feldern mit Namensliste, und nur wenn schon etwas bekannt ist)

## Namen für RIC und Fahrzeuge

Wer seine RIC kennt, tippt sie ein. Wer lieber nach dem Namen sucht, nutzt
`/description`. Der Bot kennt Namen aus zwei Quellen und fasst sie zusammen:

1. **Eine CSV** (optional) im BOSWatch3-Descriptor-Format, die nur gelesen wird.
2. **Gelernte Namen:** Bei jedem Alarm merkt sich der Bot das Paar RIC und
   Beschreibung. Bei Multicast-Alarmen kommen alle Paare des Pakets dazu.

So funktioniert die Suche:

- `/description Wache` sucht in allen bekannten Namen. Bei genau einem Treffer
  wird sofort abonniert, sonst erscheinen Buttons.
- Abonniert wird immer die **RIC** hinter dem Namen (bei Wache-Zeilen das
  RIC-Muster). Eine spätere Umbenennung des Namens in BOSWatch3 ändert daran
  nichts. In `/abo` steht trotzdem der Name.
- Mit **„Abbrechen“** schließt du die Auswahl, ohne etwas zu abonnieren.
- Die Auswahl sperrt nichts: Findet die Suche nichts, bietet der Bot
  **„Trotzdem als Muster anlegen“** an. Bei mehreren Treffern gibt es zusätzlich
  **„Alle mit … als Muster“**. Mit Platzhalter (`/description *wagen*`) wird
  direkt ein Muster auf die Beschreibung angelegt.

**Gelernte Namen** füllen die Liste ohne Pflege, kennen aber nur, was schon
einmal alarmiert hat. Ein Name aus der CSV hat bei gleicher RIC Vorrang. Ändert
BOSWatch3 den Namen einer gelernten RIC, übernimmt der Bot die Änderung. Ein
Multicast-Paket liefert nur dann Paare, wenn RIC- und Namensliste gleich lang
sind (ein Name mit „, “ darin würde die Zuordnung verschieben). Abschalten
lässt sich das Lernen pro Feld mit `learn: false`.

### CSV einbinden

Die CSV ist dieselbe Datei, die BOSWatch3 im `descriptor`-Modul benutzt. Der
Bot liest sie nur und verändert sie nie. Einbinden geht auf zwei Wegen:

- **Einfach:** Datei als `descriptions_ric.csv` in den Datenordner legen
  (Standard `/opt/bos-telegram-bot/data/`) und den Bot neu starten. Sie wird
  automatisch erkannt. Allgemein gilt `descriptions_<for>.csv`, bei einem Feld
  `for: ric` also `descriptions_ric.csv`.
- **Direkt aus BOSWatch3:** Dessen Config-Ordner in der `docker-compose.yml`
  read-only einbinden (die auskommentierte Zeile unter `volumes:`) und
  `CSV_PATH_RIC=/boswatch3-config/descriptions_ric.csv` setzen.

Die CSV wird beim Start eingelesen, Änderungen greifen nach einem Neustart.

**Kontrolle im Log:** Der Start sagt immer, was mit der CSV passiert ist:

```
INFO    storage.knowledge: CSV eingelesen (Feld ric): 1523 Datensätze (12 Muster) aus /app/data/descriptions_ric.csv
INFO    app: Keine CSV für Feld ric (gesucht: /app/data/descriptions_ric.csv), es werden nur gelernte Namen genutzt
WARNING storage.knowledge: CSV nicht gefunden (Feld ric), es werden nur gelernte Werte genutzt: /boswatch3-config/descriptions_ric.csv
```

Die Zahl nennt alle gelesenen Datensätze, davon wie viele Muster (Regex) sind und, falls es welche gibt,
wie viele Zeilen wegen eines ungültigen Ausdrucks übersprungen wurden (jede mit eigener Warnung).

**Warnung bei einer CSV, die niemand verwendet:** Der Bot sieht beim Start im Datenordner nach (im Container
`/app/data`, nur direkt im Ordner, nicht in Unterordnern). Liegt dort eine `.csv`, die keinem Feld zugeordnet
ist, wird sie nicht still ignoriert, sondern gemeldet, samt dem wahrscheinlichen Grund:

```
WARNING storage.knowledge: CSV /app/data/wachen.csv liegt im Datenordner, wird aber von keinem Feld verwendet und deshalb nicht gelesen. Zuordnen: in 'descriptions_<Feld>.csv' umbenennen (vorhanden: ric), in data/config.yaml bei dem Feld 'csv:' eintragen oder CSV_PATH_<FELD> setzen.
WARNING storage.knowledge: CSV /app/data/descriptions_RIC.csv liegt im Datenordner, wird aber von keinem Feld verwendet und deshalb nicht gelesen. Der Name muss genau 'descriptions_ric.csv' lauten (Kleinschreibung, Endung .csv), dann wird sie automatisch erkannt.
WARNING storage.knowledge: CSV /app/data/descriptions_ric.csv liegt im Datenordner, wird aber von keinem Feld verwendet und deshalb nicht gelesen. Für Feld 'ric' ist stattdessen /boswatch3-config/descriptions_ric.csv eingestellt (CSV_PATH_RIC oder 'csv:' in der Konfiguration).
```

Typische Gründe: ein anderer Dateiname, Groß-/Kleinschreibung (Linux unterscheidet sie), ein Feld, das es nicht
gibt oder keine Namenssuche (`add:`) hat, oder eine andere CSV hat Vorrang (Reihenfolge: Umgebungsvariable,
dann `csv:` in der Konfiguration, dann `descriptions_<Feld>.csv`). Die Dateien selbst werden nicht gelesen oder
verändert, ihr Inhalt landet nie im Log.

```
for,add,isRegex
1234567,Rettungswagen Musterstadt A-Wehr,false
^23456([0-9]{2})$,Feuerwehr Musterstadt,true
```

- `for`: die RIC oder, bei `isRegex=true`, ein regulärer Ausdruck für mehrere RICs
- `add`: der Name (bei Regex-Zeilen optional mit Platzhaltern, siehe unten)
- `isRegex`: `true` oder `false`, die Spalte darf bei normalen Zeilen auch fehlen

Eine Vorlage liegt in [`examples/descriptions_ric.csv`](examples/descriptions_ric.csv).
Alle Namen und Nummern in den Beispielen sind frei erfunden.

**Platzhalter in Regex-Zeilen:** Bei `isRegex=true` darf der Name Gruppen aus
dem Muster enthalten (`\1`, `\2`, ...). Der Bot setzt sie ein:

```
for:     ^23456([0-9]{2})$
add:     Feuerwehr Musterstadt \1
Alarm-RIC: 2345625
Anzeige: Feuerwehr Musterstadt 25
```

## Konfiguration anpassen (optional)

Der Bot läuft ohne eigene Konfigurationsdatei. Die Standardwerte stecken im
Image und sind bei jedem Update aktuell: die Befehle `/ric` (mit Namenssuche
über `/description`), `/message` und `/subrictext`. Anpassen kannst du auf zwei
Wegen.

**A) Neue Felder per Umgebungsvariable (der einfache Weg)**

```
EXTRA_FIELDS=stadtteil,objekt
```

Für jedes genannte Feld aus dem Alarm-JSON entsteht ein Befehl (z.B.
`/stadtteil Nord*`). Das ist der typische Fall, wenn das BOSWatch3-Modul
`descriptor` zusätzliche Felder in den Alarm schreibt. Welche Felder es gibt,
zeigt `/lastraw`: Felder, die im Alarm vorkommen, aber noch keinen Befehl
haben, werden dort samt fertigem `EXTRA_FIELDS`-Wert aufgelistet. Die
Schreibweise muss exakt zum JSON passen (Groß-/Kleinschreibung).

**B) Eigene Datei `data/config.yaml` (fortgeschritten)**

Die Datei enthält **nur die Abweichungen** und wird über die Standardwerte
gelegt. Jeder Eintrag unter `fields:` steht für ein Feld des Alarm-JSON:

```yaml
fields:
  - for: ric                        # Feld im JSON, daraus wird /ric
    csv: /boswatch3-config/descriptions_ric.csv
  - for: stadtteil                  # neues Feld, daraus wird /stadtteil
    label: "Stadtteil"
  - for: subricText
    remove: true                    # Standardfeld nicht anbieten
```

| Option        | Bedeutung                                                              |
|---------------|-------------------------------------------------------------------------|
| `for`         | Name des JSON-Feldes. Daraus entsteht der Befehl (Pflicht)               |
| `label`       | Anzeigename in Meldungen (Standard: der Feldname)                        |
| `match`       | Eingabe ohne Platzhalter: `exact` (genau) oder `contains` (enthält, Standard) |
| `command`     | anderer Befehlsname statt des Feldnamens                                 |
| `add`         | zweites Feld, das einen Namen ergänzt (z.B. `description`). Ergibt eine Namenssuche |
| `add_label`, `add_command` | Anzeigename bzw. Befehlsname dafür                          |
| `csv`         | CSV mit Namen, siehe [Namen für RIC und Fahrzeuge](#namen-für-ric-und-fahrzeuge) |
| `learn`       | Namen aus Alarmen merken (Standard `true`)                               |
| `remove: true`| entfernt einen Standardeintrag                                           |

Wie Einträge zusammengeführt werden:

- Gleiches `for` ändert oder ergänzt den Standardeintrag, alles andere an ihm
  bleibt erhalten. `null` entfernt eine einzelne Angabe (`add: null`).
- Ein neues `for` hängt einen Eintrag an. `remove: true` löscht einen.
- Nur dein `fields:` wird so zusammengeführt. Andere Blöcke wie `mqtt:` werden
  Wert für Wert überlagert, `notification_fields:` ersetzt die Liste komplett (die Nachricht selbst stellst du mit
  `notification.template` ein, siehe [Nachricht anpassen](#nachricht-anpassen)).

Eine Vorlage mit Kommentaren liegt in
[`examples/config.override.example.yaml`](examples/config.override.example.yaml),
alle Optionen stehen in der mitgelieferten [`config.yaml`](config.yaml).

- Die Datei wird vom Bot **nie angelegt oder überschrieben**. Nach einer
  Änderung den Bot neu starten, die Config wird nur beim Start gelesen.
- Fehler meldet der Bot beim Start im Log mit der betroffenen Stelle (z.B.
  `fields[for: ric]: match muss 'exact' oder 'contains' sein`) und startet dann
  nicht.
- Zugangsdaten (Token, MQTT-Passwort) gehören nicht in diese Datei, dafür sind
  die Umgebungsvariablen des Stacks da.

Die Reihenfolge, in der Werte gelten (später gewinnt): Standardwerte, deine
`data/config.yaml`, Umgebungsvariablen.

## Nachricht anpassen

Der Aufbau der Alarm-Nachricht ist eine **Vorlage** mit Platzhaltern. Ohne eigene
Einstellung gilt diese und liefert bei einem Multicast-Alarm zum Beispiel:

```
🚨 BOS-ALARM 🚨

Lagedienst Musterstadt
Rettungswagen Musterstadt
TEST Beispieltext

abonniert über: RIC: 1000011
```

Eine eigene Vorlage kommt in die `data/config.yaml` (Neustart nötig). Wichtig ist das `|`
hinter `template:`, sonst liest YAML die geschweiften Klammern als etwas anderes:

```yaml
notification:
  template: |
    🚨 <b>BOS-ALARM</b> 🚨

    {DESCRIPTION_LIST|RIC_LIST}
    {MESSAGE}

    <i>abonniert über: {MATCHED}</i>
```

Das ist zugleich die Standard-Vorlage. Die Platzhalter schreibst du wie bei BOSWatch3 in
Großbuchstaben, die Schreibweise des Feldes im Alarm ist egal (`{SUBRICTEXT}` findet `subricText`).

| Platzhalter        | Bedeutung |
|--------------------|-----------|
| `{FELD}`           | Wert eines Feldes aus dem Alarm, z.B. `{MESSAGE}`, `{RIC}` |
| `{FELD_LIST}`      | alle Werte eines Multicast-Alarms (BOSWatch3-Feld `feld_list`), **eine Zeile pro Wert**. Leere und doppelte Einträge entfallen. Gibt es keine Liste, gilt der einzelne Wert |
| `{MATCHED}`        | Name des passenden Abos |
| `{A\|B}`           | Rückfall: der erste Platzhalter, der nicht leer ist, z.B. `{DESCRIPTION_LIST\|RIC_LIST}` |
| `{{` und `}}`      | wörtliche geschweifte Klammern |

So werden Zeilen behandelt:

- **Leere Platzhalter:** Ist in einer Zeile jeder Platzhalter leer, entfällt die Zeile, auch
  mit festem Text drumherum (`Kennung: {SUBRICTEXT}` verschwindet ohne Wert). Mehrere leere
  Zeilen in Folge werden zu einer.
- **Listen:** Enthält eine Zeile genau eine `{..._LIST}`, wird sie für jeden Eintrag
  wiederholt, `• {DESCRIPTION_LIST}` ergibt also je Eintrag einen Aufzählungspunkt. Stehen mehrere
  Listen in einer Zeile, werden ihre Einträge mit `, ` verbunden.
- **Rückfall:** `{DESCRIPTION_LIST|RIC_LIST}` zeigt die Beschreibungen und nur dann die RICs,
  wenn BOSWatch3 keine Beschreibung liefert. Ein Tippfehler im Feldnamen macht den Platzhalter
  leer, die Zeile entfällt dann.
- **Sicherheit:** Werte aus dem Alarm werden automatisch HTML-sicher gemacht. Der Text der Vorlage
  selbst ist Telegram-HTML (`<b>`, `<i>`, `<code>`, `<a>` …). Ein `&` darin schreibst du als `&amp;`.
  Ungültiges HTML, falsche Klammern oder unbekannte Tags meldet der Bot beim Start mit allen
  Fehlern, statt Nachrichten zu verlieren. Ergibt die Vorlage überhaupt keinen Text, gilt die
  Standard-Vorlage.

Beispiele:

```yaml
notification:
  template: |
    🚨 <b>{MESSAGE}</b>

    • {DESCRIPTION_LIST|RIC_LIST}

    <i>{MATCHED}</i>
```

```yaml
notification:
  template: |
    <b>{MESSAGE}</b>
    {DESCRIPTION_LIST|RIC_LIST}
    Ort: {STADTTEIL}
```

Das zweite Beispiel zeigt ein Feld, das etwa das BOSWatch3-Modul `descriptor` ergänzt. Fehlt es in einem
Alarm, entfällt die Zeile. Welche Felder es gibt, zeigt `/lastraw`.

Die ältere Einstellung `notification_fields: [description, message, ric]` (je Feld eine Zeile) gilt weiter,
`notification.template` hat aber Vorrang. Namen, die selbst ein `, ` enthalten, zeigt die Nachricht auf
zwei Zeilen, weil BOSWatch3 seine Listen mit demselben Trennzeichen bildet.

## Multicast-Alarme

BOSWatch3 kann mehrere Empfänger eines Alarms zu einem Paket zusammenfassen
(Modul `multicast`). In so einem Paket nennen `ric`, `description` usw. nur
**einen** der Empfänger, alle stehen in den zugehörigen `*_list`-Feldern,
kommagetrennt (z.B. `ric_list`). Der Bot berücksichtigt das für **jedes** Feld
automatisch, ohne Konfiguration:

- Ein Abo löst aus, wenn der Wert im Feld **oder** in der zugehörigen Liste
  vorkommt. Wer eine Wache abonniert hat, bekommt den Alarm auch, wenn sie nicht
  der zuletzt genannte Empfänger im Paket ist.
- Bei mehreren Empfängern zeigt die Nachricht alle Einträge, je einer pro Zeile (Aufbau einstellbar, siehe
  [Nachricht anpassen](#nachricht-anpassen)).
- Pro Chat kommt **eine** Nachricht, auch wenn mehrere Abos passen. Unten steht
  "abonniert über: …" mit allen passenden Abos.

Einschränkung: Enthält ein Name selbst ein ", ", teilt die Nachricht ihn auf
zwei Zeilen auf, weil BOSWatch3 die Liste mit demselben Trennzeichen
zusammensetzt. Der Abo-Treffer funktioniert trotzdem.

## Doppelte Alarme

Derselbe Alarm kann mehrfach beim Bot ankommen, ohne dass etwas kaputt ist:

- Eine **Automation** (z.B. in Home Assistant, die für jede RIC einen Sensor anlegt) veröffentlicht den
  Alarm kurz nach dem Original noch einmal auf dasselbe Topic.
- Ein **Multicast mit mehreren Empfängern** wird auf das Topic jedes Empfängers veröffentlicht.

Beides zusammen ergibt bei zwei Empfängern vier Nachrichten für einen einzigen Einsatz. Der Bot erkennt das am
Zeitstempel des Alarms (`timestamp_list`, sonst `timestamp`), der in allen Kopien gleich ist, und stellt den
Alarm **an jeden Chat nur einmal** zu.

- Was Telegram angenommen hat, gilt als zugestellt. Scheitert das Senden (z.B. Zeitüberschreitung), bekommt
  dieser Chat die **nächste Kopie** des Alarms als neuen Versuch. Chats, bei denen es klappte, nicht noch einmal.
- Kommt nur die Kopie an und das Original fehlt, wird die Kopie ganz normal zugestellt. Es geht nichts verloren.
- Wiederholungen erscheinen nur im DEBUG-Log, ändern `/lastraw` nicht und werden nicht noch einmal gelernt.
- Hat ein Alarm keinen Zeitstempel, wird er nie als doppelt erkannt. Lieber doppelt als verloren.
- Das Gedächtnis liegt nur im Speicher. Nach einem Neustart beginnt es leer.

```yaml
# data/config.yaml
duplicates:
  remember_seconds: 300    # Standard. 0 = nicht erkennen, jede Nachricht zustellen
```

Kommen Alarme mehr als `remember_seconds` später noch einmal an, gelten sie als neu.

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

## Gruppen mit eigenen Abos

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

## Logs

Der Bot schreibt auf die Standardausgabe. In Portainer: Container → **Logs**, auf der Kommandozeile
`docker logs -f bos-telegram-bot`.

**INFO** (Standard) zeigt eine kurze Zeile pro Ereignis:

```
2026-10-09 08:56:54+0200 INFO    mqtt.listener: Verbunden mit MQTT-Broker 192.168.1.100:1883, lausche auf Topic homeassistant/boswatch/alarm/+
2026-10-09 08:57:28+0200 INFO    mqtt.dispatch: Alarm 1234567 (Wache Nord): 2 Abo(s) in 1 Chat(s), gesendet 1/1
2026-10-09 08:58:03+0200 INFO    mqtt.dispatch: Alarm 1234599 (Wache Süd): kein passendes Abo
2026-10-09 08:58:40+0200 WARNING mqtt.dispatch: Senden an Chat -1001234567890 fehlgeschlagen (TimedOut: Timed out)
2026-10-09 08:58:40+0200 WARNING mqtt.dispatch: Alarm 1234567 (Wache Nord): 1 Abo(s) in 1 Chat(s), gesendet 0/1
2026-10-09 08:59:12+0200 INFO    chat.fields: Abo gesetzt: Chat 123456789, Feld ric, Wert '1234567'
```

So liest man die Alarmzeile: `2 Abo(s) in 1 Chat(s), gesendet 1/1` heißt, zwei Abos passen, beide gehören zu
einem Chat, und Telegram hat die eine Nachricht angenommen. Pro Chat geht immer genau eine Nachricht
raus. Steht dort `gesendet 0/1`, kam die Nachricht nicht durch, der Grund steht in der Zeile davor.
`TimedOut` heißt, dass Telegram nicht rechtzeitig geantwortet hat. Die Nachricht kann trotzdem angekommen
sein.

Außerdem steht im INFO-Log, wer wann Abos angelegt oder entfernt hat und wer von einem Admin freigeschaltet,
abgelehnt oder gesperrt wurde (jeweils mit Telegram-ID, ohne Namen).

Trifft derselbe Alarm mehrfach ein (siehe [Doppelte Alarme](#doppelte-alarme)), steht nur die erste Zeile im
INFO-Log. Die Wiederholungen erscheinen im DEBUG-Log, nur ein Neuversuch nach einem Fehler hat wieder eine
INFO-Zeile (`Alarm …: Wiederholung, 1 Chat(s) noch offen, gesendet 1/1`).

**Der Alarmtext steht bewusst nie im INFO-Log**, er kann Adressen und andere persönliche Angaben enthalten.
Die Alarmzeile nennt nur RIC und Name.

**DEBUG** zeigt zusätzlich die Rohdaten, zum Beispiel für die Frage "warum kam der Alarm nicht an?":

- jede Nachricht vom Broker mit Topic, QoS, retain-Flag und dem kompletten Payload
- warum etwas ignoriert wurde (kein JSON, leere Nachricht)
- welches Abo auf welchen Wert gepasst hat
- die fertige Nachricht pro Chat und wie lange Telegram zum Annehmen gebraucht hat
- jede Anfrage des Bots an Telegram, auch das regelmäßige Abfragen neuer Nachrichten (deshalb viele Zeilen)

DEBUG enthält die Alarmtexte. Nur zur Fehlersuche einschalten und das Log danach nicht aufbewahren.

**Einstellen:** Umgebungsvariable `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) oder in
`data/config.yaml`:

```yaml
logging:
  level: DEBUG
```

Die Umgebungsvariable gewinnt. Nach einer Änderung den Container neu starten. Ein ungültiger Wert
stoppt den Start mit einer Erklärung.

**Zeitzone:** Die Zeiten stehen in der Zeitzone des Containers, ohne Angabe ist das UTC (`+0000`). Für
Ortszeit die Variable `TZ` setzen, z.B. `Europe/Berlin`. Der Versatz steht in jeder Zeile, deshalb lassen
sich die Zeiten auch mit denen des Brokers vergleichen.

**Das Token des Bots wird in jeder Logzeile geschwärzt** (`bot<TOKEN>`), auch in Fehlermeldungen der
verwendeten Bibliotheken. Ein Log lässt sich deshalb gefahrlos für eine Fehlersuche weitergeben, der
Alarminhalt bei DEBUG aber nicht.

## Datensicherung und was der Bot schreibt

Alles, was der Bot sich merkt, liegt im Datenordner (Standard
`/opt/bos-telegram-bot/data`):

| Datei               | Inhalt                                                     |
|---------------------|--------------------------------------------------------------|
| `bot_db.sqlite3`    | Abos aller Chats, freigeschaltete User und Anfragen, gelernte Namen |
| `config.yaml`       | deine Anpassungen (nur falls du eine angelegt hast)          |
| `*.csv`             | deine Namenslisten (nur falls du sie hier abgelegt hast)     |

Token, Admin-IDs und MQTT-Zugang stehen nicht dort, sie kommen aus den
Umgebungsvariablen des Stacks und sollten separat notiert sein.

Der Bot **schreibt ausschließlich `bot_db.sqlite3`**. CSV-Dateien und die
Config liest er nur, auch wenn sie aus einem fremden, read-only eingebundenen
Ordner wie dem von BOSWatch3 stammen.

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

## Update von einer früheren Version

- **Alte `data/config.yaml`:** Frühere Versionen haben `fields:` als Zuordnung
  mit `mode: lookup/pattern` geschrieben. Das neue Format ist eine Liste (siehe
  oben). Der Bot erkennt die alte Datei beim Start, meldet es im Log und startet
  nicht. Hattest du dort nichts Eigenes eingetragen, genügt es, die Datei zu
  löschen. Eigene Werte (z.B. MQTT-Topic oder zusätzliche Felder) übernimmst du
  im neuen Format.
- **`/subric_text`** heißt jetzt `/subrictext` (der Befehl folgt dem Feldnamen).
  Bestehende Abos werden beim Start automatisch umgestellt.
- **`/ric` sucht nicht mehr in der CSV.** Die RIC gibst du direkt ein, nach
  Namen suchst du mit `/description`. Bestehende Abos bleiben gültig.
- **CSV:** `data/descriptions_ric.csv` wird weiterhin automatisch erkannt.
  `CSV_PATH_DESCRIPTION` funktioniert noch, `CSV_PATH_RIC` genügt aber.
- Der Bot legt keine leere CSV mehr an. Die Namen füllen sich aus den Alarmen.

## Grenzen der Telegram-Autovervollständigung

Telegram zeigt im "/"-Menü automatisch alle registrierten Befehle mit
Beschreibung an (`set_my_commands` in `bos_telegram_bot/app.py`) — das ist aber nur eine
Vervollständigung des **Befehlsnamens**. Eine Live-Vorschlagsliste *während*
du z.B. bei `/description Mus` tippst, unterstützt Telegram für normale
Nachrichten nicht. Die aktuelle Lösung (Eingabe abschicken → bei mehreren
Treffern erscheint eine Buttons-Liste) ist der pragmatische Ersatz dafür.

Echtes "Tippen und sofort Vorschläge sehen" ginge nur über Telegrams
**Inline-Mode** (`@BotName suchtext` in einem beliebigen Chat). Das ist
derzeit nicht umgesetzt, es bräuchte einen eigenen Inline-Handler und die
Aktivierung bei BotFather.

## Entwicklung

Die Tests brauchen nur Python und die Pakete aus `requirements.txt`:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

### Aufbau

```
main.py                    dünner Einstieg (das Dockerfile startet diese Datei)
config.yaml                Standardkonfiguration, steckt im Image
bos_telegram_bot/
  app.py                   Verdrahtung: Konfiguration, Datenbank, Telegram-Befehle, Start
  config.py                Konfiguration laden, zusammenführen und prüfen
  logs.py                  Logging: Format, Pegel, Schwärzen des Tokens
  core/                    reine Logik, ohne Telegram und ohne Datenbank
    matching.py              Muster, Platzhalter, "Zahl+", Abgleich mit Multicast-Listen
    delivery.py              erkennt doppelte Alarme, merkt sich die schon belieferten Chats
    template.py              Aufbau der Alarm-Nachricht
  storage/
    database.py              SQLite: Abos, Benutzer, gelernte Namen
    knowledge.py             CSV und gelernte Namen
  mqtt/
    listener.py              Verbindung zum Broker, nimmt Alarme entgegen
    dispatch.py              gleicht ab, baut die Nachricht, sendet sie
  chat/                    Telegram-Seite
    access.py                Admins, Freigabe, Zugriffsanfragen
    fields.py                /ric, /message ... und die Namenssuche /description
    prompts.py               Rückfrage-Dialog
    hints.py                 Hinweise gegen Tippfehler
    overview.py              /start, /lastraw, /abo
    admin.py                 /users, /adduser
    buttons.py               Klicks auf Inline-Buttons
    common.py                kleine Hilfen
tests/                     spiegelt diese Struktur
```

Die Schichten bauen aufeinander auf: `core` kennt weder Telegram noch die Datenbank, `storage` kennt
kein Telegram, `mqtt` und `chat` nutzen beide `core` und `storage`, kennen sich aber nicht. `logs.py` ist
neutral und darf von überall genutzt werden. Ein Test
(`tests/test_architecture.py`) hält diese Regeln ein.

## Für Maintainer: Image veröffentlichen

Der Workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)
baut das Image automatisch:

- Push auf `main` → Tag `latest`
- Git-Tag `v1.2.3` → Tags `1.2.3` und `1.2` (für Nutzer, die eine feste Version wollen)

Einmalig nach dem ersten erfolgreichen Lauf: Auf GitHub unter *Packages →
bos-telegram-bot → Package settings* die Sichtbarkeit auf **Public** stellen,
sonst können andere das Image nicht ohne Login ziehen.

