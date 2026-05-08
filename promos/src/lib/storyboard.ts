/**
 * Storyboard for the yt-summary promo.
 *
 * Tightened from 114s to ~62s — the original cut was too generous on
 * dwell time, especially the logo-reveal opener which had 10s of
 * negligible information. New durations roughly halve the leisurely
 * parts and trim the dense scenes more conservatively.
 *
 * Logo-reveal takes 7s (not 5s) so we can fit a three-beat hook
 * BEFORE the logo lands:
 *   "Hungry for ideas."
 *   "Tired of the watch time?"
 *   → logo + tagline answer
 *
 * Scene timings still snap to the nearest downbeat from the music
 * cues. The Suno track is left at full length for now — visual cuts
 * end ~62s, music continues underneath.
 *
 * Per-scene budgets (start → end, seconds):
 *   logo-reveal     0  →  7.0   hook → answer + tagline
 *   problem         7  → 13.0   three line reveals, ~2s each
 *   save-to-queue  13 → 23.0   headline + screenshot dwell
 *   magic-happens  23 → 37.0   diagram + 4 step pulses (the drop)
 *   skim-decide    37 → 49.0   three card sections, hero asset
 *   self-hosted    49 → 57.0   quick-setup + provider chips
 *   outro          57 → 65.0   logo + URL, then install command
 */

import { sToFrame, snapToDownbeat } from "./cues";

export interface Scene {
  id: string;
  startS: number;
  endS: number;
  startFrame: number;
  endFrame: number;
  durationFrames: number;
}

const raw = [
  { id: "logo-reveal",   startS: 0.0,  endS: 7.0  },
  { id: "problem",       startS: 7.0,  endS: 13.0 },
  { id: "save-to-queue", startS: 13.0, endS: 23.0 },
  { id: "magic-happens", startS: 23.0, endS: 37.0 },
  { id: "skim-decide",   startS: 37.0, endS: 49.0 },
  { id: "self-hosted",   startS: 49.0, endS: 57.0 },
  { id: "outro",         startS: 57.0, endS: 65.0 },
];

export const TOTAL_DURATION_S = 65;
export const TOTAL_FRAMES = sToFrame(TOTAL_DURATION_S);

export const scenes: Scene[] = raw.map((r) => {
  const startS = snapToDownbeat(r.startS);
  const endS = snapToDownbeat(r.endS);
  const startFrame = sToFrame(startS);
  const endFrame = sToFrame(endS);
  return {
    id: r.id,
    startS,
    endS,
    startFrame,
    endFrame,
    durationFrames: endFrame - startFrame,
  };
});

export const sceneById = (id: string): Scene => {
  const s = scenes.find((x) => x.id === id);
  if (!s) throw new Error(`Unknown scene: ${id}`);
  return s;
};
