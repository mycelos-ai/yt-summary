(function () {
  const popover = document.getElementById('highlight-popover');
  if (!popover) return;

  const data = window.__HIGHLIGHT_DATA__ || {};
  const videoId = data.video_id;
  const source = data.source || 'summary';
  const target = document.querySelector(data.target_selector || '[data-highlight-target]');
  if (!videoId || !target) return;

  let lastSelection = null;
  let pendingSentiment = null;

  function getOffsets(range) {
    const pre = document.createRange();
    pre.selectNodeContents(target);
    pre.setEnd(range.startContainer, range.startOffset);
    const start = pre.toString().length;
    const end = start + range.toString().length;
    return [start, end];
  }

  function showPopover(rect) {
    popover.style.left = `${window.scrollX + rect.right}px`;
    popover.style.top = `${window.scrollY + rect.bottom + 6}px`;
    popover.hidden = false;
    popover.querySelector('[data-comment-form]').hidden = true;
  }

  function hidePopover() {
    popover.hidden = true;
    lastSelection = null;
    pendingSentiment = null;
  }

  document.addEventListener('selectionchange', () => {
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed) {
      hidePopover();
      return;
    }
    const range = sel.getRangeAt(0);
    if (!target.contains(range.commonAncestorContainer)) {
      hidePopover();
      return;
    }
    const text = sel.toString().trim();
    if (text.length < 3) {
      hidePopover();
      return;
    }
    const [start, end] = getOffsets(range);
    lastSelection = { text, start, end };
    showPopover(range.getBoundingClientRect());
  });

  async function postFeedback(sentiment, comment) {
    if (!lastSelection) return;
    const resp = await fetch('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        video_id: videoId,
        source: source,
        selected_text: lastSelection.text,
        text_offset_start: lastSelection.start,
        text_offset_end: lastSelection.end,
        sentiment: sentiment,
        comment: comment || null,
      }),
    });
    if (resp.ok) {
      showToast('Saved · profile will update');
    } else {
      showToast('Could not save feedback');
    }
    hidePopover();
  }

  popover.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'interesting' || action === 'not_interesting') {
      postFeedback(action, null);
    } else if (action === 'comment') {
      pendingSentiment = 'interesting';
      popover.querySelector('[data-comment-form]').hidden = false;
    } else if (action === 'copy' && lastSelection) {
      navigator.clipboard.writeText(lastSelection.text);
      showToast('Copied');
      hidePopover();
    }
  });

  popover.querySelector('[data-comment-form]').addEventListener('submit', (e) => {
    e.preventDefault();
    const ta = e.target.querySelector('textarea');
    postFeedback(pendingSentiment || 'interesting', ta.value);
    ta.value = '';
  });

  function showToast(msg) {
    let t = document.getElementById('highlight-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'highlight-toast';
      t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1f2937;color:#fff;padding:8px 14px;border-radius:6px;z-index:9999;transition:opacity 200ms';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.style.opacity = '0'; }, 2000);
  }

  // Restore existing highlights on load.
  if (Array.isArray(data.existing)) {
    const fullText = target.textContent;
    data.existing.forEach((fb) => {
      const idx = fullText.indexOf(fb.selected_text, fb.text_offset_start);
      if (idx === -1) return;
      const html = target.innerHTML;
      const escaped = fb.selected_text
        .replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      target.innerHTML = html.replace(
        new RegExp(escaped, 'i'),
        `<mark class="highlight-${fb.sentiment}" title="${(fb.comment || '').replace(/"/g, '&quot;')}">${fb.selected_text}</mark>`,
      );
    });
  }
})();
