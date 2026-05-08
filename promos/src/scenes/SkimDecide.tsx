import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { brand } from "../lib/brand";

/**
 * 64s - 89s: shows what the user actually gets. A mock summary
 * card — TL;DR, key points, mentioned resources — fades in as if
 * yt-summary just delivered it. Headline: "Skim. Decide. Move on."
 */
export const SkimDecide: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const headlineOpacity = interpolate(frame, [0, fps * 0.8], [0, 1], {
    extrapolateRight: "clamp",
  });

  const cardEntry = spring({
    frame: frame - fps * 1.5,
    fps,
    config: { damping: 16, stiffness: 65 },
  });
  const cardOpacity = interpolate(
    frame,
    [fps * 1.5, fps * 2.5],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const cardScale = 0.92 + cardEntry * 0.08;

  // Sequentially highlight each section of the summary.
  const sections = [
    { delay: 4.0, label: "TL;DR" },
    { delay: 6.5, label: "Key points" },
    { delay: 9.0, label: "Resources" },
  ];

  return (
    <AbsoluteFill
      style={{
        background:
          "radial-gradient(ellipse at center, #001a30 0%, #000 80%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div style={{ opacity: headlineOpacity, textAlign: "center", marginBottom: 40 }}>
        <div
          style={{
            color: brand.tronBlue,
            fontFamily: '"Geist Mono", monospace',
            fontSize: 16,
            letterSpacing: "4px",
            textTransform: "uppercase",
            opacity: 0.8,
            marginBottom: 8,
          }}
        >
          Three minutes later
        </div>
        <h2
          style={{
            color: "white",
            fontSize: 72,
            fontWeight: 600,
            letterSpacing: "-1.5px",
            margin: 0,
          }}
        >
          Skim. Decide. Move on.
        </h2>
      </div>

      {/* Mock summary card */}
      <div
        style={{
          width: 1100,
          maxWidth: "85%",
          background: "rgba(255,255,255,0.02)",
          backdropFilter: "blur(12px)",
          border: `1px solid ${brand.tronBlue}`,
          borderRadius: 16,
          padding: "40px 56px",
          opacity: cardOpacity,
          transform: `scale(${cardScale})`,
          boxShadow: `0 0 80px ${brand.tronGlow}, 0 24px 64px rgba(0,0,0,0.5)`,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            color: "rgba(255,255,255,0.55)",
            fontFamily: '"Geist Mono", monospace',
            fontSize: 14,
            marginBottom: 24,
          }}
        >
          <span>youtu.be/dQw4w9WgXcQ</span>
          <span style={{ color: brand.tronBlue }}>· 1h 47m → 2 min read</span>
        </div>

        {sections.map((s, idx) => {
          const start = fps * s.delay;
          const peak = start + fps * 0.7;
          const intensity = interpolate(
            frame,
            [start, peak],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
          );
          if (intensity <= 0) return null;
          return (
            <div
              key={s.label}
              style={{
                marginBottom: idx < sections.length - 1 ? 28 : 0,
                opacity: intensity,
                transform: `translateY(${(1 - intensity) * 8}px)`,
              }}
            >
              <div
                style={{
                  display: "inline-block",
                  fontFamily: '"Geist Mono", monospace',
                  fontSize: 12,
                  fontWeight: 600,
                  letterSpacing: "2px",
                  textTransform: "uppercase",
                  color: brand.tronBlue,
                  background: "rgba(74, 243, 255, 0.1)",
                  padding: "4px 10px",
                  borderRadius: 4,
                  marginBottom: 12,
                }}
              >
                {s.label}
              </div>
              <div
                style={{
                  color: "rgba(255,255,255,0.92)",
                  fontSize: 22,
                  lineHeight: 1.55,
                  fontWeight: 400,
                }}
              >
                {idx === 0 && (
                  <>
                    The speaker walks through three architectural decisions
                    that shaped Anthropic's training pipeline, with concrete
                    benchmarks for each.
                  </>
                )}
                {idx === 1 && (
                  <ul style={{ margin: 0, paddingLeft: 24, color: "rgba(255,255,255,0.85)" }}>
                    <li>Constitutional AI replaces 70% of human RLHF effort</li>
                    <li>Context windows scale better with attention sinks</li>
                    <li>Eval suites should be private, not Hugging Face</li>
                  </ul>
                )}
                {idx === 2 && (
                  <div style={{ color: brand.tronBlue, fontFamily: '"Geist Mono", monospace', fontSize: 18 }}>
                    arxiv.org/2212.08073 · github.com/anthropics/hh-rlhf
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
