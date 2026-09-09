(function () {
  var DISMISS_KEY = "sepko-pwa-dismiss";
  var INSTALLED_KEY = "sepko-pwa-installed";
  var DAYS = 14;
  var deferred = null;
  var shown = false;
  var mode = "install";
  var script = document.currentScript;
  var title = (script && script.getAttribute("data-title")) || "Instaliraj ProRačun";
  var msg = (script && script.getAttribute("data-msg")) ||
    "Dodaj ProRačun na početni ekran — brži pristup računima i fiskalizaciji, bez browser trake.";
  var okLabel = (script && script.getAttribute("data-ok")) || "Instaliraj";
  var laterLabel = (script && script.getAttribute("data-later")) || "Kasnije";
  var iosMsg = (script && script.getAttribute("data-ios")) ||
    "Na iPhone: Deli → Dodaj na početni ekran.";
  var openLabel = (script && script.getAttribute("data-open")) || "Otvori app";
  var openTitle = (script && script.getAttribute("data-open-title")) || "ProRačun je instaliran";
  var openMsg = (script && script.getAttribute("data-open-msg")) ||
    "Aplikacija je već na uređaju. Otvori je da radi bez browser trake.";
  var openIos = (script && script.getAttribute("data-open-ios")) ||
    "Otvori ProRačun sa početnog ekrana (ikona ProRačun).";
  var skip = (script && script.getAttribute("data-skip")) === "1";
  var btn = document.getElementById("btn-pwa-install");

  function standalone() {
    return window.matchMedia("(display-mode: standalone)").matches ||
      window.matchMedia("(display-mode: window-controls-overlay)").matches ||
      navigator.standalone === true;
  }

  function dismissed() {
    try {
      return parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10) > Date.now();
    } catch (e) {
      return false;
    }
  }

  function dismiss() {
    try {
      localStorage.setItem(DISMISS_KEY, String(Date.now() + DAYS * 864e5));
    } catch (e) {}
  }

  function markInstalled() {
    try {
      localStorage.setItem(INSTALLED_KEY, "1");
    } catch (e) {}
  }

  function knownInstalled() {
    try {
      return localStorage.getItem(INSTALLED_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function loggedIn() {
    return !!document.querySelector(".app");
  }

  function isIos() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent || "");
  }

  function setOpenMode() {
    mode = "open";
    markInstalled();
    deferred = null;
    if (!btn) return;
    btn.hidden = false;
    btn.textContent = openLabel;
    btn.setAttribute("aria-label", openLabel);
  }

  function openApp() {
    var url = "/app";
    if (isIos()) {
      if (!window.sepkoDialog) {
        window.location.assign(url);
        return;
      }
      window.sepkoDialog.alert({
        title: openTitle,
        message: openIos,
        tone: "info",
        okLabel: "U redu",
      });
      return;
    }
    var w = window.open(url, "sepko-pwa");
    if (!w) window.location.assign(url);
  }

  function showInstall() {
    if (shown || !deferred || !window.sepkoDialog) return;
    shown = true;
    if (btn) btn.hidden = false;
    window.sepkoDialog.confirm({
      title: title,
      message: msg,
      tone: "info",
      okLabel: okLabel,
      cancelLabel: laterLabel,
      onOk: function () {
        var ev = deferred;
        deferred = null;
        if (!ev || typeof ev.prompt !== "function") return;
        ev.prompt();
        if (ev.userChoice) {
          ev.userChoice.then(function (choice) {
            if (choice && choice.outcome === "accepted") {
              setOpenMode();
              return;
            }
            dismiss();
          }).catch(function () { dismiss(); });
        }
      },
      onCancel: dismiss,
    });
  }

  function showOpen() {
    if (shown || !window.sepkoDialog) return;
    shown = true;
    setOpenMode();
    window.sepkoDialog.confirm({
      title: openTitle,
      message: isIos() ? openIos : openMsg,
      tone: "info",
      okLabel: openLabel,
      cancelLabel: laterLabel,
      onOk: openApp,
      onCancel: dismiss,
    });
  }

  function showIosHint() {
    if (shown || dismissed() || !window.sepkoDialog) return;
    shown = true;
    window.sepkoDialog.alert({
      title: title,
      message: iosMsg,
      tone: "info",
      okLabel: "U redu",
      onOk: dismiss,
      onCancel: dismiss,
    });
  }

  function onBeforeInstall(e) {
    if (e && typeof e.preventDefault === "function") e.preventDefault();
    deferred = e || deferred;
    try { window.__sepkoDeferredPrompt = deferred; } catch (err) {}
    if (skip || standalone() || !loggedIn()) return;
    mode = "install";
    if (btn) {
      btn.hidden = false;
      btn.textContent = okLabel;
    }
    if (!dismissed()) {
      window.setTimeout(showInstall, 900);
    }
  }

  window.addEventListener("beforeinstallprompt", onBeforeInstall);
  if (window.__sepkoDeferredPrompt) onBeforeInstall(window.__sepkoDeferredPrompt);

  if (standalone()) {
    markInstalled();
    return;
  }

  if (skip || !loggedIn()) return;

  window.addEventListener("appinstalled", function () {
    setOpenMode();
  });

  if (btn) {
    btn.addEventListener("click", function () {
      shown = false;
      if (mode === "open" || knownInstalled()) {
        openApp();
        return;
      }
      if (deferred) showInstall();
      else if (isIos()) showIosHint();
    });
  }

  function detectInstalled() {
    if (deferred) return;
    var related = navigator.getInstalledRelatedApps;
    var ready = knownInstalled()
      ? Promise.resolve(true)
      : (typeof related === "function"
        ? related.call(navigator).then(function (apps) {
            return !!(apps && apps.length);
          }).catch(function () { return false; })
        : Promise.resolve(false));
    ready.then(function (yes) {
      if (!yes || deferred) return;
      setOpenMode();
      if (!dismissed()) window.setTimeout(showOpen, 900);
    });
  }

  window.setTimeout(detectInstalled, 1800);

  if (isIos() && knownInstalled()) {
    setOpenMode();
    if (!dismissed()) window.setTimeout(showOpen, 1200);
  } else if (isIos() && !dismissed()) {
    window.setTimeout(showIosHint, 1200);
  }
})();
