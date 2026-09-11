/* Sync Sepko data-theme with Web Awesome light/dark classes + select chevrons */
(function () {
  function syncWaTheme(theme) {
    var root = document.documentElement;
    root.classList.add("wa-theme-shoelace", "wa-palette-shoelace");
    if (theme === "dark") {
      root.classList.add("wa-dark");
      root.classList.remove("wa-light");
    } else {
      root.classList.add("wa-light");
      root.classList.remove("wa-dark");
    }
  }

  var cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  syncWaTheme(cur);

  var obs = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      if (mutations[i].attributeName === "data-theme") {
        var t = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
        syncWaTheme(t);
      }
    }
  });
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  window.sepkoWaTheme = syncWaTheme;

  /* WA default expand icon often empty without FA kit — inject SVG chevron everywhere */
  var CHEVRON_HTML =
    '<svg slot="expand-icon" class="wa-select-chevron" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">' +
    '<path fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" d="m6 9 6 6 6-6"/>' +
    "</svg>";

  function ensureSelectChevrons(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var list = [];
    if (scope.matches && scope.matches("wa-select")) list.push(scope);
    if (scope.querySelectorAll) {
      scope.querySelectorAll("wa-select").forEach(function (el) {
        list.push(el);
      });
    }
    list.forEach(function (sel) {
      if (sel.querySelector('[slot="expand-icon"]')) return;
      sel.insertAdjacentHTML("afterbegin", CHEVRON_HTML);
    });
  }

  function bootChevrons() {
    ensureSelectChevrons(document);
    if (!window.MutationObserver || !document.body) return;
    var mo = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var nodes = mutations[i].addedNodes;
        for (var j = 0; j < nodes.length; j++) {
          var n = nodes[j];
          if (!n || n.nodeType !== 1) continue;
          ensureSelectChevrons(n);
        }
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootChevrons);
  } else {
    bootChevrons();
  }

  window.sepkoEnsureSelectChevrons = ensureSelectChevrons;
})();
