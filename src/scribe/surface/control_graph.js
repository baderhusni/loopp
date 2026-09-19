() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD','META','LINK','TITLE']);
  const txt = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g,' ').trim();

  function visible(el) {
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    return true;
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'password') return 'textbox';
      if (t === 'hidden') return 'hidden';
      return 'textbox';
    }
    return 'generic';
  }

  // Accessible name, with the recovery steps legacy markup actually needs.
  // Returns [name, source].
  function nameOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return [aria.trim(), 'aria'];
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/).map(id => txt(document.getElementById(id))).filter(Boolean);
      if (parts.length) return [parts.join(' '), 'aria'];
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && txt(lab)) return [txt(lab), 'label'];
    }
    const wrap = el.closest('label');
    if (wrap && txt(wrap)) return [txt(wrap), 'label'];

    const tag = el.tagName, type = (el.type || '').toLowerCase();
    if (tag === 'INPUT' && ['submit','button','reset'].includes(type) && el.value)
      return [el.value.trim(), 'value'];
    if (tag === 'INPUT' && type === 'image' && el.alt) return [el.alt.trim(), 'value'];
    if (tag === 'BUTTON' && txt(el)) return [txt(el), 'text'];
    if (tag === 'A') {
      if (txt(el)) return [txt(el), 'text'];
      const img = el.querySelector('img[alt]');
      if (img && img.alt.trim()) return [img.alt.trim(), 'text'];
    }

    // --- the legacy case: the name lives in a neighbouring table cell ---
    const cell = el.closest('td, th');
    if (cell) {
      let prev = cell.previousElementSibling;
      while (prev) {
        const t = txt(prev);
        if (t) return [t.replace(/[:\s]+$/,''), 'table_adjacent'];
        prev = prev.previousElementSibling;
      }
      // nothing to the left: try this column's header
      const row = cell.parentElement, table = cell.closest('table');
      if (row && table) {
        const idx = Array.prototype.indexOf.call(row.children, cell);
        const head = table.querySelector('tr');
        if (head && head !== row && head.children[idx]) {
          const t = txt(head.children[idx]);
          if (t) return [t, 'table_adjacent'];
        }
      }
    }

    // free-form layout: nearest text before the control
    let n = el.previousSibling, guard = 0;
    while (n && guard++ < 6) {
      const t = (n.nodeType === 3 ? n.textContent : txt(n)).replace(/\s+/g,' ').trim();
      if (t) return [t.replace(/[:\s]+$/,''), 'text'];
      n = n.previousSibling;
    }
    if (el.placeholder) return [el.placeholder.trim(), 'placeholder'];
    if (el.title) return [el.title.trim(), 'title'];
    if (el.name) return [el.name.trim(), 'attr_name'];
    return ['', 'none'];
  }

  function cssPath(el) {
    const parts = [];
    let cur = el, depth = 0;
    while (cur && cur.nodeType === 1 && depth++ < 6) {
      let seg = cur.tagName.toLowerCase();
      if (cur.getAttribute && cur.getAttribute('name'))
        seg += `[name="${cur.getAttribute('name')}"]`;
      else {
        const sibs = cur.parentElement ? Array.from(cur.parentElement.children)
          .filter(c => c.tagName === cur.tagName) : [];
        if (sibs.length > 1) seg += `:nth-of-type(${sibs.indexOf(cur)+1})`;
      }
      parts.unshift(seg);
      if (cur.parentElement === document.body || !cur.parentElement) break;
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  // A frame's coordinates are its own. Clicking by coordinate happens at the
  // top-level page, so every box is offset by where this frame sits in it.
  let offX = 0, offY = 0;
  try {
    let win = window;
    while (win.frameElement) {
      const fr = win.frameElement.getBoundingClientRect();
      offX += fr.left; offY += fr.top;
      win = win.parent;
    }
  } catch (e) { /* cross-origin: fall back to frame-relative */ }

  const out = { url: location.href, title: document.title, elements: [], tables: [],
                frame_offset: [offX, offY] };
  const sel = 'input, select, textarea, button, a[href], [role], [onclick]';
  let i = 0;
  document.querySelectorAll('[data-scribe-ref]').forEach(e => e.removeAttribute('data-scribe-ref'));
  for (const el of document.querySelectorAll(sel)) {
    if (SKIP.has(el.tagName)) continue;
    const role = roleOf(el);
    if (role === 'hidden' || role === 'generic') continue;
    if (!visible(el)) continue;
    const [name, name_source] = nameOf(el);
    const ref = 'e' + (++i);
    el.setAttribute('data-scribe-ref', ref);
    const r = el.getBoundingClientRect();
    out.elements.push({
      ref, role, name, name_source, tag: el.tagName,
      value: (el.tagName === 'SELECT'
              ? (el.selectedOptions[0] ? el.selectedOptions[0].text : '')
              : (el.type === 'password' ? '' : (el.value || ''))),
      enabled: !el.disabled,
      field_name: el.getAttribute('name') || '',
      link_text: el.tagName === 'A' ? txt(el) : '',
      css: cssPath(el),
      elem_id: el.id || '',
      bbox: [r.x + offX, r.y + offY, r.width, r.height],
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text) : undefined,
    });
  }

  for (const tb of document.querySelectorAll('table')) {
    const rows = Array.from(tb.rows);
    if (rows.length < 2) continue;
    const hdrCells = Array.from(rows[0].cells);
    const isHeader = hdrCells.length > 1 && hdrCells.every(c => c.tagName === 'TH');
    if (!isHeader) continue;                       // layout table, not a data grid
    let caption = '';
    let prev = tb.previousElementSibling, g = 0;
    while (prev && g++ < 3) { const t = txt(prev); if (t) { caption = t.slice(0,60); break; }
                              prev = prev.previousElementSibling; }
    out.tables.push({
      caption,
      headers: hdrCells.map(c => txt(c)),
      rows: rows.slice(1).map(r => Array.from(r.cells).map(c => txt(c))),
    });
  }
  // Label/value pairs. Legacy summary screens are layout tables of
  // <td>caption</td><td>value</td>, which is neither a data grid nor a form,
  // and is exactly where the values worth extracting live.
  out.pairs = [];
  for (const tb of document.querySelectorAll('table')) {
    const rows = Array.from(tb.rows);
    const headerish = rows.length && Array.from(rows[0].cells).every(c => c.tagName === 'TH');
    if (headerish) continue;                       // that one is a data grid
    for (const r of rows) {
      const cells = Array.from(r.cells);
      for (let i = 0; i + 1 < cells.length; i += 2) {
        const label = txt(cells[i]), value = txt(cells[i+1]);
        if (!label || label.length > 40) continue;
        if (cells[i].querySelector('input, select, textarea, button')) continue;
        out.pairs.push({ label: label.replace(/[:\s]+$/,''), value });
      }
    }
  }
  out.text = (document.body ? (document.body.innerText || '') : '').replace(/\n{3,}/g,'\n\n').trim();
  return out;
}
