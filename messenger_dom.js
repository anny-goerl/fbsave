// Read-only collector. No clicks, keyboard input, network interception or app APIs.
// Evaluated before navigation so removals during virtual scrolling are observed.
(() => {
  if (window !== window.top) return;
  const expected = __CHAT_ID__;
  const selector = '[data-scope="messages_table"]';
  const queue = [];
  let queuedRows = 0, queuedBytes = 0, lastSnapshot = '';
  let sequence = 0, acknowledged = 0, overflow = false, ambiguous = false;
  let removedRows = 0;
  function blocked() {
    return !!document.querySelector('input[type="password"], input[name="email"], input[name="pin"], ' +
      'input[autocomplete="one-time-code"], iframe[src*="captcha"], iframe[src*="recaptcha"], ' +
      '[data-testid*="captcha"], form[action*="/checkpoint"]');
  }
  function inChat() {
    const p = location.pathname;
    const m = p.match(/^\/(?:messages\/)?(?:e2ee\/)?t\/([^/]+)\/?$/);
    return m && m[1] === expected;
  }
  function table() {
    if (!inChat() || blocked()) return null;
    const tables = [...document.querySelectorAll(selector)];
    // Multiple lists may be separate open chats. Never guess which to export.
    if (tables.length > 1) ambiguous = true;
    return tables.length === 1 ? tables[0] : null;
  }
  function read(row, root) {
    if (row.closest(selector) !== root) return null;
    const labels = [];
    const metadataElements = [];
    // A row's accessibility label can be on a single-row wrapper.
    for (let e = row; e && root.contains(e); e = e.parentElement) {
      if (e !== row && e.querySelectorAll('[role="row"]').length !== 1) break;
      metadataElements.push(e);
      if (e.hasAttribute('aria-label')) labels.push(e.getAttribute('aria-label'));
      if (e === root) break;
    }
    for (const e of row.querySelectorAll('[aria-label]')) {
      if (e.closest('[role="row"]') === row) labels.push(e.getAttribute('aria-label'));
    }
    let messageId = '', idSource = '';
    for (const attr of ['data-message-id', 'data-mid']) {
      const e = metadataElements.find(e => e.hasAttribute(attr)) || row.querySelector(`[${attr}]`);
      if (e && e.getAttribute(attr)) {
        messageId = e.getAttribute(attr); idSource = attr; break;
      }
    }
    if (!messageId) {
      for (const a of row.querySelectorAll('a[href]')) {
        try {
          const u = new URL(a.href);
          // Only an actual message permalink in this chat, never an attachment ID.
          if (!u.pathname.match(new RegExp('/t/' + expected + '/?$'))) continue;
          for (const key of ['message_id', 'mid']) {
            if (u.searchParams.has(key)) {
              messageId = u.searchParams.get(key); idSource = 'permalink:' + key; break;
            }
          }
        } catch (_) {}
        if (messageId) break;
      }
    }
    const time = row.querySelector('time[datetime], [data-timestamp]');
    const attachments = [], attachmentUrls = new Set();
    for (const e of row.querySelectorAll('img, video, audio, a[download], a[data-attachment], source')) {
      // Exclude known avatars and emoji. Unknown images are retained as candidates.
      if (e.matches('img') && (e.closest('[data-scope="avatar"]') ||
          /^(profilbild|profile picture|avatar|emoji)(\b|:)/i.test(e.alt || ''))) continue;
      const url = e.currentSrc || e.src || e.href;
      if (!url || attachmentUrls.has(url)) continue;
      attachmentUrls.add(url);
      attachments.push({url, kind: e.tagName.toLowerCase(),
        name: e.getAttribute('download') || '', alt: e.getAttribute('alt') || ''});
    }
    const sender = row.getAttribute('data-sender') ||
      row.querySelector('[data-sender]')?.getAttribute('data-sender') || '';
    const content = row.querySelector('[data-message-text], [data-ad-preview="message"]');
    return {message_id: messageId, id_source: idSource, labels,
      timestamp: row.getAttribute('data-timestamp') || time?.getAttribute('datetime') ||
        time?.getAttribute('data-timestamp') || '',
      sender, text: content ? content.textContent : '',
      // Keep unknown row content rather than silently discard unrecognised messages.
      dom_text: row.innerText || row.textContent || '', attachments};
  }
  function enqueue(rows) {
    if (!rows.length) return;
    const snapshot = JSON.stringify(rows);
    if (snapshot === lastSnapshot) return;
    const bytes = snapshot.length * 2;
    if (queue.length >= 10000 || queuedRows + rows.length > 50000 ||
        queuedBytes + bytes > 32 * 1024 * 1024) { overflow = true; return; }
    lastSnapshot = snapshot;
    queuedRows += rows.length; queuedBytes += bytes;
    queue.push({sequence: ++sequence, rows, bytes});
  }
  function scan() {
    const root = table();
    if (root) enqueue([...root.querySelectorAll('[role="row"]')].map(r => read(r, root)).filter(Boolean));
  }
  const observer = new MutationObserver(records => {
    const root = table();
    if (!root) return;
    // Removed subtrees still have their text/attributes when MutationObserver runs.
    for (const record of records) {
      if (!(record.target === root || root.contains(record.target))) continue;
      for (const node of record.removedNodes) {
        if (node.nodeType !== 1) continue;
        const rows = node.matches('[role="row"]') ? [node] : [...node.querySelectorAll('[role="row"]')];
        const values = rows.map(r => {
          // Detached rows no longer have closest(root); use a temporary read root.
          const wrapper = document.createElement('div');
          wrapper.setAttribute('data-scope', 'messages_table');
          wrapper.appendChild(node.cloneNode(true));
          const copies = [...wrapper.querySelectorAll('[role="row"]')];
          return read(copies[rows.indexOf(r)], wrapper);
        }).filter(Boolean);
        removedRows += values.length;
        enqueue(values);
      }
    }
    scan();
  });
  observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  function scrollState(direction) {
    const root = table();
    if (!root) return {available: false};
    let scroller = root;
    while (scroller && !(scroller.clientHeight > 0 &&
        scroller.scrollHeight > scroller.clientHeight + 2 &&
        /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
    // Never scroll the page/sidebar as a fallback.
    if (!scroller || scroller === document.body || scroller === document.documentElement)
      return {available: true, scrollable: false, at_top: true, at_bottom: true};
    if (direction) {
      scan();
      scroller.scrollTop += (direction === 'up' ? -1 : 1) * Math.max(1, scroller.clientHeight * 0.65);
    }
    const reverse = getComputedStyle(scroller).flexDirection === 'column-reverse';
    return {available: true, scrollable: true, reverse, top: scroller.scrollTop,
      height: scroller.scrollHeight, viewport: scroller.clientHeight,
      at_top: reverse ? scroller.scrollTop <= -(scroller.scrollHeight - scroller.clientHeight) + 2 : scroller.scrollTop <= 2,
      at_bottom: reverse ? scroller.scrollTop >= -2 : scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2};
  }
  window.__fbsave = {
    poll() {
      scan();
      const root = table();
      return {batches: queue.filter(b => b.sequence > acknowledged), overflow, ambiguous,
        blocked: blocked(),
        removed_rows: removedRows,
        available: !!root, loading: !!root && (root.matches('[aria-busy="true"]') ||
          !!root.querySelector('[role="progressbar"], [aria-busy="true"]')),
        scroll: scrollState()};
    },
    ack(n) {
      acknowledged = Math.max(acknowledged, n);
      while (queue.length && queue[0].sequence <= acknowledged) {
        const batch = queue.shift();
        queuedRows -= batch.rows.length; queuedBytes -= batch.bytes;
      }
    },
    scroll: scrollState
  };
})();
