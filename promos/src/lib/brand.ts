/**
 * Brand tokens for the promo. Mirrors the colours from the app's
 * app.css :root variables, with two extra "Tron-blue" hues for the
 * synthwave aesthetic of the promo background.
 */

export const brand = {
  green: "#00d4a4",
  greenDeep: "#00b48a",
  ink: "#0a0a0a",
  canvas: "#ffffff",
  steel: "#5a5a5c",
  // Tron palette
  tronDeep: "#001428",
  tronBlue: "#4af3ff",
  tronCyan: "#00e5ff",
  tronGlow: "rgba(74, 243, 255, 0.6)",
};

export const fonts = {
  ui: '"Inter", -apple-system, sans-serif',
  mono: '"Geist Mono", "SF Mono", monospace',
};

/** Repo URL shown in the outro. */
export const REPO_URL = "github.com/mycelos-ai/yt-summary";

/** One-command installer shown in the outro panel. Kept short by
 *  splitting the pipe onto two lines so it fits at readable size on
 *  1920×1080 without horizontal cramping. */
export const INSTALL_CMD =
  "curl -fsSL https://raw.githubusercontent.com/mycelos-ai/yt-summary/main/install.sh | sh";
