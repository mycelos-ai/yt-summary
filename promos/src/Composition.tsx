import React from "react";
import { AbsoluteFill, Audio, Sequence, staticFile } from "remotion";
import { scenes } from "./lib/storyboard";
import { LogoReveal } from "./scenes/LogoReveal";
import { Problem } from "./scenes/Problem";
import { Placeholder } from "./scenes/Placeholder";

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
      <Audio src={staticFile("music/promo_2.mp3")} />

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
