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
import { brand } from "../lib/brand";

/**
 * 0 - 7s: opens with a two-beat hook posing the question, then the
 * logo lands as the answer.
 *
 *   0.0 - 2.0s   "Hungry for ideas."        white, fade in/out
 *   2.0 - 4.0s   "Tired of the watch time?" cyan, fade in/out
 *   4.0 - 7.0s   logo + "yt-summary" + tagline (Watch later, but smart.)
 *
 * The hook uses the same fast cross-fade pattern as the Problem
 * scene so the rhythm feels consistent across the cold open.
 */

const hookBeats = [
  { text: "Hungry for ideas.", color: "white" as const, glow: false },
  { text: "Tired of the watch time?", color: "cyan" as const, glow: true },
];

export const LogoReveal: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // ── Logo + tagline land at 4s (frame 120) ───────────────────
  const logoStart = fps * 4.0;
  const logoSpring = spring({
    frame: frame - logoStart,
    fps,
    config: { damping: 14, stiffness: 90 },
  });
  const logoOpacity = interpolate(
    frame,
    [logoStart, logoStart + fps * 0.4],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Tagline 0.8s after the logo lands
  const taglineStart = logoStart + fps * 0.8;
  const taglineOpacity = interpolate(
    frame,
    [taglineStart, taglineStart + fps * 0.6],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const taglineY = interpolate(
    frame,
    [taglineStart, taglineStart + fps * 0.6],
    [12, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Subtle pulsing glow on the logo (loose half-time of 123 BPM)
  const pulse = (Math.sin((frame / fps) * Math.PI * 2 * (123 / 60 / 2)) + 1) / 2;
  const glow = 30 + pulse * 25;

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(ellipse at center, ${brand.tronDeep} 0%, #000 70%)`,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {/* Hook beats — only render before the logo lands */}
      {hookBeats.map((beat, idx) => {
        const startFrame = idx * fps * 2.0;
        const peakStart = startFrame + fps * 0.3;
        const peakEnd = startFrame + fps * 1.6;
        const endFrame = startFrame + fps * 2.0;
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
        const isCyan = beat.color === "cyan";
        return (
          <AbsoluteFill
            key={beat.text}
            style={{
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <div
              style={{
                opacity,
                transform: `scale(${scale})`,
                color: isCyan ? brand.tronBlue : "white",
                fontSize: 96,
                fontWeight: 600,
                letterSpacing: "-2px",
                textAlign: "center",
                textShadow: beat.glow
                  ? `0 0 32px ${brand.tronGlow}`
                  : "none",
              }}
            >
              {beat.text}
            </div>
          </AbsoluteFill>
        );
      })}

      {/* Logo answer — only renders once we hit 4s */}
      {frame >= logoStart && (
        <>
          <div
            style={{
              transform: `scale(${0.8 + logoSpring * 0.2})`,
              opacity: logoOpacity,
              filter: `drop-shadow(0 0 ${glow}px ${brand.tronGlow})`,
            }}
          >
            <Img
              src={staticFile("logo.png")}
              style={{
                width: 280,
                height: 280,
                objectFit: "contain",
              }}
            />
          </div>
          <div
            style={{
              marginTop: 48,
              opacity: taglineOpacity,
              transform: `translateY(${taglineY}px)`,
            }}
          >
            <h1
              style={{
                color: "white",
                fontSize: 72,
                fontWeight: 600,
                letterSpacing: "-1.5px",
                margin: 0,
                textAlign: "center",
              }}
            >
              yt-summary
            </h1>
            <p
              style={{
                color: brand.tronBlue,
                fontSize: 28,
                fontWeight: 400,
                letterSpacing: "0.5px",
                textAlign: "center",
                marginTop: 16,
                textShadow: `0 0 16px ${brand.tronGlow}`,
              }}
            >
              Watch later, but smart.
            </p>
          </div>
        </>
      )}
    </AbsoluteFill>
  );
};
