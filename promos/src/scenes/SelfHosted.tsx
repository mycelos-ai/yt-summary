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
 * 89s - 105s: where the user keeps control. Quick Setup screenshot
 * with a Tron-blue glow border, copy: "Your stack. Your keys.
 * Your data." Six provider names appear as orbiting chips.
 */
const providers = [
  "OpenAI", "Anthropic", "Gemini", "Groq", "Ollama", "OpenRouter",
];

export const SelfHosted: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const headlineOpacity = interpolate(frame, [0, fps * 0.8], [0, 1], {
    extrapolateRight: "clamp",
  });

  const screenshotEntry = spring({
    frame: frame - fps * 0.5,
    fps,
    config: { damping: 18, stiffness: 80 },
  });
  const screenshotOpacity = interpolate(
    frame,
    [fps * 0.5, fps * 1.5],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const screenshotScale = 0.94 + screenshotEntry * 0.06;

  return (
    <AbsoluteFill
      style={{
        background:
          "radial-gradient(ellipse at center, #001a30 0%, #000 80%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "0 48px",
      }}
    >
      <div style={{ opacity: headlineOpacity, textAlign: "center", marginBottom: 32 }}>
        <h2
          style={{
            color: "white",
            fontSize: 64,
            fontWeight: 600,
            letterSpacing: "-1px",
            margin: 0,
          }}
        >
          Your stack.{" "}
          <span style={{ color: brand.tronBlue, textShadow: `0 0 24px ${brand.tronGlow}` }}>
            Your keys.
          </span>
        </h2>
        <p
          style={{
            color: "rgba(255,255,255,0.65)",
            fontSize: 22,
            marginTop: 12,
            letterSpacing: "0.3px",
          }}
        >
          Self-hosted. Pick any provider. Mix-and-match.
        </p>
      </div>

      <div
        style={{
          maxWidth: 1500,
          width: "85%",
          opacity: screenshotOpacity,
          transform: `scale(${screenshotScale})`,
          borderRadius: 16,
          overflow: "hidden",
          border: `1px solid ${brand.tronBlue}`,
          boxShadow: `0 0 80px ${brand.tronGlow}, 0 24px 80px rgba(0,0,0,0.6)`,
        }}
      >
        <Img
          src={staticFile("scenes/quick-setup.png")}
          style={{ width: "100%", height: "auto", display: "block" }}
        />
      </div>

      {/* Provider chips fade in sequentially */}
      <div
        style={{
          marginTop: 32,
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          justifyContent: "center",
        }}
      >
        {providers.map((name, idx) => {
          const start = fps * (2.5 + idx * 0.25);
          const peak = start + fps * 0.4;
          const opacity = interpolate(frame, [start, peak], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          });
          if (opacity <= 0) return null;
          return (
            <span
              key={name}
              style={{
                opacity,
                padding: "8px 16px",
                background: "rgba(74, 243, 255, 0.08)",
                border: `1px solid rgba(74, 243, 255, 0.5)`,
                borderRadius: 999,
                color: brand.tronBlue,
                fontFamily: '"Geist Mono", monospace',
                fontSize: 16,
                letterSpacing: "0.5px",
              }}
            >
              {name}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
