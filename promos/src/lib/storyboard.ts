/**
 * Storyboard for the yt-summary promo.
 *
 * Scene timings are anchored to musical cues from cues.json:
 * - Section boundaries at 5s, 22s, 39s, 89s define the major beats
 *   of the 90-second arc.
 * - We snap each cut to the nearest downbeat for tight musical feel.
 * - Quiet windows around 6-10s, 23-26s, 84-88s, 105-112s give
 *   moments where text reveals don't fight the bass.
 *
 * The 90-second cut ends just as the second drop begins — natural
 * outro on the breakdown around 105s.
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
  { id: "logo-reveal",  startS: 0.0,  endS: 10.5  },  // intro build
  { id: "problem",      startS: 10.5, endS: 22.0  },  // what's wrong
  { id: "save-to-queue", startS: 22.0, endS: 40.0 },  // the save action
  { id: "magic-happens", startS: 40.0, endS: 64.0 },  // pi5 + pipeline
  { id: "skim-decide",   startS: 64.0, endS: 89.0 },  // summary + verdict
  { id: "self-hosted",   startS: 89.0, endS: 105.0 }, // settings + stack
  { id: "outro",         startS: 105.0, endS: 114.0 }, // logo + URL
];

export const TOTAL_DURATION_S = 114;
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
