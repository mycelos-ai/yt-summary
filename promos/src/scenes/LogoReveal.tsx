import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig, Img, staticFile } from "remotion";
import { brand } from "../lib/brand";

/**
 * 0 - 10.5s: black canvas, logo fades in with a Tron-style glow
 * pulse, tagline appears below as the synth build resolves.
 */
export const LogoReveal: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Logo scale-in spring, lands by ~1.5s
  const logoScale = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 90 },
  });

  // Logo opacity ramps in over 0.5s
  const logoOpacity = interpolate(frame, [0, 15], [0, 1], {
    extrapolateRight: "clamp",
  });

  // Tagline waits 2.5s then fades up
  const taglineOpacity = interpolate(
    frame,
    [fps * 2.5, fps * 3.5],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const taglineY = interpolate(
    frame,
    [fps * 2.5, fps * 3.5],
    [12, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Subtle pulsing glow synced loosely to a 123 BPM half-time pulse
  // (every other beat ≈ every 0.97s)
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
      <div
        style={{
          transform: `scale(${0.8 + logoScale * 0.2})`,
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
    </AbsoluteFill>
  );
};
