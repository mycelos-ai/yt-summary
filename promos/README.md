# yt-summary promos

Remotion sub-project that produces the promo video for the main app's
README and social posts. Independent of the runtime — building or
iterating on the promo doesn't affect the actual yt-summary service.

## Quick start

```bash
cd promos
npm install
npm run dev          # opens Remotion Studio on http://localhost:3000
```

Edit any scene under `src/scenes/` and the studio hot-reloads.

## Render to MP4

```bash
npm run build        # writes out/promo.mp4
```

## Architecture

- **`src/cues.json`** — output of `scripts/analyze_music.py`. Tempo,
  beats, downbeats, low-energy windows, section boundaries.
- **`src/lib/cues.ts`** — TS wrapper that loads the JSON and exposes
  `sToFrame()`, `snapToDownbeat()`, `nearestQuietWindow()`.
- **`src/lib/storyboard.ts`** — the scene list with start/end times,
  snapped to downbeats.
- **`src/scenes/`** — one file per scene, all rendered into a single
  composition.
- **`public/music/promo_2.mp3`** — the Suno-generated background
  track. Picked from two takes; the unused take is gitignored.

## Regenerate the music

```bash
# from repo root, not from promos/
python scripts/generate_music.py \
  --style "Tron Legacy soundtrack: dark synthwave, orchestral strings, glassy lead synths, deep 808 bass, pulsing arpeggios" \
  --title "yt-summary promo" \
  --output promos/public/music/promo.mp3 \
  --style-weight 0.85 --weirdness 0.15

# Two takes (_1.mp3 and _2.mp3) drop into promos/public/music/.
# Pick one, rename it to promo_2.mp3 (or update the Audio src in
# Composition.tsx), then re-analyze:

python scripts/analyze_music.py promos/public/music/promo_2.mp3 \
  -o promos/src/cues.json
```

## Tron-Legacy aesthetic

The promo leans into a Daft Punk / Tron Legacy vibe — dark synthwave,
deep blue background, cyan glow on brand elements. See `src/lib/brand.ts`
for the palette.
