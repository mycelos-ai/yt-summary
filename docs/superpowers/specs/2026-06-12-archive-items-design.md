# Items archivieren statt löschen — Design

**Datum:** 2026-06-12
**Status:** Entwurf genehmigt

## Ziel

Nutzer können ein Library-Item (Video, Web-Artikel, E-Mail-Summary) von der
Detailseite aus archivieren. Archivierte Items verschwinden aus allen
aktiven Ansichten (Library, Suche, Ask, Digest), bleiben aber vollständig
erhalten und sind über eine eigene Archiv-Seite wiederherstellbar.

## Entscheidungen (aus dem Brainstorming)

- **Archivieren statt Löschen** (Soft-Delete): kein Datenverlust, kein
  Kaskaden-Problem (vier Tabellen referenzieren `videos(id)` ohne
  `ON DELETE CASCADE`), und der Playlist-Sync-Re-Import löst sich von
  selbst — das Item existiert weiterhin, die Dedupe greift.
- Button auf der **Detailseite**; **kein Bestätigungsdialog** (umkehrbar).
- **Eigene Archiv-Seite** (`/archive`); archivierte Items sind überall
  sonst ausgeblendet — auch in Suche und Ask.

## 1. Datenmodell

Neue nullable Spalte auf `videos`:

- `archived_at TEXT` — Zeitstempel (`datetime('now')`-Format), `NULL` = aktiv.

Migration im bestehenden Stil (`_ensure_column`, gated auf
`_table_exists(conn, "videos")`). Kein Backfill nötig.

Das `Video`-Dataclass erhält `archived_at: datetime | None` (Position
spiegelt die DDL-Reihenfolge).

## 2. Repo-Schicht

- `videos_repo.set_archived(db, video_id, *, user_id, archived: bool) -> bool`
  — setzt/cleart `archived_at`; gibt `False` zurück, wenn das Video nicht
  existiert oder dem Profil nicht gehört (Route antwortet dann 404).
- `videos_repo.list_archived(db, *, user_id, limit, offset)` — für die
  Archiv-Seite, sortiert nach `archived_at DESC`.
- Alle **aktiven** Listen-/Such-Queries erhalten den Filter
  `archived_at IS NULL`:
  - Home-Library-Liste + „Load more“ (`list_for_user` o. ä.)
  - Volltextsuche (FTS) und Vektorsuche (Kandidaten-Filter nach dem
    vec0-Lookup, da die virtuelle Tabelle die Spalte nicht kennt)
  - Ask/Synthese-Quellenauswahl
  - Digest: `_gather_pool` und `list_candidates`
  - Related-Strips und Tag-gefilterte Ansichten
- Die Detailseite (`/v/{id}`) lädt weiterhin auch archivierte Videos —
  sonst wäre Wiederherstellen unmöglich. Alte Digest-/Synthese-Links
  funktionieren dadurch weiter.

## 3. Routen

- `POST /v/{video_id}/archive` — archiviert, Redirect 303 auf `/`.
- `POST /v/{video_id}/unarchive` — stellt wieder her, Redirect 303 auf
  `/v/{video_id}`.
- Beide: 404 bei fremdem Profil oder unbekannter ID.
- `GET /archive` — Archiv-Seite, Video-Karten-Grid wie die Library,
  leerer Zustand („Nichts archiviert“).

## 4. UI

- **Detailseite, aktives Item:** Button „Archive“ bei den bestehenden
  Aktionen (Export-Menü-Zeile). Form-POST, kein JS nötig.
- **Detailseite, archiviertes Item:** dezenter Hinweis-Banner
  („Archiviert am …“) + Button „Restore“.
- **Library:** dezenter Link „Archive →“ unterhalb der Library-Sektion
  (analog zu „All digests →“), nur sichtbar, wenn mindestens ein Item
  archiviert ist (Count-Query) — oder immer sichtbar, falls das einfacher
  bleibt; Implementierung darf das Einfachere wählen.
- Video-Karten im Archiv: bestehende `video_card.html` wiederverwenden.

## 5. Ränder

- Archivierte Videos behalten Tags, Chat-Verlauf, Feedback, TTS-Audio —
  alles bleibt unangetastet und ist nach dem Wiederherstellen wieder da.
- Digest-`top_items_json` mit archivierten Videos: Links bleiben gültig
  (Detailseite erreichbar). Keine Sonderbehandlung.
- Playlist-Sync: Item bleibt in `playlist_videos`; die Playlist-Detailseite
  zeigt archivierte Items NICHT mehr (gleicher Filter), der Sync legt wegen
  Dedupe nichts Neues an.
- Embeddings/FTS: Zeilen bleiben indiziert; Ausschluss erfolgt über den
  SQL-Filter, nicht über Index-Manipulation.

## 6. Tests

- Migration: Legacy-`videos` ohne Spalte → `archived_at` vorhanden.
- Repo: `set_archived` Roundtrip, Fremd-Profil → `False`; aktive Listen
  ohne archivierte; `list_archived` nur archivierte, User-scoped.
- Suche/Ask/Digest: archiviertes Video taucht in FTS-Suche, Vektorsuche,
  Digest-Kandidaten und `_gather_pool` nicht mehr auf.
- Routen: archive/unarchive Statusflüsse, 404-Fälle, Redirects;
  `/archive` rendert Karten + leeren Zustand; Detailseite zeigt
  Restore-Button bei archiviertem Item.
