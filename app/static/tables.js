// Lightweight enhancement for any element marked `data-searchable` and/or
// `data-collapse-after="N"`.
//
// - Tables: rows are `tbody > tr`. Lists: direct children with class `log-line`.
// - Search filters rows by case-insensitive substring match on textContent.
// - When a search query is active, the collapse cap is bypassed so all matches
//   are visible.
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
      // For tables wrapped in `.table-wrapper`, insert the search above the wrapper.
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

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-searchable], [data-collapse-after]').forEach(instrument);
  });
})();
