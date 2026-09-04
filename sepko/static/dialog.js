(function () {
  var root = document.getElementById("sepko-dialog");
  if (!root) return;

  var titleEl = document.getElementById("sepko-dialog-title");
  var msgEl = document.getElementById("sepko-dialog-msg");
  var iconEl = root.querySelector(".confirm-icon");
  var cancelBtn = document.getElementById("sepko-dialog-cancel");
  var okBtn = document.getElementById("sepko-dialog-ok");
  var pendingOk = null;
  var lastFocus = null;
  var lblCancel = root.getAttribute("data-lbl-cancel") || "Odustani";
  var lblOk = root.getAttribute("data-lbl-ok") || "U redu";

  function iconFor(tone) {
    if (tone === "ok") return "✓";
    if (tone === "info") return "i";
    return "!";
  }

  function close() {
    root.hidden = true;
    pendingOk = null;
    document.body.classList.remove("sepko-dialog-open");
    if (lastFocus && typeof lastFocus.focus === "function") {
      try { lastFocus.focus(); } catch (e) {}
    }
    lastFocus = null;
  }

  function open(opts) {
    opts = opts || {};
    var tone = opts.tone || "warn";
    var isAlert = !!opts.alert;
    lastFocus = document.activeElement;
    if (titleEl) titleEl.textContent = opts.title || "Obavještenje";
    if (msgEl) msgEl.textContent = opts.message || "";
    if (iconEl) {
      iconEl.setAttribute("data-tone", tone);
      iconEl.textContent = iconFor(tone);
    }
    if (okBtn) {
      okBtn.className = "btn" + (tone === "danger" ? " danger" : "");
      okBtn.textContent = opts.okLabel || lblOk;
    }
    if (cancelBtn) {
      cancelBtn.textContent = opts.cancelLabel || lblCancel;
      cancelBtn.hidden = isAlert;
    }
    pendingOk = opts.onOk || null;
    root.hidden = false;
    document.body.classList.add("sepko-dialog-open");
    if (okBtn) okBtn.focus();
  }

  if (cancelBtn) cancelBtn.addEventListener("click", close);
  if (okBtn) {
    okBtn.addEventListener("click", function () {
      var fn = pendingOk;
      close();
      if (fn) fn();
    });
  }
  root.addEventListener("click", function (e) {
    if (e.target === root) close();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !root.hidden) {
      e.preventDefault();
      close();
    }
  });

  window.sepkoDialog = {
    confirm: function (opts) {
      open(opts);
    },
    alert: function (opts) {
      opts = opts || {};
      opts.alert = true;
      open(opts);
    },
  };

  document.addEventListener(
    "submit",
    function (e) {
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      if (form.getAttribute("data-sepko-ok") === "1") {
        form.removeAttribute("data-sepko-ok");
        return;
      }
      var msg = form.getAttribute("data-confirm");
      if (!msg) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      open({
        title: form.getAttribute("data-confirm-title") || "Potvrda",
        message: msg,
        tone: form.getAttribute("data-confirm-tone") || "danger",
        okLabel: form.getAttribute("data-confirm-ok") || "Obriši",
        cancelLabel: form.getAttribute("data-confirm-cancel") || lblCancel,
        onOk: function () {
          form.setAttribute("data-sepko-ok", "1");
          HTMLFormElement.prototype.submit.call(form);
        },
      });
    },
    true
  );

  document.querySelectorAll(".flash").forEach(function (el) {
    if (el.querySelector(".flash-dismiss")) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "flash-dismiss";
    btn.setAttribute("aria-label", "Zatvori");
    btn.textContent = "×";
    btn.addEventListener("click", function () {
      el.classList.add("flash-hide");
      window.setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 220);
    });
    el.appendChild(btn);
    if (!el.classList.contains("error") && !el.classList.contains("warn")) {
      window.setTimeout(function () {
        if (!el.parentNode || el.classList.contains("flash-hide")) return;
        btn.click();
      }, 7000);
    }
  });
})();
