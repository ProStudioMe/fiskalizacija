/* Sync Sepko data-theme with Web Awesome light/dark classes */
(function () {
  function syncWaTheme(theme) {
    var root = document.documentElement;
    root.classList.add('wa-theme-shoelace', 'wa-palette-shoelace');
    if (theme === 'dark') {
      root.classList.add('wa-dark');
      root.classList.remove('wa-light');
    } else {
      root.classList.add('wa-light');
      root.classList.remove('wa-dark');
    }
  }

  var cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  syncWaTheme(cur);

  var obs = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      if (mutations[i].attributeName === 'data-theme') {
        var t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        syncWaTheme(t);
      }
    }
  });
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  window.sepkoWaTheme = syncWaTheme;
})();
