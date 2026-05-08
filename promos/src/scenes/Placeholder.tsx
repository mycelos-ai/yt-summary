import React from "react";
import { AbsoluteFill } from "remotion";
import { brand } from "../lib/brand";

/**
 * Stand-in for scenes we haven't built yet. Renders the scene id
 * so the composition still plays end-to-end while we iterate.
 */
export const Placeholder: React.FC<{ id: string }> = ({ id }) => {
  return (
    <AbsoluteFill
      style={{
        background: brand.tronDeep,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          color: brand.tronBlue,
          fontSize: 48,
          fontWeight: 500,
          fontFamily: "monospace",
          letterSpacing: "0.5px",
          opacity: 0.7,
          textShadow: `0 0 16px ${brand.tronGlow}`,
        }}
      >
        ⟨ {id} ⟩
      </div>
    </AbsoluteFill>
  );
};
