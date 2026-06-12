# Digest mit Video-Auswahl — Design

**Datum:** 2026-06-12
**Status:** Entwurf genehmigt

## Ziel

Beim manuellen Erzeugen eines Digests („Daily Summary") soll der Nutzer auswählen
können, welche Videos berücksichtigt werden. Das Kandidatenfenster reicht vom
Ende des letzten Digests bis jetzt, gekappt auf maximal 96 Stunden. Der
automatische Cron-Digest nutzt dieselbe Fensterlogik, nimmt aber alle Kandidaten.

## Entscheidungen (aus dem Brainstorming)

- Auswahl auf Ebene **einzelner Videos** (keine Quellen-Gruppierung).
- **Cron-Digest bleibt** und nimmt automatisch alle Kandidaten im Fenster.
- Cutoff **fest 96 Stunden** (4 Tage), kein neues Setting.
- Kandidaten sind in der Auswahlliste **alle vorausgewählt**; zusätzlich
  „Alle auswählen / Alle abwählen".
- Eigene Auswahlseite **`GET /digest/new`** (Variante 1); das bisherige freie
  `period_hours`-Eingabefeld entfällt.
- Kein neues „last imported"-Feld: das `period_end` des letzten Digests
  übernimmt die Rolle von „wann wurde zuletzt zusammengefasst".

## 1. Fensterlogik (gemeinsam für manuell & Cron)

Neue Helper-Funktion (in `app/services/digest.py`):

```
period_end   = jetzt (UTC)
period_start = max(period_end des letzten Digests des Users, jetzt − 96h)
```

Gibt es noch keinen Digest, gilt `jetzt − 96h`. Kandidaten sind wie bisher
Videos des Users mit nicht-leerem `highlights_json` und `created_at` im
Fenster. Die bestehende Normalisierung über `datetime(created_at)` (Space-
vs. T-Separator, siehe `services/digest.py` `_gather_pool`) bleibt erhalten.

Beim Bestimmen des „letzten Digests" zählen Digests mit Status `ready`,
`pending` und `rendering`; `failed` wird ignoriert (ein fehlgeschlagener
Digest hat nichts zusammengefasst).

## 2. Datenmodell

Neue nullable Spalte auf `digests`:

- `selected_video_ids_json TEXT` — JSON-Liste von Video-IDs.
- `NULL` = automatischer Digest (Cron bzw. Alt-Daten): Pool wie bisher aus dem
  Fenster.
- Gesetzt = manuelle Auswahl: Pool wird auf diese IDs eingeschränkt.

Migration im bestehenden Stil von `app/db.py` (idempotentes `ALTER TABLE` /
ensure-column-Muster wie bei früheren Spaltenergänzungen).

## 3. Routen & Flow

### `GET /digest/new` (neu)

Rendert die Auswahlseite:

- Hinweiszeile, welcher Zeitraum abgedeckt wird („seit letztem Digest am …“
  bzw. „letzte 4 Tage“).
- Pro Kandidat: Checkbox (vorausgewählt), Titel, Kind/Quelle, Datum.
- Oben „Alle auswählen / Alle abwählen" (kleines Alpine-Toggle, rein
  clientseitig).
- Keine Kandidaten → Hinweistext statt Liste, Generieren-Button deaktiviert.
- Optionale Fußnote: „n weitere Videos im Zeitraum haben noch keine
  Highlights" (erscheinen nicht in der Liste, da sie nichts beitragen können).

### `POST /digest/generate` (geändert)

- Nimmt `video_ids[]` (Formular-Checkboxen) entgegen.
- Berechnet das Fenster serverseitig neu und validiert die IDs gegen die
  aktuellen Kandidaten; fremde oder nicht (mehr) im Fenster liegende IDs
  werden verworfen.
- Leere (effektive) Auswahl → Redirect zurück nach `/digest/new` mit
  Fehlermeldung.
- Legt den Digest mit `period_start`/`period_end` und
  `selected_video_ids_json` an und startet wie bisher den Background-Job
  (`run_for_existing_digest`).
- Das Parameter-Handling für `period_hours` entfällt im manuellen Flow.

### Einstiegspunkte

- „+ New digest“-Button auf `/` (home.html) und „Generate now“ auf
  `/digest` (list.html) werden zu Links auf `/digest/new`; die bisherigen
  POST-Formulare mit `period_hours` entfallen.

### `_gather_pool()` (geändert)

Optionaler Parameter `video_ids`: wenn gesetzt, wird der Pool zusätzlich auf
diese IDs eingeschränkt (Highlights- und User-Bedingung bleiben). Ohne
Parameter Verhalten wie heute.

## 4. Cron (`DigestScheduler`)

Der Scheduler (`app/scheduler.py`) nutzt statt des fixen 24h-Fensters dieselbe
Fensterfunktion (seit letztem Digest, max. 96h) und nimmt alle Kandidaten
(`selected_video_ids_json = NULL`). Der Schutz „nur ein Digest pro
Profil pro Kalendertag" über `exists_in_range()` bleibt unverändert.

## 5. Fehlerfälle & Ränder

- **Race zwischen Anzeigen und Submit:** Neue Videos, die nach dem Laden von
  `/digest/new` eintreffen, gehören zum nächsten Digest — `period_end` wird
  beim Submit gesetzt, die Validierung akzeptiert nur IDs, die im neu
  berechneten Fenster liegen. Abgewählte oder herausgefallene IDs werden
  still verworfen.
- **Manuell + Cron am selben Tag:** Ein manueller Digest setzt
  `period_end = jetzt`; der nächste Cron-Lauf schließt daran an. Die
  Ein-Digest-pro-Tag-Prüfung des Schedulers verhindert wie bisher Dubletten.
- **Alt-Digests ohne neue Spalte:** `NULL` wird als „automatisch" gelesen;
  kein Backfill nötig.

## 6. Tests

- Fensterfunktion: kein Vorgänger / Vorgänger jünger als 96h / Vorgänger
  älter als 96h / `failed`-Digests werden ignoriert.
- `_gather_pool` mit ID-Einschränkung: nur gewählte IDs, Highlights-Gate
  bleibt wirksam, fremde User-IDs ausgeschlossen.
- `GET /digest/new`: Kandidatenliste, leerer Zustand, Fußnote für Videos ohne
  Highlights.
- `POST /digest/generate`: Auswahl wird persistiert, leere Auswahl abgelehnt,
  fremde/abgelaufene IDs gefiltert, Background-Job nutzt die Auswahl.
- Scheduler: Fenster „seit letztem Digest, max. 96h" statt 24h.
