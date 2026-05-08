/**
 * Music-cue helpers.
 *
 * The promo's scene timing is derived from cues.json (output of
 * scripts/analyze_music.py). Functions here translate seconds ↔
 * frames, snap to downbeats, and find the next quiet window for
 * smooth cuts.
 */

import cuesData from "../cues.json";

export const FPS = 30;

export interface Cues {
  file: string;
  duration_s: number;
  tempo_bpm: number;
  beats_per_bar: number;
  beats_s: number[];
  downbeats_s: number[];
  low_energy_windows: { start: number; end: number; duration: number; rms_db: number }[];
  section_boundaries_s: number[];
}

export const cues: Cues = cuesData as Cues;

/** seconds → integer frame at our project FPS */
export const sToFrame = (s: number): number => Math.round(s * FPS);

/** Snap a target time to the nearest downbeat (within ±0.6s, else
 * return target unchanged so we don't yank too far). */
export const snapToDownbeat = (
  targetS: number,
  maxJumpS: number = 0.6,
): number => {
  if (cues.downbeats_s.length === 0) return targetS;
  let best = cues.downbeats_s[0];
  let bestDelta = Math.abs(targetS - best);
  for (const d of cues.downbeats_s) {
    const delta = Math.abs(targetS - d);
    if (delta < bestDelta) {
      best = d;
      bestDelta = delta;
    }
  }
  return bestDelta <= maxJumpS ? best : targetS;
};

/** Find the closest quiet window centred on target (or the nearest
 * one within `maxDistance` seconds). Returns null if none nearby. */
export const nearestQuietWindow = (
  targetS: number,
  maxDistanceS: number = 4.0,
) => {
  const candidates = cues.low_energy_windows
    .map((w) => ({
      ...w,
      distance: Math.min(
        Math.abs(targetS - w.start),
        Math.abs(targetS - w.end),
      ),
    }))
    .filter((w) => w.distance <= maxDistanceS)
    .sort((a, b) => a.distance - b.distance);
  return candidates[0] ?? null;
};
