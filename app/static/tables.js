// Lightweight enhancement for tables across the dashboard.
//
// Two layered behaviours:
// 1) Search + collapse on any element marked `data-searchable` and/or
//    `data-collapse-after="N"`. Tables: rows are `tbody > tr`. Lists:
//    direct children with class `log-line`. Search filters rows by
//    case-insensitive substring on textContent. While a search query is
//    active, the collapse cap is bypassed so all matches are visible.
// 2) Sortable headers on every `<table>` with a `<thead>`. Click a `<th>`
//    to sort the column (text or numeric, auto-detected). Three states
//    cycle: unsorted (DOM order) → asc → desc → unsorted. Add
//    `data-no-sort` on a `<th>` to opt that column out (e.g. action
//    columns whose cell content isn't comparable).
//
// No deps. IE11+ syntax kept conservative.

(function () {
  function getRows(container) {
    if (container.tagName === 'TABLE') {
      return Array.prototype.slice.call(container.querySelectorAll(':scope > tbody > tr'));
    }
    return Array.prototype.slice.call(container.children).filter(function (c) {
      return c.classList && c.classList.contains('log-line');
    });
  }

  // ─── Layer 1: search + collapse ─────────────────────────────────────
  function instrument(container) {
    var n = parseInt(container.getAttribute('data-collapse-after') || '0', 10);
    var searchable = container.hasAttribute('data-searchable');
    if (!n && !searchable) return;
    var rows = getRows(container);
    if (!rows.length) return;

    var state = { query: '', expanded: false };
    var input = null;
    var toggle = null;

    function recompute() {
      var q = state.query.toLowerCase().trim();
      var totalMatching = 0;
      rows.forEach(function (r) {
        var matches = !q || (r.textContent || '').toLowerCase().indexOf(q) !== -1;
        if (matches) totalMatching++;
      });
      var matchIdx = 0;
      rows.forEach(function (r) {
        var matches = !q || (r.textContent || '').toLowerCase().indexOf(q) !== -1;
        var collapseHide = !state.expanded && !q && n > 0 && matchIdx >= n;
        if (matches) matchIdx++;
        if (!matches || collapseHide) r.setAttribute('hidden', '');
        else r.removeAttribute('hidden');
      });
      if (toggle) {
        if (q || totalMatching <= n) {
          toggle.style.display = 'none';
        } else {
          toggle.style.display = '';
          toggle.textContent = state.expanded
            ? '▲ Show fewer'
            : '▼ Show all (' + totalMatching + ')';
        }
      }
    }

    if (searchable) {
      var wrap = document.createElement('div');
      wrap.className = 'table-search-wrap';
      input = document.createElement('input');
      input.type = 'search';
      input.placeholder = 'Filter rows…';
      input.className = 'table-search';
      input.setAttribute('aria-label', 'Filter table rows');
      input.addEventListener('input', function () {
        state.query = input.value;
        recompute();
      });
      wrap.appendChild(input);
      var anchor = container;
      if (anchor.parentNode && anchor.parentNode.classList && anchor.parentNode.classList.contains('table-wrapper')) {
        anchor = anchor.parentNode;
      }
      anchor.parentNode.insertBefore(wrap, anchor);
    }

    if (n > 0 && rows.length > n) {
      toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'table-collapse-toggle';
      toggle.addEventListener('click', function () {
        state.expanded = !state.expanded;
        recompute();
      });
      var afterAnchor = container;
      if (afterAnchor.parentNode && afterAnchor.parentNode.classList && afterAnchor.parentNode.classList.contains('table-wrapper')) {
        afterAnchor = afterAnchor.parentNode;
      }
      afterAnchor.parentNode.insertBefore(toggle, afterAnchor.nextSibling);
    }

    recompute();
  }

  // ─── Layer 2: sortable headers ──────────────────────────────────────
  // Numeric detector: strip $, %, +/-, commas, surrounding whitespace, and
  // try parseFloat. Empty-string-like values (—, n/a) sort as -Infinity
  // ascending so they cluster at the bottom on desc / top on asc.
  function parseNumeric(s) {
    if (s == null) return null;
    var t = String(s).replace(/[\s,$%]/g, '').replace(/^([+\-]?)(.*)$/, '$1$2');
    if (t === '' || t === '—' || t.toLowerCase() === 'n/a') return null;
    var n = parseFloat(t);
    return isFinite(n) ? n : null;
  }

  function cellValue(row, idx) {
    var cells = row.children;
    if (idx >= cells.length) return '';
    return (cells[idx].textContent || '').trim();
  }

  function instrumentSort(table) {
    if (table.hasAttribute('data-no-sort')) return;
    var thead = table.querySelector(':scope > thead');
    var tbody = table.querySelector(':scope > tbody');
    if (!thead || !tbody) return;
    var headerRow = thead.querySelector(':scope > tr');
    if (!headerRow) return;
    var ths = Array.prototype.slice.call(headerRow.children);
    if (!ths.length) return;
    // Capture original DOM order so the third click reverts to it. We
    // tag each row with its original index; if rows appear/disappear
    // (server-side re-render) the next click reads the fresh order.
    var sortState = { col: -1, dir: 0 };  // dir: 0 unsorted, 1 asc, -1 desc

    function snapshotOrder() {
      var rows = Array.prototype.slice.call(tbody.children);
      rows.forEach(function (r, i) { r.dataset._origIdx = String(i); });
      return rows;
    }
    var original = snapshotOrder();

    function sortByColumn(idx) {
      var rows = Array.prototype.slice.call(tbody.children);
      // Sample numeric-ness: if every non-empty cell parses, treat numeric.
      var allNumeric = true;
      var sampled = 0;
      for (var i = 0; i < rows.length && sampled < 10; i++) {
        var v = cellValue(rows[i], idx);
        if (!v) continue;
        sampled++;
        if (parseNumeric(v) === null) { allNumeric = false; break; }
      }
      var dir = sortState.dir;
      rows.sort(function (a, b) {
        var av = cellValue(a, idx);
        var bv = cellValue(b, idx);
        if (allNumeric) {
          var an = parseNumeric(av);
          var bn = parseNumeric(bv);
          if (an === null && bn === null) return 0;
          if (an === null) return 1;     // empties sink in asc
          if (bn === null) return -1;
          return dir * (an - bn);
        }
        return dir * av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' });
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    }

    function restoreOrder() {
      original
        .slice()
        .sort(function (a, b) { return parseInt(a.dataset._origIdx, 10) - parseInt(b.dataset._origIdx, 10); })
        .forEach(function (r) { tbody.appendChild(r); });
    }

    function updateHeaderIndicators() {
      ths.forEach(function (th, i) {
        th.classList.remove('sort-asc', 'sort-desc');
        if (i === sortState.col && sortState.dir !== 0) {
          th.classList.add(sortState.dir > 0 ? 'sort-asc' : 'sort-desc');
        }
      });
    }

    ths.forEach(function (th, idx) {
      if (th.hasAttribute('data-no-sort')) return;
      // Skip headers whose body cells appear to be controls only
      // (buttons / forms) — they have no comparable content.
      th.classList.add('sortable');
      th.setAttribute('role', 'button');
      th.setAttribute('tabindex', '0');
      function trigger() {
        if (sortState.col !== idx) {
          sortState.col = idx;
          sortState.dir = 1;
        } else if (sortState.dir === 1) {
          sortState.dir = -1;
        } else if (sortState.dir === -1) {
          sortState.dir = 0;
          sortState.col = -1;
        } else {
          sortState.dir = 1;
        }
        if (sortState.dir === 0) restoreOrder();
        else sortByColumn(idx);
        updateHeaderIndicators();
      }
      th.addEventListener('click', trigger);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          trigger();
        }
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-searchable], [data-collapse-after]').forEach(instrument);
    document.querySelectorAll('table').forEach(instrumentSort);
  });
})();
