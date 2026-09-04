/* SEPKO — Lucide icons helper */
(function () {
  function refresh() {
    if (typeof lucide === "undefined" || !lucide.createIcons) return;
    lucide.createIcons({
      attrs: { "stroke-width": 1.75 },
    });
  }
  window.sepkoIcons = refresh;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }
})();
