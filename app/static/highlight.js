(function () {
  const popover = document.getElementById('highlight-popover');
  if (!popover) return;

  const data = window.__HIGHLIGHT_DATA__ || {};
  const defaultVideoId = data.video_id;
  const defaultSource = data.source || 'summary';
  const targetSelector = data.target_selector || '[data-highlight-target]';

  // Multiple targets supported: video detail page has one summary
  // target with the default video_id; digest page has N source items
  // each with its own data-video-id attribute (so feedback lands on
  // the right Video row).
  const targets = Array.from(document.querySelectorAll(targetSelector));
  if (targets.length === 0) return;

  let lastSelection = null;
  let pendingSentiment = null;

  function targetForRange(range) {
    // The closest target containing the selection — that's the one
    // we anchor feedback to. If the selection straddles two targets
    // (shouldn't happen in practice) we ignore it.
    for (const t of targets) {
      if (t.contains(range.commonAncestorContainer)) return t;
    }
    return null;
  }

  function anchorForTarget(target) {
    // Returns { videoId, digestId } — exactly one is set.
    // Resolution order: data-video-id on the target → data-digest-id
    // on the target → default video_id from __HIGHLIGHT_DATA__ (video
    // detail page only ever uses video_id).
    if (target.dataset.videoId) {
      return { videoId: target.dataset.videoId, digestId: null };
    }
    if (target.dataset.digestId) {
      return { videoId: null, digestId: target.dataset.digestId };
    }
    if (defaultVideoId) {
      return { videoId: defaultVideoId, digestId: null };
    }
    return { videoId: null, digestId: null };
  }

  function sourceForTarget(target) {
    return target.dataset.feedbackSource || defaultSource;
  }

  function getOffsets(target, range) {
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
    const target = targetForRange(range);
    if (!target) {
      hidePopover();
      return;
    }
    const text = sel.toString().trim();
    if (text.length < 3) {
      hidePopover();
      return;
    }
    const { videoId, digestId } = anchorForTarget(target);
    if (!videoId && !digestId) {
      hidePopover();
      return;
    }
    const [start, end] = getOffsets(target, range);
    lastSelection = {
      text,
      start,
      end,
      videoId,
      digestId,
      source: sourceForTarget(target),
    };
    showPopover(range.getBoundingClientRect());
  });

  async function postFeedback(sentiment, comment) {
    if (!lastSelection) return;
    const body = {
      source: lastSelection.source,
      selected_text: lastSelection.text,
      text_offset_start: lastSelection.start,
      text_offset_end: lastSelection.end,
      sentiment: sentiment,
      comment: comment || null,
    };
    // XOR: send video_id OR digest_id, never both.
    if (lastSelection.videoId) {
      body.video_id = lastSelection.videoId;
    } else {
      body.digest_id = parseInt(lastSelection.digestId, 10);
    }
    const resp = await fetch('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
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

  // Restore existing highlights on load. Each existing fb carries
  // either a video_id or a digest_id; pick the matching target. Video
  // detail page omits both (single default target).
  if (Array.isArray(data.existing)) {
    data.existing.forEach((fb) => {
      let target = targets[0];
      if (fb.video_id || fb.digest_id) {
        const match = targets.find((t) => {
          const a = anchorForTarget(t);
          if (fb.video_id) return a.videoId === fb.video_id;
          if (fb.digest_id) return String(a.digestId) === String(fb.digest_id);
          return false;
        });
        if (match) target = match;
        else return;  // no matching target → skip silently
      }
      const fullText = target.textContent;
      const idx = fullText.indexOf(fb.selected_text, fb.text_offset_start || 0);
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
