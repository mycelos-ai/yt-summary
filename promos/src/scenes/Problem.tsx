import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { brand } from "../lib/brand";

/**
 * 10.5s - 22s: YouTube thumbnail-overload background with a slow
 * Ken-Burns zoom, three text reveals layered on top with a dark
 * gradient so the message stays readable. The background represents
 * the chaos the user is escaping from.
 */
const lines = [
  "Endless videos.",
  "Too long. Too many.",
  "You'll never watch them all.",
];

export const Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  // Slow Ken-Burns: scale from 1.0 to 1.12 over the full scene,
  // pan slightly left-to-right so the chaos feels alive.
  const bgScale = interpolate(frame, [0, durationInFrames], [1.0, 1.12]);
  const bgX = interpolate(frame, [0, durationInFrames], [-30, 30]);

  return (
    <AbsoluteFill style={{ background: "#000", overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          transform: `scale(${bgScale}) translateX(${bgX}px)`,
          opacity: 0.6,
        }}
      >
        <Img
          src={staticFile("scenes/youtube-overload.png")}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      </AbsoluteFill>

      {/* Dark vignette so text stays legible over the chaotic bg */}
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse at center, transparent 0%, rgba(0,0,0,0.85) 80%)",
        }}
      />

      {lines.map((text, idx) => {
        // Each line gets ~3.5s; cross-fade between them.
        const startFrame = idx * fps * 3.5;
        const peakStart = startFrame + fps * 0.4;
        const peakEnd = startFrame + fps * 3.0;
        const endFrame = startFrame + fps * 3.5;
        const opacity = interpolate(
          frame,
          [startFrame, peakStart, peakEnd, endFrame],
          [0, 1, 1, 0],
          { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
        );
        const scale = interpolate(
          frame,
          [startFrame, peakStart],
          [0.95, 1],
          { extrapolateRight: "clamp", extrapolateLeft: "clamp" }
        );
        if (opacity <= 0) return null;
        const isLast = idx === lines.length - 1;
        return (
          <AbsoluteFill
            key={text}
            style={{
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <div
              style={{
                opacity,
                transform: `scale(${scale})`,
                color: isLast ? brand.tronBlue : "white",
                fontSize: 96,
                fontWeight: 600,
                letterSpacing: "-2px",
                textAlign: "center",
                textShadow: isLast
                  ? `0 0 32px ${brand.tronGlow}, 0 0 8px rgba(0,0,0,0.9)`
                  : "0 4px 24px rgba(0,0,0,0.9)",
              }}
            >
              {text}
            </div>
          </AbsoluteFill>
        );
      })}
    </AbsoluteFill>
  );
};
