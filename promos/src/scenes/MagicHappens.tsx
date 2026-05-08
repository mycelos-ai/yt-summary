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
 * 40s - 64s (24s, the musical drop): the architecture diagram fades
 * in centered, then four step labels emerge in sequence as the
 * pipeline runs left → right (YouTube videos → yt-summary engine →
 * Knowledge cards). The diagram image already shows the four steps;
 * we layer numbered callout chips that brighten in time.
 */
export const MagicHappens: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Diagram fade-in over the first 0.8s, with a tiny scale settle.
  const diagramOpacity = interpolate(frame, [0, fps * 0.8], [0, 1], {
    extrapolateRight: "clamp",
  });
  const diagramScale = spring({
    frame,
    fps,
    config: { damping: 16, stiffness: 70, mass: 0.8 },
  });

  // Headline above the diagram, fades in slightly later.
  const headlineOpacity = interpolate(
    frame,
    [fps * 0.6, fps * 1.4],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Four step pulses, ~2s apart starting at 2.5s. The drop in the
  // music is at this point — pulses sync to the bass rhythm.
  const steps = [
    { label: "01 · Extract", x: 30, y: 78 },
    { label: "02 · Summarize", x: 50, y: 78 },
    { label: "03 · Understand", x: 50, y: 86 },
    { label: "04 · Knowledge", x: 78, y: 78 },
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
      <div
        style={{
          opacity: headlineOpacity,
          marginBottom: 32,
          textAlign: "center",
        }}
      >
        <div
          style={{
            color: brand.tronBlue,
            fontFamily: brand.tronBlue ? '"Geist Mono", monospace' : "inherit",
            fontSize: 16,
            letterSpacing: "4px",
            textTransform: "uppercase",
            opacity: 0.8,
            marginBottom: 8,
          }}
        >
          The pipeline
        </div>
        <h2
          style={{
            color: "white",
            fontSize: 56,
            fontWeight: 600,
            letterSpacing: "-1px",
            margin: 0,
            textShadow: `0 0 24px ${brand.tronGlow}`,
          }}
        >
          Videos in. Knowledge out.
        </h2>
      </div>

      <div
        style={{
          position: "relative",
          width: "78%",
          maxWidth: 1500,
          opacity: diagramOpacity,
          transform: `scale(${0.92 + diagramScale * 0.08})`,
          filter: "drop-shadow(0 0 64px rgba(74, 243, 255, 0.15))",
        }}
      >
        <Img
          src={staticFile("scenes/architecture-diagram.png")}
          style={{
            width: "100%",
            height: "auto",
            display: "block",
            borderRadius: 12,
          }}
        />

        {/* Step callout pulses, each peaks at its own moment then
            fades back to a low ambient brightness. Coordinates are
            percent-based on the diagram so they stay anchored
            regardless of render scale. */}
        {steps.map((step, idx) => {
          // Steps fire at 2.5, 4.5, 6.5, 8.5s — 4 pulses across ~8s,
          // leaving 5s of headline + 1s tail.
          const pulseStart = fps * (2.5 + idx * 2.0);
          const pulsePeak = pulseStart + fps * 0.4;
          const pulseEnd = pulseStart + fps * 1.5;
          const pulseOut = pulseStart + fps * 2.2;
          const intensity = interpolate(
            frame,
            [pulseStart, pulsePeak, pulseEnd, pulseOut],
            [0, 1, 1, 0.35],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
          );
          if (intensity <= 0) return null;
          return (
            <div
              key={step.label}
              style={{
                position: "absolute",
                left: `${step.x}%`,
                top: `${step.y}%`,
                transform: "translate(-50%, -50%)",
                padding: "8px 14px",
                background: `rgba(0, 20, 40, ${0.7 + intensity * 0.25})`,
                border: `1px solid ${brand.tronBlue}`,
                borderRadius: 999,
                color: brand.tronBlue,
                fontFamily: '"Geist Mono", monospace',
                fontSize: 18,
                fontWeight: 500,
                letterSpacing: "0.5px",
                opacity: 0.4 + intensity * 0.6,
                boxShadow: `0 0 ${20 + intensity * 30}px ${brand.tronGlow}`,
                whiteSpace: "nowrap",
              }}
            >
              {step.label}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
