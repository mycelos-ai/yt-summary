import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { brand } from "../lib/brand";

/**
 * 10.5s - 22s: three lines of text describing the problem,
 * delivered in beat-synced flashes. Each line has its own ~3.5s slot.
 */
const lines = [
  "Endless videos.",
  "Too long. Too many.",
  "You'll never watch them all.",
];

export const Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill
      style={{
        background: "#000",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
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
        return (
          <div
            key={text}
            style={{
              position: "absolute",
              opacity,
              transform: `scale(${scale})`,
              color: idx === lines.length - 1 ? brand.tronBlue : "white",
              fontSize: 84,
              fontWeight: 600,
              letterSpacing: "-1.5px",
              textShadow:
                idx === lines.length - 1
                  ? `0 0 24px ${brand.tronGlow}`
                  : "none",
            }}
          >
            {text}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
