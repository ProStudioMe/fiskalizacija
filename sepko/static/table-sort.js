/**
 * Univerzalno sortiranje tabela (.soft-table, .data-table).
 * Klik na th → asc/desc. Preskače checkbox i kolone s data-nosort / .th-sort (serverski).
 */
(function () {
  function cellText(td) {
    if (!td) return "";
    return (td.textContent || "").replace(/\s+/g, " ").trim();
  }

  function parseValue(text) {
    var t = text.replace(/\u00a0/g, " ").trim();
    if (!t || t === "—" || t === "-") return { n: null, s: "" };
    // broj tipa 1.234,56 € ili 69.00
    var num = t
      .replace(/[€%\s]/g, "")
      .replace(/\.(?=\d{3}(\D|$))/g, "")
      .replace(",", ".");
    if (/^-?\d+(\.\d+)?$/.test(num)) {
      return { n: parseFloat(num), s: t.toLowerCase() };
    }
    // datum dd.mm.yyyy
    var dm = t.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})/);
    if (dm) {
      return {
        n: Date.UTC(+dm[3], +dm[2] - 1, +dm[1]),
        s: t.toLowerCase(),
      };
    }
    // ISO / yyyy-mm-dd
    var iso = t.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (iso) {
      return {
        n: Date.UTC(+iso[1], +iso[2] - 1, +iso[3]),
        s: t.toLowerCase(),
      };
    }
    return { n: null, s: t.toLowerCase() };
  }

  function compare(a, b, dir) {
    var mul = dir === "asc" ? 1 : -1;
    if (a.n != null && b.n != null && !isNaN(a.n) && !isNaN(b.n)) {
      if (a.n < b.n) return -1 * mul;
      if (a.n > b.n) return 1 * mul;
      return 0;
    }
    if (a.s < b.s) return -1 * mul;
    if (a.s > b.s) return 1 * mul;
    return 0;
  }

  function enhance(table) {
    if (table.dataset.sortReady === "1") return;
    table.dataset.sortReady = "1";
    var thead = table.tHead;
    var tbody = table.tBodies[0];
    if (!thead || !tbody) return;
    var headerRow = thead.rows[0];
    if (!headerRow) return;

    Array.prototype.forEach.call(headerRow.cells, function (th, colIdx) {
      if (th.classList.contains("col-check")) return;
      if (th.hasAttribute("data-nosort")) return;
      if (th.querySelector("a.th-sort")) return; // već serverski sort
      var label = cellText(th);
      if (!label) return;
      // prazna / samo akcije
      if (th.querySelector(".row-actions") || th.classList.contains("cell-actions")) return;

      th.classList.add("th-sortable");
      th.setAttribute("role", "button");
      th.tabIndex = 0;
      th.title = "Sortiraj: " + label;

      function activate() {
        var cur = th.getAttribute("data-dir");
        var next = cur === "asc" ? "desc" : "asc";
        Array.prototype.forEach.call(headerRow.cells, function (h) {
          h.removeAttribute("data-dir");
          h.classList.remove("sorted-asc", "sorted-desc");
        });
        th.setAttribute("data-dir", next);
        th.classList.add(next === "asc" ? "sorted-asc" : "sorted-desc");

        var rows = Array.prototype.slice.call(tbody.rows);
        rows.sort(function (r1, r2) {
          var v1 = parseValue(cellText(r1.cells[colIdx]));
          var v2 = parseValue(cellText(r2.cells[colIdx]));
          return compare(v1, v2, next);
        });
        rows.forEach(function (r) {
          tbody.appendChild(r);
        });
      }

      th.addEventListener("click", function (e) {
        if (e.target.closest("a, button, input, select, details, summary, label")) return;
        activate();
      });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      });
    });
  }

  function init() {
    document
      .querySelectorAll("table.soft-table, table.data-table")
      .forEach(enhance);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
