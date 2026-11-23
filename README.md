# fbsave — Beweissicherung von Facebook-Kommentaren

Zusätzlich steht ein unabhängiger, wiederaufnehmbarer **Messenger-Exporter**
für genau einen ausdrücklich angegebenen Chat zur Verfügung (siehe unten).

Dokumentiert alle Kommentare (inkl. Antworten und Antworten auf Antworten)
unter einem öffentlichen Facebook-Post für eine Strafanzeige: Einzel-Screenshot
pro Kommentar, Gesamt-Screenshot, CSV und JSON mit Autor/Profillink/Zeitstempel/Text
sowie ein `manifest.json` mit UTC-Zeitstempeln des Laufs und SHA256-Hashes
aller Dateien.

Technik: Playwright steuert Chromium auf `www.facebook.com`. (`mbasic.` und
`m.facebook.com` werden von Meta inzwischen dorthin umgeleitet — der frühere
mbasic-Ansatz funktioniert nicht mehr.) Kommentare sind `div[role="article"]`-
Elemente; Autor und Relativzeit stehen im `aria-label`, die **exakte**
Kommentarzeit (Unix-Epoch) wird aus dem von Facebook mitgelieferten
Relay-/GraphQL-JSON geerntet.

## Setup & Start (empfohlen: uv)

