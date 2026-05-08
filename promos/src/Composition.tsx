import React from "react";
import { AbsoluteFill, Audio, interpolate, Sequence, staticFile } from "remotion";
import { FPS } from "./lib/cues";
import { scenes, TOTAL_FRAMES } from "./lib/storyboard";
import { LogoReveal } from "./scenes/LogoReveal";
import { Problem } from "./scenes/Problem";
import { SaveToQueue } from "./scenes/SaveToQueue";
import { MagicHappens } from "./scenes/MagicHappens";
import { SkimDecide } from "./scenes/SkimDecide";
import { SelfHosted } from "./scenes/SelfHosted";
import { Outro } from "./scenes/Outro";
import { Placeholder } from "./scenes/Placeholder";

// Audio fade-out: volume ramps from 1.0 to 0 over the last 2.5
// seconds of the composition. Synced with the visual fade in the
// outro scene so video and audio die together.
const FADE_OUT_FRAMES = Math.round(2.5 * FPS);
const audioVolume = (frame: number): number =>
  interpolate(
    frame,
    [TOTAL_FRAMES - FADE_OUT_FRAMES, TOTAL_FRAMES],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

/**
 * Map scene id → the React component that renders it. Anything
 * missing falls through to the Placeholder so the composition
 * still plays end-to-end while we build.
 */
const SceneFor: React.FC<{ id: string }> = ({ id }) => {
  switch (id) {
    case "logo-reveal":
      return <LogoReveal />;
    case "problem":
      return <Problem />;
    case "save-to-queue":
      return <SaveToQueue />;
    case "magic-happens":
      return <MagicHappens />;
    case "skim-decide":
      return <SkimDecide />;
    case "self-hosted":
      return <SelfHosted />;
    case "outro":
      return <Outro />;
    default:
      return <Placeholder id={id} />;
  }
};

export const YtSummaryPromo: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: "#000" }}>
      {/* Background music sits underneath every scene. The MP3 file
          lives in promos/public/music/ and is reachable via
          staticFile() at render time. */}
      <Audio src={staticFile("music/promo_2.mp3")} volume={audioVolume} />

      {scenes.map((scene) => (
        <Sequence
          key={scene.id}
          from={scene.startFrame}
          durationInFrames={scene.durationFrames}
          name={scene.id}
        >
          <SceneFor id={scene.id} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
