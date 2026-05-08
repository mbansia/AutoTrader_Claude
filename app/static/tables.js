// Lightweight enhancement layer for tables across the dashboard.
//
// Three independent subsystems share a single per-row visibility model so
// they layer cleanly:
//
//   data-match-search       global search input (data-searchable)
//   data-match-collapse     show-N-rows toggle (data-collapse-after)
//   data-match-col-filters  per-column filter inputs (auto on every table)
//
// A row is visible iff every flag is non-"0". Each subsystem updates its
// flag and calls applyVisibility(rows). No subsystem touches `hidden`
// directly — that's the unified output, not the input.
//
// Plus: clickable column-header sorting (asc → desc → original DOM order).
//
// Opt-outs: `data-no-sort` on a `<th>` skips sort for that column;
// `data-no-column-filter` on a `<table>` skips the per-column filter row.
//
// No deps. Conservative IE11+ syntax.

(function () {
  function getRows(container) {
    if (container.tagName === 'TABLE') {
      return Array.prototype.slice.call(container.querySelectorAll(':scope > tbody > tr'));
    }
    return Array.prototype.slice.call(container.children).filter(function (c) {
      return c.classList && c.classList.contains('log-line');
    });
  }

  function applyVisibility(rows) {
    rows.forEach(function (r) {
      var hide = r.dataset.matchSearch === '0'
              || r.dataset.matchCollapse === '0'
              || r.dataset.matchColFilters === '0';
      if (hide) r.setAttribute('hidden', '');
      else r.removeAttribute('hidden');
    });
  }

  // ─── Layer 1: global search + collapse ──────────────────────────────
  function instrumentSearchCollapse(container) {
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
        r.dataset.matchSearch = matches ? '1' : '0';
        if (matches) totalMatching++;
      });
      var matchIdx = 0;
      rows.forEach(function (r) {
        if (r.dataset.matchSearch !== '1') return;
        var collapseHide = !state.expanded && !q && n > 0 && matchIdx >= n;
        r.dataset.matchCollapse = collapseHide ? '0' : '1';
        matchIdx++;
      });
      // Rows that didn't match search still need a definite collapse flag.
      rows.forEach(function (r) {
        if (r.dataset.matchSearch !== '1') r.dataset.matchCollapse = '1';
      });
      applyVisibility(rows);
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

    rows.forEach(function (r) {
      r.dataset.matchSearch = '1';
      r.dataset.matchCollapse = '1';
    });
    recompute();
  }

  // ─── Layer 2: sortable headers ──────────────────────────────────────
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
    var sortState = { col: -1, dir: 0 };

    function snapshotOrder() {
      var rows = Array.prototype.slice.call(tbody.children);
      rows.forEach(function (r, i) { r.dataset._origIdx = String(i); });
      return rows;
    }
    var original = snapshotOrder();

    function sortByColumn(idx) {
      var rows = Array.prototype.slice.call(tbody.children);
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
          if (an === null) return 1;
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

  // ─── Layer 3: per-column filter inputs ──────────────────────────────
  // Injects a second <tr> into <thead> with one <th> per column. Each
  // <th> has a small <input type="search">; typing narrows visible
  // rows to those whose cell at that column contains the substring
  // (case-insensitive). Multiple column filters AND together. Combined
  // with the global search + collapse via the shared visibility model.
  function instrumentColumnFilters(table) {
    if (table.hasAttribute('data-no-column-filter')) return;
    var thead = table.querySelector(':scope > thead');
    var tbody = table.querySelector(':scope > tbody');
    if (!thead || !tbody) return;
    var headerRow = thead.querySelector(':scope > tr');
    if (!headerRow) return;
    var ths = Array.prototype.slice.call(headerRow.children);
    if (!ths.length) return;

    // Build the filter row mirroring the header column count.
    var filterRow = document.createElement('tr');
    filterRow.className = 'col-filter-row';
    var filterValues = ths.map(function () { return ''; });

    var rowsRef = [];

    function recompute() {
      // Refresh rows snapshot in case sort reordered them in-place
      // (sort uses appendChild so the live HTMLCollection still works,
      // but cells indices are stable).
      rowsRef = Array.prototype.slice.call(tbody.children);
      rowsRef.forEach(function (r) {
        var pass = true;
        for (var i = 0; i < filterValues.length; i++) {
          var q = filterValues[i];
          if (!q) continue;
          var v = cellValue(r, i).toLowerCase();
          if (v.indexOf(q) === -1) { pass = false; break; }
        }
        r.dataset.matchColFilters = pass ? '1' : '0';
      });
      applyVisibility(rowsRef);
    }

    ths.forEach(function (th, idx) {
      var cell = document.createElement('th');
      cell.className = 'col-filter-cell';
      var input = document.createElement('input');
      input.type = 'search';
      input.className = 'col-filter-input';
      input.setAttribute('aria-label', 'Filter ' + ((th.textContent || '').trim() || ('column ' + (idx + 1))));
      input.placeholder = 'filter…';
      // Stop click events here from bubbling to the sortable header
      // sitting above (otherwise typing into the filter would also
      // trigger a sort cycle on every focus click).
      input.addEventListener('click', function (e) { e.stopPropagation(); });
      input.addEventListener('keydown', function (e) { e.stopPropagation(); });
      input.addEventListener('input', function () {
        filterValues[idx] = input.value.toLowerCase().trim();
        recompute();
      });
      cell.appendChild(input);
      filterRow.appendChild(cell);
    });
    thead.appendChild(filterRow);

    // Initialize all rows to "passing" so the unified visibility model
    // doesn't hide them before any filter is typed.
    Array.prototype.slice.call(tbody.children).forEach(function (r) {
      r.dataset.matchColFilters = '1';
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-searchable], [data-collapse-after]').forEach(instrumentSearchCollapse);
    document.querySelectorAll('table').forEach(instrumentSort);
    document.querySelectorAll('table').forEach(instrumentColumnFilters);
  });
})();
