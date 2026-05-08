/**
 * Storyboard for the yt-summary promo.
 *
 * Tightened from 114s to ~60s — the original cut was too generous on
 * dwell time, especially the logo-reveal opener which had 10s of
 * negligible information. New durations roughly halve the leisurely
 * parts (logo, outro) and trim the dense scenes more conservatively.
 *
 * Scene timings still snap to the nearest downbeat from the music
 * cues. The Suno track is left at full length for now — visual cuts
 * end ~60s, music continues underneath. We can ffmpeg-trim later
 * once Stefan signs off on the new pacing.
 *
 * Per-scene budgets (start → end, seconds):
 *   logo-reveal     0  → 5.0   short pulse, tagline, in and out
 *   problem         5  → 11.0  three line reveals, ~2s each
 *   save-to-queue  11 → 21.0  headline + screenshot dwell
 *   magic-happens  21 → 35.0  diagram + 4 step pulses (the drop)
 *   skim-decide    35 → 47.0  three card sections, hero asset
 *   self-hosted    47 → 55.0  quick-setup + provider chips
 *   outro          55 → 60.0  logo + URL, fade
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
  { id: "logo-reveal",   startS: 0.0,  endS: 5.0  },
  { id: "problem",       startS: 5.0,  endS: 11.0 },
  { id: "save-to-queue", startS: 11.0, endS: 21.0 },
  { id: "magic-happens", startS: 21.0, endS: 35.0 },
  { id: "skim-decide",   startS: 35.0, endS: 47.0 },
  { id: "self-hosted",   startS: 47.0, endS: 55.0 },
  { id: "outro",         startS: 55.0, endS: 60.0 },
];

export const TOTAL_DURATION_S = 60;
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
