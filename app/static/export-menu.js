// Clipboard helpers for the export menu. Alpine drives open/close; these
// do the copy actions. Progressive enhancement: if the clipboard API is
// unavailable (old browser / non-secure context), fall back to a prompt
// so the user can still grab the text/URL.
function _ytsFlash(btn, msg) {
  if (!btn) return;
  const original = btn.textContent;
  btn.textContent = msg;
  setTimeout(() => { btn.textContent = original; }, 1500);
}

async function _ytsWrite(text, btn, okMsg) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      _ytsFlash(btn, okMsg);
      return;
    }
    throw new Error("clipboard unavailable");
  } catch (e) {
    window.prompt("Copy manually:", text);
  }
}

async function ytsCopyMarkdown(mdUrl, btn) {
  try {
    const resp = await fetch(mdUrl);
    if (!resp.ok) throw new Error("fetch failed: " + resp.status);
    const text = await resp.text();
    await _ytsWrite(text, btn, "Copied ✓");
  } catch (e) {
    window.prompt("Couldn't fetch the Markdown — here's the link:",
      new URL(mdUrl, location).href);
  }
}

function ytsCopyLink(mdUrl, btn) {
  _ytsWrite(new URL(mdUrl, location).href, btn, "Link copied ✓");
}
