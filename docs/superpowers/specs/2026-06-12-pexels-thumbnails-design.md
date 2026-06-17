# Pexels-Thumbnails für E-Mail- und Web-Items — Design

**Datum:** 2026-06-12
**Status:** Entwurf genehmigt

## Ziel

E-Mail-Summaries (und Web-Artikel ohne og:image) bekommen ein passendes
Stock-Foto von Pexels als Thumbnail, damit die Library nicht aus lauter
identischen Platzhalter-Icons besteht.

## Entscheidungen (aus dem Brainstorming)

- **Pexels** (statt Unsplash): kostenloser API-Key, 200 Anfragen/h,
  simple Such-API.
- **Suchbegriff vom LLM**: der bestehende Highlights-Extraktions-Call
  liefert zusätzlich `image_query` (2–4 englische, bildtaugliche
  Suchwörter). Bewusst NICHT der per Profil anpassbare Summary-Prompt —
  der Highlights-Prompt ist systemkontrolliert. Der Wert wird
  **persistiert** (`videos.image_query`), damit der Backfill ihn ohne
  erneuten LLM-Call wiederverwenden kann.
- **Geltungsbereich:** `kind='email'` immer; `kind='web'` nur, wenn nach
  dem og:image-Versuch kein Thumbnail existiert. `kind='youtube'` nie.
- **Backfill** als wiederholbares CLI-Skript (`--force` für Re-Runs nach
  Algorithmus-Änderungen), damit man die Bildqualität iterativ testen kann.

## 1. Settings

- Neuer Settings-Key `pexels_api_key` (Tabelle `settings`, wie die
  IMAP-Zugangsdaten; Klartext ist dort etabliert).
- Settings-Seite: Eingabefeld „Pexels API Key“ mit kurzem Hinweistext
  (Link auf pexels.com/api). Leer = Feature inaktiv, keinerlei API-Calls.

## 2. LLM: `image_query`

Der `image_query`-Suchbegriff (2–4 englische Wörter) wird per **separatem,
billigem LLM-Call** aus der fertigen Summary abgeleitet — eine gemeinsame
Funktion `stock_images.generate_image_query(summary, model_row)`, die
sowohl die Live-Pipeline als auch der Backfill nutzen (DRY).

Begründung für den separaten Call statt eines Feldes im
Highlights-Envelope: `summarize_with_highlights` gibt `(summary,
highlights)` zurück und wird in ~25 bestehenden Tests gemockt; eine
Arity-Änderung oder ein Umstieg der Pipeline auf den rohen Envelope
würde all diese brechen. Der separate Call läuft ohnehin nur für
E-Mail/Web-Items mit gesetztem Pexels-Key — im Normalfall (kein Key)
kostet er nichts.

Der erzeugte Wert wird in der neuen Spalte `videos.image_query`
(nullable TEXT) **persistiert** — die Live-Pipeline schreibt ihn beim
Verarbeiten, der Backfill liest ihn wieder aus (und generiert+speichert
ihn, falls er fehlt). Parsing/Generierung tolerant: leere Summary, kein
Modell oder ein LLM-Fehler → `None`, niemals ein Fehler.

`HIGHLIGHTS_SCHEMA_HINT` bekommt zusätzlich ein optionales
`image_query`-Feld samt Parser `parse_image_query` (für den Fall, dass
das LLM es im Envelope mitliefert) — der Parser ist eigenständig und
bricht die bestehende `parse_summary_payload`-Signatur nicht.

Datenmodell-Ergänzung: `videos.image_query TEXT` per `_ensure_column`-
Migration; `Video`-Dataclass erhält `image_query: str | None`.

## 3. Service: `app/services/stock_images.py` (neu)

Eine kleine, isoliert testbare Einheit:

```
async def fetch_pexels_thumbnail(
    *, query: str, api_key: str, target: Path,
) -> bool
```

- GET `https://api.pexels.com/v1/search?query=…&per_page=1&orientation=landscape`
  mit Header `Authorization: <api_key>`.
- Treffer: `photos[0].src.large` herunterladen → `target` (JPEG).
  Wiederverwendung des bestehenden Download-Pfads
  (`download_thumbnail` bzw. dasselbe Muster).
- Rückgabe `True` nur, wenn die Datei am Ende existiert.
- JEDER Fehler (kein Treffer, 429, Timeout, kaputtes JSON) → `False`,
  geloggt auf debug/info-Level. Bilder sind kosmetisch — die Pipeline
  darf nie daran scheitern.

## 4. Pipeline-Integration

Im Worker-Schritt nach der Highlights-Extraktion (gleiche Stelle, an der
`highlights_json` gespeichert wird):