Die Abhängigkeiten stehen als Inline-Metadaten (PEP 723) im Skript —
[uv](https://docs.astral.sh/uv/) kümmert sich damit automatisch um Python,
virtualenv und Playwright; fehlendes Chromium lädt das Skript beim ersten
Start selbst nach:

```bash
brew install uv      # einmalig (oder: curl -LsSf https://astral.sh/uv/install.sh | sh)

cd ~/dev/fbsave
uv run fbsave.py     # das ist alles
```

<details>
<summary>Alternative: klassisches venv (ohne uv)</summary>

```bash
python3 -m venv .venv
./.venv/bin/pip install playwright
./.venv/bin/python -m playwright install chromium
./.venv/bin/python fbsave.py
```
</details>

## Verwendung

```bash
# Standard: sichtbarer Browser, knappe Fortschrittsanzeige,
# Ziel-URL ist als Default im Skript hinterlegt
uv run fbsave.py

# Unsichtbar / andere URL / Schnelltest des Zeitparsers
uv run fbsave.py --headless
uv run fbsave.py --url "https://www.facebook.com/..."
uv run fbsave.py --selftest

# Fehlersuche: ausführliches Log + Debug-Dateien (Screenshots/HTML je Schritt)
uv run fbsave.py --debug
```

Die normale Terminal-Ausgabe zeigt nur den Fortschritt und am Ende, was
gesichert wurde und wo es liegt:

```
🎯 Post: https://www.facebook.com/...
🍪 Cookie-Hinweis bestätigt
↕️  Sortierung umgestellt auf: Alle Kommentare
✅ Keine weiteren Kommentare nachgeladen — 214 Kommentar-Elemente sichtbar (38 Runden)
✅ 187 Kommentare/Antworten erfasst

📊 Ergebnis: 187 Beiträge gesichert — 142 Kommentare, 45 Antworten
   Zeitstempel: 185 exakt (von Facebook geliefert), 2 aus Relativzeit berechnet

📁 Ordner:       captures/capture_20260610T120301Z/
📄 Tabelle:      captures/capture_20260610T120301Z/comments.csv
📄 JSON:         captures/capture_20260610T120301Z/comments.json
🖼️  Screenshots:  187 × comment_NNN.png + full_page.png
🔏 Manifest:     captures/capture_20260610T120301Z/manifest.json (UTC-Zeitstempel + SHA256-Hashes)
```

### Login

Eine in `state.json` gespeicherte Session wird automatisch benutzt (erzeugen
mit `uv run save_login.py`: manuell einloggen, Enter drücken).
Ohne Login zeigt Facebook je nach Region nur eine Login-Wand — das Skript
erkennt das und bricht mit Hinweis ab. `state.json` enthält Session-Cookies —
nicht weitergeben (steht in `.gitignore`).

## Ablauf des Skripts

1. Post laden (URL wird auf `www.facebook.com` normalisiert)
2. Cookie-Consent wegklicken, falls vorhanden (nur ohne Login), danach neu laden
3. Kommentar-Sortierung auf **„Alle Kommentare"** umstellen (sonst blendet
   „Relevanteste zuerst" Kommentare aus!)
4. „Weitere Kommentare ansehen", „Alle X Antworten ansehen" und „Mehr anzeigen"
   anklicken und bis ans Ende der Kommentarliste scrollen, bis keine weiteren
   Kommentare und Antworten nachladen
5. Gesamt-Screenshots, dann Extraktion pro `role=article`:
   Autor + Relativzeit aus `aria-label`, Profillink, Kommentar-ID aus den
   Links, Text, Bild-Alt-Texte, Einzel-Screenshot
6. Exakte Zeitstempel aus den gesammelten GraphQL-Antworten zuordnen

## Ausgabe (`captures/capture_<UTC>/`)

| Datei | Inhalt |
|---|---|
| `comments.csv` | `index, comment_id, is_reply, parent_id, parent_author, author, profile_url, timestamp_raw, timestamp_utc, timestamp_source, text, attachment_alt, screenshot_file, aria_label` (UTF-8-BOM, Excel-tauglich) |
| `comments.json` | JSON-Array mit allen Feldern aus `comments.csv` und verschachtelten Antworten in `replies` (UTF-8, zwei Leerzeichen Einrückung) |
| `comment_NNN.png` | Einzel-Screenshot pro Kommentar/Antwort |
| `full_page.png`, `full_dialog.png` | Gesamt-Screenshots |
| `manifest.json` | URLs, Seitentitel, Start/Ende UTC, Versionen, Zähler, SHA256 jeder Datei |

In `comments.json` enthält `replies` die direkten Antworten eines Eintrags.
Antworten auf Antworten haben wiederum ein eigenes `replies`-Array. Wenn die
`parent_id` fehlt, der Elternkommentar nicht erfasst wurde oder `parent_author`
dem Autor der gefundenen Zeile widerspricht, bleibt die Antwort auf oberster
Ebene. `index` verweist weiterhin auf ihre Position in `comments.csv`.

### Debug-Dateien (nur mit `--debug`, ebenfalls gehasht)

| Datei | Inhalt |
|---|---|
| `debug_NN_<schritt>.png/.html` | Screenshot + kompletter HTML-Dump nach jedem Schritt |
| `debug_structure_*.json` | DOM-Diagnose: Anzahl Artikel, alle aria-Labels, alle Button-Texte, Dialoge — zeigt sofort, welcher Selektor nicht greift |
| `debug_graphql_times.json` | alle geernteten exakten Zeitstempel je Kommentar-ID |
| `debug_skipped_arias.json` | aria-Labels, die keinem Kommentar-Muster entsprachen (nur falls vorhanden) |

`probe.py` ist ein eigenständiges Diagnose-Werkzeug, das nur die DOM-Struktur
dumpt (`probe_out/`), ohne eine Beweissicherung zu erzeugen.

## Wichtige Hinweise zur Beweiskraft

- **Zeitstempel:** `timestamp_source=graphql_exact` heißt: exakte, von Facebook
  selbst gelieferte Kommentarzeit (sekundengenau, UTC). Nur wenn keine
  Kommentar-ID zuordenbar ist, wird die angezeigte Relativzeit
  (`timestamp_raw`, auch auf den Screenshots sichtbar) relativ zur Capture-Zeit
  umgerechnet (`computed_from_relative`, Näherung). Das Manifest zählt beide
  Quellen aus.
- **Bild-Kommentare:** Kommentare ohne Text (nur Bild/GIF/Sticker) haben ein
  leeres `text`-Feld; das Bild ist im Einzel-Screenshot gesichert und sein
  Alt-Text steht in `attachment_alt`.
- **Hashes:** `manifest.json` wird zuletzt geschrieben und enthält SHA256 aller
  übrigen Dateien. Verzeichnis nach dem Lauf nicht mehr verändern; ideal sofort
  archivieren (ZIP) und den Manifest-Hash separat notieren/versenden.
- **Sortierung:** Wenn das Umstellen auf „Alle Kommentare" fehlschlägt, warnt
  das Skript — dann können von Facebook ausgeblendete Kommentare fehlen
  (`manifest.json: sorted_to_all_comments`).
- **Nachladen:** Wenn `--max-rounds` erreicht ist, speichert fbsave die bereits
  geladenen Beiträge und beendet den Lauf mit einem Fehlercode. Das Manifest
  enthält dann `comment_loading_finished: false`.
- Auch bei Fehlern/Abbruch (Strg-C) werden CSV, JSON und Manifest mit den bis dahin
  gesammelten Daten geschrieben.
- Facebook ändert sein Markup regelmäßig. Wenn ein Lauf 0 Kommentare liefert:
  mit `--debug` erneut laufen lassen — `debug_structure_*.json` und die
  HTML-Dumps zeigen, welche aria-Labels und Button-Texte aktuell verwendet
  werden.

## Messenger: einen Chat lokal archivieren

```bash
# Session wie oben manuell speichern; keine Anmeldung/Zugangskontrolle umgehen.
uv run save_login.py

# Zuerst nur Text und Anhang-Metadaten, sichtbarer Browser:
uv run fbsave.py messenger \
  --url "https://www.facebook.com/messages/t/CHAT_ID" \
  --output messenger_exports/mein-chat --no-media

# Fortsetzen, erneut den Verlauf durchlaufen und Medien nachholen:
uv run fbsave.py messenger \
  --url "https://www.facebook.com/messages/t/CHAT_ID" \
  --output messenger_exports/mein-chat --resume
```

Ohne `--no-media` werden direkt verfügbare Anhänge sofort nach jeder Laderunde
gesichert. Unterstützt werden explizite HTTPS-Thread-URLs mit
`facebook.com/messages/t/ID`, `facebook.com/messages/e2ee/t/ID`,
`messenger.com/t/ID` und `messenger.com/e2ee/t/ID`. Query und Fragment werden
verworfen. Der Modus benötigt eine vorhandene `state.json`; mit `--state DATEI`
kann eine andere Session gewählt werden. Bei fehlender/abgelaufener Session,
fehlendem Zugriff, Login-/PIN-/Captcha-Sperren oder unbekanntem DOM wird
abgebrochen und das bisherige Archiv erhalten. Die Freigabe verschlüsselter
Nachrichten muss der Benutzer gegebenenfalls manuell im regulären Messenger
erledigen. `state.json` allein kann für E2EE-Verläufe unzureichend sein.

Weitere Optionen:

| Option | Bedeutung |
|---|---|
| `--headless` | Unsichtbares Chromium (Standard: sichtbar) |
| `--max-rounds 2000` | Globales Limit der Scrollrunden pro Lauf |
| `--idle-seconds 15` | Mindest-Ruhezeit an jeder Scrollgrenze; bei langsamem Laden erhöhen |
| `--poll-ms 250` | Abstand zwischen Journal-Abfragen; vier Abfragen pro Scrollrunde |
| `--max-media-mb 50` | Maximale gespeicherte Größe eines Anhangs in MiB |
| `--debug` | Ausschließlich Anzahl, Laderunden und Status; keine Nachrichten oder DOM-Dumps |

Der Kommentar-Modus mit seinen bisherigen Argumenten und Debug-Dateien bleibt
unverändert. `messenger` muss direkt hinter `fbsave.py` stehen. Python ≥ 3.9,
Playwright ≥ 1.45 und installiertes Chromium sind erforderlich. Anders als der
Kommentar-Modus lädt Messenger Chromium nicht automatisch nach:
`uv run --with playwright python -m playwright install chromium`.
Die Archiv-Dateisperre nutzt `fcntl` (macOS/Linux; Windows wird derzeit nicht
unterstützt).

### Erfassung, Vollständigkeit und Wiederaufnahme

Der Sammler liest ausschließlich `role="row"` innerhalb genau eines
`data-scope="messages_table"` auf der angegebenen Thread-URL. Er liest keine
Chatliste aus, navigiert nicht in andere Chats und bricht bei mehreren
Nachrichtenlisten ab. Ein vor dem Seitenstart installierter
[MutationObserver über Playwright](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-add-init-script)
sammelt auch entfernte Zeilen virtualisierter Listen. Der Export scrollt mit
überlappenden Schritten zuerst nach oben, dann nach unten und fragt laufend
nach Änderungen. Er klickt keine Buttons, fokussiert keinen Editor und löst
keine Nachrichtenaktionen aus. Es werden keine privaten Messenger-APIs oder
GraphQL-Endpunkte angesprochen.

Beobachtungen werden als JSONL mit `flush`/`fsync` gesichert, bevor der
Browserpuffer bestätigt wird. Strg-C und SIGTERM erzeugen JSON, CSV und Manifest
aus den bis dahin gesicherten Daten. Bei SIGKILL/Stromausfall bleibt das Journal
Wiederherstellungsquelle; eine unvollständige letzte Zeile darf beim Fortsetzen
entfernt werden. Bereits vollständig geschriebene, beschädigte Zeilen führen
zum Abbruch. Noch ausschließlich im Browserpuffer befindliche Beobachtungen
können bei einem harten Prozessabbruch verloren gehen. Der Puffer ist begrenzt;
ein erkannter Überlauf führt zu einem Fehler statt zu einer Erfolgsmeldung.

`--resume` ist für einen vorhandenen Ausgabeordner erforderlich und überprüft
Chat-ID und Formatversion. Pro Ordner darf nur ein Exporter schreiben.
Die Wiederaufnahme liest das Journal ein und durchläuft den Chat **erneut**,
da DOM-Scrollpositionen keine stabilen Server-Cursor sind. Alte Nachrichten
bleiben erhalten, auch wenn sie im neuen Lauf fehlen. Änderungen unter
derselben Nachrichten-ID werden mit vorherigem Text und Anhangsmetadaten in
`revisions` erhalten;
das ist eine Beobachtungshistorie, keine von Messenger bestätigte Edit-Historie.
Der Checkpoint speichert die Laufparameter und Ladegrenzen, nicht die Session.

Exitcodes: `0` = beide Scrollgrenzen ohne beobachtete Änderungen erreicht,
`2` = Rundenlimit oder leerer, nicht verifizierter Verlauf, `1` = Fehler,
`130` = unterbrochen. Auch Exitcode 0 garantiert keine Vollständigkeit.
`manifest.json: completeness` bleibt immer `unverified`. `top_stable`,
`bottom_stable`, Status, Runden, Parser- und Anhangszähler dokumentieren die
beobachtete Abdeckung. Nachladevorgänge, die länger als die gewählte Ruhezeit
dauern, können fehlen. Höhere Limits und ein zweiter Lauf helfen bei der
Plausibilitätsprüfung, beweisen aber keine vollständige Historie.

### Identität, Zeitstempel und Ausgabe

Nachrichten-IDs werden aus `data-message-id`, `data-mid` oder einem
Nachrichten-Permalink desselben Chats bevorzugt. `id_source` dokumentiert die
Quelle. Fehlt eine ID, wird SHA-256 aus Absender, verfügbarem Zeitwert, Text und
Anhangsbeschreibung gebildet; eine laufende Vorkommensnummer innerhalb eines
DOM-Snapshots erhält gleichzeitig sichtbare identische Nachrichten.
**Identische Nachrichten in verschiedenen virtuellen Ausschnitten können
zusammenfallen.** Wechselnde Metadaten oder später sichtbare IDs können
umgekehrt Duplikate erzeugen. Der Fallback ist keine verlässliche Messenger-ID;
`deduplication` und `fallback_count` machen seine Verwendung sichtbar.

Datum, Uhrzeit, Absender und Text werden aus expliziten deutschen/englischen
ARIA-Labels (z. B. `Um 16. Oktober 2026, 11:53, Du: ...`) oder vorhandenen
DOM-Metadaten gelesen. Labels wie `Eingabe, Nachricht gesendet: ...` gelten
als Hilfslabels, nicht als zusätzliche Nachricht. Nicht interpretierbare
Zeilen bleiben als `parse_status: unparsed` mit beobachtetem Text erhalten.
ARIA-Zeiten enthalten keine gesicherte Zeitzone: `timestamp_local` bleibt ohne
Offset und `timestamp_utc` leer. Nur explizite Epoch-/Offset-Metadaten erlauben
eine UTC-Zuordnung. Es wird kein Jahr und keine Zeitzone geraten. Die Ausgabe
sortiert nach verfügbaren Zeitwerten; bei unbekannten oder gemischten Zeitzonen
ist die Reihenfolge nur eine Anzeigehilfe. Die Journalreihenfolge ist die
Beobachtungsreihenfolge, keine behauptete Nachrichtenchronologie.

| Datei | Inhalt |
|---|---|
| `messages.jsonl` | Fortlaufendes Journal der neuen/geänderten Datensätze einschließlich Medienstatus |
| `checkpoint.json` | Chat-Bindung, Formatversion, Laufparameter, Unterbrechungs- und Ladezustand |
| `messages.json` | Zusammengeführte Nachrichten, Rohlabel, Identitätsquelle, Anhänge und Revisionen |
| `messages.csv` | UTF-8-BOM; Datum, Uhrzeit, Absender, Text, IDs und JSON-Anhangsliste |
| `media/<SHA256>.bin` | Lokal gesicherte Anhangsbytes, Dateiname aus Inhalts-Hash |
| `manifest.json` | Laufhistorie, Abdeckungsgrenzen, Zähler, Größe und SHA-256 der Archivdateien |

CSV-Felder, die als Tabellenformeln interpretiert werden könnten, erhalten ein
führendes Apostroph. JSON/JSONL erhalten den Originaltext. Das Manifest hasht
die verwalteten Ausgabedateien und alle erzeugten Hash-Mediendateien, auch
frühere Anhangsversionen; es hasht sich selbst
nicht. `.export.lock` ist eine technische Sperrdatei. Die Einzeldateien werden
atomar ersetzt; das Dateiset als Ganzes ist keine Transaktion. Nach einem
Abbruch mit `--resume` neu erzeugen. Hashes erkennen spätere Veränderungen,
belegen aber weder Urheberschaft noch die Vollständigkeit oder Authentizität
des Facebook-Verlaufs. Nach Abschluss eine unveränderte Archivkopie anlegen.

### Medien, nicht verifizierte Annahmen und Datenschutz

Die Struktur `messages_table`/`role=row` und das deutsche ARIA-Beispiel basieren
auf den bereitgestellten Angaben von November 2026. **Es wurde kein Live-Chat
zur Verifikation geöffnet.** Weitere ID-/Text-/Sender-/Zeit-Attribute,
englische Labels und E2EE-URL-Varianten sind vorsichtige, synthetisch getestete
Annahmen und müssen bei DOM-Änderungen angepasst werden. Eine einzelne
Nachrichtenliste unter der passenden URL wird als der angegebene Chat
interpretiert; eine zusätzliche unabhängige Chat-ID im DOM wird nicht geprüft.

Medienkandidaten sind `img`, `video`, `audio`, `source` sowie als Download/Anhang
ausgezeichnete Links innerhalb der Nachrichtenzeilen. Bekannte Avatar- und
Emoji-Labels werden ausgefiltert; das ist eine Heuristik. Gesichert werden
direkte HTTPS-Quellen unter `fbcdn.net`/`fbsbx.com` und gleichursprüngliche
`blob:`-URLs. Für CDN-GETs wird der
[Request-Kontext der Browser-Session](https://playwright.dev/python/docs/api/class-apirequestcontext)
genutzt. Redirects werden einzeln geprüft; fremde Hosts, Facebook-Seitenlinks
und externe Linkvorschauen werden nicht heruntergeladen. Quellen, Fehlstatus,
lokale Pfade und Hashes stehen am Anhang. Fehlgeschlagene Quellen werden pro
Lauf einmal und bei Wiederaufnahme erneut versucht. Vorhandene Dateien werden
vor Wiederverwendung anhand ihres Hashes geprüft.

Originaldateien, volle Bildauflösung, nur nach Klick verfügbare Downloads,
abgelaufene URLs, Playlists/Streamsegmente, Lazy-Loading, Sticker und
verschlüsselte Anhänge können fehlen oder nur als Vorschau vorliegen.
Blob-URLs lassen sich nach einem Neustart häufig nicht erneut abrufen.
`.bin` erhält die Bytes ohne Ausführung oder Inhaltsinterpretation; der
ursprüngliche Downloadname bleibt als Metadatum erhalten. Das Größenlimit
begrenzt die gespeicherten Bytes, **nicht** zuverlässig Netzwerkverbrauch oder
Spitzenspeicher: Playwright puffert Antworten bereits beim GET. Es gibt keine
Zusage für sehr große Verläufe; zusammengeführte Daten und Projektionen liegen
im Arbeitsspeicher, das Journal wächst mit Beobachtungsänderungen.

**Schon das Öffnen oder Scrollen eines Chats kann automatische
Lesebestätigungen, Präsenz- oder andere serverseitige Effekte auslösen.**
Diese werden nicht aktiv ausgelöst, aber auch nicht durch Eingriffe in das
Messenger-Protokoll blockiert. Wer garantiert keine Lesebestätigung auslösen
darf, sollte diesen DOM-Exporter nicht mit einer Live-Session öffnen.

Nur Chats archivieren, auf die das angemeldete Konto regulär Zugriff hat und
für deren Speicherung eine Berechtigung besteht. Es werden weder Captchas noch
PINs, Login-, E2EE- oder andere Zugangskontrollen umgangen. Das Archiv enthält
private Nachrichten, Teilnehmernamen und möglicherweise signierte, zeitweise
zugriffsfähige Medien-URLs. Geschützt lokal aufbewahren, Zugriff und
Aufbewahrungsdauer begrenzen, bei Bedarf Datenträgerverschlüsselung verwenden
und nicht ungeprüft teilen. Neue Ausgabeordner werden mit restriktiven
Verzeichnisrechten angelegt; vorhandene Ordner behalten ihre Rechte.
`messenger_exports/` und `state.json` sind in `.gitignore`; für andere
Ausgabeorte selbst auf den Ausschluss achten. Session-Cookies werden nicht ins
Archiv kopiert. Messenger-Debug-Ausgaben enthalten ausschließlich Zähler und
feste Statusmeldungen, keine Cookies, Zugangsdaten, Nachrichten, URLs,
Screenshots oder HTML-Dumps. Extern aktiviertes Playwright-Protokoll-Logging
oder fremde Diagnosewerkzeuge gehören nicht zu dieser Debug-Funktion.

### Tests

```bash
python3 -m unittest -v test_fbsave  # bestehender Kommentar-Exporter
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m unittest -v test_messenger
```

Die neuen Tests verwenden nur synthetische Daten, lokal beantwortete
Browseranfragen und keine echte Facebook-Session. Sie prüfen ARIA/Metadaten,
Hilfslabels, beide Scrollrichtungen einer virtualisierten Liste, verzögertes
Nachladen, entfernte Zeilen, Rundenlimit, Unterbrechung, Wiederaufnahme,
Deduplizierung, Medien- und Redirect-Regeln, Hashes sowie logfreie private
Inhalte. Ohne installiertes Playwright werden Browser-Fixtures übersprungen;
in CI wird Playwright mit Chromium installiert und vollständig ausgeführt.
