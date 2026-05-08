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
 * 22s - 40s: the answer to the problem. The actual YouTube UI with
 * the context menu open ("Save to Watch later / Save to playlist /
 * ...") slides in from the right; on the left, the headline
 * "Save → your queue." appears in three beats.
 *
 * The image is the strongest part of this scene because it shows
 * the literal action the user takes — no abstraction needed.
 */
export const SaveToQueue: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Three beats of headline reveal — tighter cadence (was ~1.5s,
  // now ~0.7s between beats) for the shorter cut.
  const a1 = interpolate(frame, [0, fps * 0.5], [0, 1], { extrapolateRight: "clamp" });
  const a2 = interpolate(frame, [fps * 0.7, fps * 1.2], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const a3 = interpolate(frame, [fps * 1.6, fps * 2.2], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  // Screenshot slides in at 2.5s instead of 4s — leaves ~7.5s of
  // dwell time so the eye can find the menu item.
  const entryStart = fps * 2.5;
  const screenshotEntry = spring({
    frame: frame - entryStart,
    fps,
    config: { damping: 18, stiffness: 75 },
  });
  const screenshotOpacity = interpolate(
    frame,
    [entryStart, entryStart + fps * 0.8],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const screenshotX = interpolate(screenshotEntry, [0, 1], [800, 0]);

  return (
    <AbsoluteFill
      style={{
        background:
          "radial-gradient(ellipse at left, #001a30 0%, #000 70%)",
        display: "flex",
        alignItems: "center",
        padding: "0 80px",
      }}
    >
      {/* Left half: headline */}
      <div style={{ flex: 1, maxWidth: "45%" }}>
        <div
          style={{
            opacity: a1,
            color: brand.tronBlue,
            fontFamily: '"Geist Mono", monospace',
            fontSize: 18,
            letterSpacing: "4px",
            textTransform: "uppercase",
            marginBottom: 12,
          }}
        >
          The killer pattern
        </div>
        <h2
          style={{
            color: "white",
            fontSize: 96,
            fontWeight: 600,
            lineHeight: 1.05,
            letterSpacing: "-2px",
            margin: 0,
          }}
        >
          <span style={{ opacity: a1 }}>Save </span>
          <span
            style={{
              opacity: a2,
              color: brand.tronBlue,
              textShadow: `0 0 32px ${brand.tronGlow}`,
            }}
          >
            → your queue.
          </span>
        </h2>
        <p
          style={{
            opacity: a3,
            color: "rgba(255,255,255,0.75)",
            fontSize: 26,
            fontWeight: 400,
            marginTop: 32,
            lineHeight: 1.4,
            maxWidth: 520,
          }}
        >
          Tap save while you scroll on your phone or laptop. The
          summary lands here a few minutes later.
        </p>
      </div>

      {/* Right half: YouTube UI screenshot with the menu open */}
      <div
        style={{
          flex: 1.2,
          opacity: screenshotOpacity,
          transform: `translateX(${screenshotX}px)`,
          display: "flex",
          justifyContent: "flex-end",
          position: "relative",
        }}
      >
        <div
          style={{
            position: "relative",
            borderRadius: 16,
            overflow: "hidden",
            border: `1px solid ${brand.tronBlue}`,
            boxShadow: `0 0 64px ${brand.tronGlow}, 0 16px 48px rgba(0,0,0,0.6)`,
            maxWidth: "100%",
          }}
        >
          <Img
            src={staticFile("scenes/save-to-playlist.png")}
            style={{ width: "100%", height: "auto", display: "block" }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};
