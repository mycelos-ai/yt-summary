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

/** One-command installer shown in the outro panel.
 *
 *  Uses the mycelos.com vanity domain (302-redirected to the GitHub
 *  raw URL) for brevity and brand consistency in the video. The
 *  underlying script and repo stay public at github.com/mycelos-ai/
 *  yt-summary — mycelos.com is just a comfort entry point. */
export const INSTALL_CMD =
  "curl -fsSL mycelos.com/yt-summary/install.sh | sh";