1. Thumbnail-Beschaffung gilt nur für `kind in ('email', 'web')` und nur,
   wenn `thumbnail_path` noch `NULL`/leer ist. YouTube nie.
2. `pexels_api_key` aus den Settings lesen (User-scoped). Fehlt er →
   Schritt komplett überspringen (kein LLM-Call, kein Netz).
3. `generate_image_query(summary, model_row)` → Suchbegriff; ist er leer
   → überspringen. Erfolg → in `videos.image_query` speichern.
4. `fetch_pexels_thumbnail(query=…, api_key=…,
   target=config.thumbnails_dir / f"{video_id}.jpg")`; bei `True`
   `thumbnail_path` am Video setzen (`videos_repo.set_thumbnail_path`).

Die gemeinsame Funktion `ensure_stock_thumbnail(db, video, *, config,
api_key, force)` kapselt Schritt 1+4 (Eignungsprüfung + Fetch + Set) und
wird von Pipeline und Backfill geteilt.

## 5. Backfill-CLI

Ein wiederholbares Skript, um bestehende Items nachträglich (und nach
Algorithmus-Änderungen erneut) mit Thumbnails zu versehen:

```
python -m app.scripts.backfill_thumbnails [--force] [--user-id N] [--limit N] [--dry-run]
```

Verhalten:

- Wählt Kandidaten: `kind in ('email','web')`, optional auf `--user-id`
  beschränkt; ohne `--force` nur Items mit leerem `thumbnail_path`, mit
  `--force` **alle** (so lassen sich Bilder nach einem geänderten
  `image_query`-Prompt oder anderer Pexels-Logik neu holen).
- Pro Item: `image_query` aus der Spalte nutzen; fehlt es, ein günstiger
  LLM-Mini-Call aus der gespeicherten `summary`, dessen Ergebnis in
  `videos.image_query` zurückgeschrieben wird (damit ein späterer Lauf
  ohne erneuten Call auskommt).
- Dann `fetch_pexels_thumbnail(...)`; bei Erfolg `thumbnail_path` setzen.
  Bei `--force` wird die Ziel-`.jpg` überschrieben.
- Teilt sich die Kern-Logik mit der Live-Pipeline über eine gemeinsame
  Funktion (z. B. `ensure_stock_thumbnail(db, video, *, force)` im
  `stock_images`-Service oder einem dünnen Pipeline-Helfer) — keine
  duplizierte Beschaffungslogik.
- Gibt am Ende eine **Zusammenfassung** aus (geprüft / Query generiert /
  Bild geholt / kein Treffer / Fehler), damit man die Trefferquote beim
  Iterieren sieht. `--dry-run` ermittelt nur die Queries und loggt sie,
  ohne Pexels aufzurufen oder zu schreiben — nützlich, um die LLM-Queries
  vor einem echten Lauf zu begutachten.
- Rate-Limit-schonend: kurze Pause zwischen den Pexels-Calls (z. B.
  ~0,3 s), damit auch große Bestände unter 200/h bleiben.

## 6. Ränder

- Kein Key konfiguriert → exakt heutiges Verhalten (Icon-Platzhalter).
- Pexels-Rate-Limit (200/h) ist für Newsletter-Volumina irrelevant;
  429 wird wie jeder Fehler geschluckt, kein Retry.
- og:image schlägt fehl, Pexels liefert: Web-Artikel bekommt das
  Stock-Foto (gewollt).
- Attribution: Pexels verlangt keine Pflicht-Attribution in der UI
  (im Gegensatz zu Unsplash) — bewusst einer der Gründe für Pexels.

## 7. Tests

- Migration: Legacy-`videos` ohne Spalte → `image_query` vorhanden.
- `stock_images`: gemocktes HTTP — Treffer (Datei entsteht, `True`),
  kein Treffer (`False`), HTTP-Fehler/Timeout (`False`, kein Raise),
  defektes JSON (`False`).
- Highlights-Parsing: `image_query` vorhanden / fehlt / falscher Typ;
  Persistenz in `videos.image_query`.
- Backfill-CLI (mit gemocktem Pexels + gemocktem LLM): überspringt Items
  mit Thumbnail ohne `--force`, verarbeitet sie mit `--force`; nutzt
  gespeichertes `image_query`, generiert+speichert es bei Fehlen;
  `--dry-run` schreibt nichts und ruft Pexels nicht auf; Zusammenfassungs-
  zähler stimmen; `--user-id`/`--limit` greifen.
- Pipeline: E-Mail-Item ohne Thumbnail + Key gesetzt → `thumbnail_path`
  gesetzt; ohne Key → unverändert; YouTube-Item → nie aufgerufen;
  Web-Item mit og:image → nicht überschrieben.
- Settings-Roundtrip: Key speichern/anzeigen/leeren über die
  Settings-Route.
