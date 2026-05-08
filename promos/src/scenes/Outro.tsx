import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { brand, REPO_URL } from "../lib/brand";

/**
 * 105s - 114s: closing. Logo centred with a final pulse, repo URL
 * below in monospace, MIT badge corner. The track is breaking down
 * around 105-112s (longest quiet window) so we lean into a calmer
 * ending.
 */
export const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const logoEntry = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 60 },
  });
  const logoOpacity = interpolate(frame, [0, fps * 0.8], [0, 1], {
    extrapolateRight: "clamp",
  });

  const urlOpacity = interpolate(
    frame,
    [fps * 1.2, fps * 2.0],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Final fade-out across the last 1.5s of the scene
  const fadeOut = interpolate(
    frame,
    [durationInFrames - fps * 1.5, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  const pulse = (Math.sin((frame / fps) * Math.PI * 2 * 0.5) + 1) / 2;
  const glow = 30 + pulse * 25;

  return (
    <AbsoluteFill
      style={{
        background:
          "radial-gradient(ellipse at center, #001a30 0%, #000 70%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        opacity: fadeOut,
      }}
    >
      <div
        style={{
          opacity: logoOpacity,
          transform: `scale(${0.85 + logoEntry * 0.15})`,
          filter: `drop-shadow(0 0 ${glow}px ${brand.tronGlow})`,
        }}
      >
        <Img
          src={staticFile("logo.png")}
          style={{ width: 220, height: 220, objectFit: "contain" }}
        />
      </div>

      <h1
        style={{
          opacity: logoOpacity,
          color: "white",
          fontSize: 64,
          fontWeight: 600,
          letterSpacing: "-1.5px",
          marginTop: 32,
        }}
      >
        yt-summary
      </h1>

      <div
        style={{
          opacity: urlOpacity,
          marginTop: 24,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 12,
        }}
      >
        <code
          style={{
            color: brand.tronBlue,
            fontFamily: '"Geist Mono", monospace',
            fontSize: 28,
            letterSpacing: "0.5px",
            textShadow: `0 0 16px ${brand.tronGlow}`,
          }}
        >
          {REPO_URL}
        </code>
        <span
          style={{
            color: "rgba(255,255,255,0.55)",
            fontFamily: '"Geist Mono", monospace',
            fontSize: 14,
            letterSpacing: "1.5px",
            textTransform: "uppercase",
          }}
        >
          Self-hosted · MIT licensed
        </span>
      </div>
    </AbsoluteFill>
  );
};
