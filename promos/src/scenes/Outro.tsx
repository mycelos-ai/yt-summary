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
import { brand, INSTALL_CMD, REPO_URL } from "../lib/brand";

/**
 * 57s - 65s (8s total): three phases that play out as the music's
 * second breakdown settles.
 *
 *   0.0 - 2.5s   logo lands + "yt-summary" wordmark
 *   2.5 - 5.0s   repo URL + "Self-hosted · MIT licensed"
 *   5.0 - 8.0s   install command panel + "Installs in 30 seconds."
 *
 * The whole scene fades out across the last 1.5s alongside the
 * audio fade in Composition.tsx.
 */
export const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  // Phase 1 — logo
  const logoEntry = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 60 },
  });
  const logoOpacity = interpolate(frame, [0, fps * 0.6], [0, 1], {
    extrapolateRight: "clamp",
  });

  // Phase 2 — URL row, lands at 2.5s
  const urlOpacity = interpolate(
    frame,
    [fps * 2.5, fps * 3.2],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Phase 3 — install panel, lands at 5.0s
  const installOpacity = interpolate(
    frame,
    [fps * 5.0, fps * 5.7],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const installY = interpolate(
    frame,
    [fps * 5.0, fps * 5.7],
    [16, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Final 1.5s fade-out (synchronised with the audio fade)
  const fadeOut = interpolate(
    frame,
    [durationInFrames - fps * 1.5, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Subtle logo-glow pulse
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
        padding: "0 80px",
      }}
    >
      {/* Logo */}
      <div
        style={{
          opacity: logoOpacity,
          transform: `scale(${0.85 + logoEntry * 0.15})`,
          filter: `drop-shadow(0 0 ${glow}px ${brand.tronGlow})`,
        }}
      >
        <Img
          src={staticFile("logo.png")}
          style={{ width: 180, height: 180, objectFit: "contain" }}
        />
      </div>

      <h1
        style={{
          opacity: logoOpacity,
          color: "white",
          fontSize: 56,
          fontWeight: 600,
          letterSpacing: "-1.5px",
          marginTop: 24,
        }}
      >
        yt-summary
      </h1>

      {/* Phase 2 — URL row */}
      <div
        style={{
          opacity: urlOpacity,
          marginTop: 20,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 8,
        }}
      >
        <code
          style={{
            color: brand.tronBlue,
            fontFamily: '"Geist Mono", monospace',
            fontSize: 24,
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
            fontSize: 13,
            letterSpacing: "1.5px",
            textTransform: "uppercase",
          }}
        >
          Self-hosted · MIT licensed
        </span>
      </div>

      {/* Phase 3 — install command panel */}
      <div
        style={{
          opacity: installOpacity,
          transform: `translateY(${installY}px)`,
          marginTop: 36,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span
          style={{
            color: "rgba(255,255,255,0.7)",
            fontSize: 18,
            fontWeight: 500,
            letterSpacing: "0.3px",
          }}
        >
          Installs in 30 seconds.
        </span>
        <div
          style={{
            background: "rgba(0, 20, 40, 0.85)",
            border: `1px solid ${brand.tronBlue}`,
            borderRadius: 10,
            padding: "16px 24px",
            boxShadow: `0 0 32px ${brand.tronGlow}`,
            maxWidth: "90%",
          }}
        >
          <code
            style={{
              color: brand.tronBlue,
              fontFamily: '"Geist Mono", monospace',
              fontSize: 18,
              letterSpacing: "0.3px",
              whiteSpace: "nowrap",
              display: "block",
              overflow: "hidden",
            }}
          >
            <span style={{ color: "rgba(255,255,255,0.45)" }}>$ </span>
            {INSTALL_CMD}
          </code>
        </div>
      </div>
    </AbsoluteFill>
  );
};
