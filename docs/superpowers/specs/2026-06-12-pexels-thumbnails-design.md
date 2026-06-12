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
  der Highlights-Prompt ist systemkontrolliert.
- **Geltungsbereich:** `kind='email'` immer; `kind='web'` nur, wenn nach
  dem og:image-Versuch kein Thumbnail existiert. `kind='youtube'` nie.
- Bestehende Items bleiben unverändert (kein Backfill in diesem Wurf;
  optionaler Sweep als Folgeprojekt).

## 1. Settings

- Neuer Settings-Key `pexels_api_key` (Tabelle `settings`, wie die
  IMAP-Zugangsdaten; Klartext ist dort etabliert).
- Settings-Seite: Eingabefeld „Pexels API Key“ mit kurzem Hinweistext
  (Link auf pexels.com/api). Leer = Feature inaktiv, keinerlei API-Calls.

## 2. LLM: `image_query`

Der Highlights-Extraktions-Prompt (dort, wo `highlights_json` entsteht)
bekommt ein zusätzliches Output-Feld:

```
"image_query": "<2-4 englische Suchwörter für ein passendes Stockfoto>"
```

Parsing tolerant: fehlt das Feld oder ist es leer/kein String → `None`,
niemals ein Fehler. Der Wert wird nicht persistiert, sondern direkt im
selben Pipeline-Schritt verbraucht.

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

1. Gilt nur für `kind in ('email', 'web')` und nur, wenn
   `thumbnail_path` noch `NULL`/leer ist.
2. `pexels_api_key` aus den Settings lesen (User-scoped wie üblich);
   fehlt er oder fehlt `image_query` → Schritt überspringen.
3. `fetch_pexels_thumbnail(query=image_query, api_key=…,
   target=config.thumbnails_dir / f"{video_id}.jpg")`.
4. Bei `True`: `thumbnail_path` am Video setzen (bestehendes
   Upsert-/Update-Muster wie beim Web-Import).

## 5. Ränder

- Kein Key konfiguriert → exakt heutiges Verhalten (Icon-Platzhalter).
- Pexels-Rate-Limit (200/h) ist für Newsletter-Volumina irrelevant;
  429 wird wie jeder Fehler geschluckt, kein Retry.
- og:image schlägt fehl, Pexels liefert: Web-Artikel bekommt das
  Stock-Foto (gewollt).
- Attribution: Pexels verlangt keine Pflicht-Attribution in der UI
  (im Gegensatz zu Unsplash) — bewusst einer der Gründe für Pexels.

## 6. Tests

- `stock_images`: gemocktes HTTP — Treffer (Datei entsteht, `True`),
  kein Treffer (`False`), HTTP-Fehler/Timeout (`False`, kein Raise),
  defektes JSON (`False`).
- Highlights-Parsing: `image_query` vorhanden / fehlt / falscher Typ.
- Pipeline: E-Mail-Item ohne Thumbnail + Key gesetzt → `thumbnail_path`
  gesetzt; ohne Key → unverändert; YouTube-Item → nie aufgerufen;
  Web-Item mit og:image → nicht überschrieben.
- Settings-Roundtrip: Key speichern/anzeigen/leeren über die
  Settings-Route.
