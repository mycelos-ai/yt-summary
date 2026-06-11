# SVG logo — "Play wird Text"

**Date:** 2026-06-11
**Status:** Approved

## Goal

Replace the black line-art PNG logo (`logo-alt.png`) with a scalable SVG mark
that ties into the design system: ink `#0a0a0a` plus brand green `#00d4a4`.
One mark, used everywhere (header, favicon, apple-touch-icon).

## The mark

Variant "A · Play wird Text": a green play triangle on the left that hands off
into three shortening ink text lines on the right — video in, summary out.
Drawn on a 24×24 viewBox with rounded caps/joins so it stays legible at 16 px.

Chosen over two alternatives (document-with-play, ask-bubble-with-play)
because it tells the core story best and survives favicon size without an
outline that turns to mush.

## Assets (`app/static/icons/`)

| File | Purpose | Notes |
|---|---|---|
| `logo.svg` | Header brand icon | Fixed colors (header is always on white canvas) |
| `favicon.svg` | Browser tab icon | Same mark + `@media (prefers-color-scheme: dark)` flips ink lines to white for dark tabs; green stays |
| `favicon-16x16.png`, `favicon-32x32.png`, `favicon.ico` | Legacy fallbacks | Regenerated from the SVG via `rsvg-convert` / ImageMagick |
| `apple-touch-icon.png` | iOS home screen | 180×180, white background + padding (iOS dislikes transparency) |

The old `logo.png` / `logo-alt.png` stay in the repo, just unreferenced.

## Template changes (`app/templates/base.html`)

- `<head>`: add `<link rel="icon" type="image/svg+xml" href="favicon.svg">`
  before the PNG fallback links.
- Header: point `<img class="brand-logo">` at `icons/logo.svg` instead of
  `icons/logo-alt.png`. No CSS changes — existing 28×28 sizing applies.

## Verification

Start the app, screenshot the header, confirm the SVG favicon and logo are
served (200, `image/svg+xml`).
