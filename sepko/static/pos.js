/* SEPKO mini POS — desktop + PWA kasa */
(function () {
  var dataEl = document.getElementById("pos-articles-data");
  if (!dataEl) return;
  var articles = [];
  try {
    articles = JSON.parse(dataEl.textContent || "[]");
  } catch (e) {
    articles = [];
  }

  var grid = document.getElementById("pos-grid");
  var empty = document.getElementById("pos-empty");
  var search = document.getElementById("pos-search");
  var linesEl = document.getElementById("pos-lines");
  var cartEmpty = document.getElementById("pos-cart-empty");
  var totalEl = document.getElementById("pos-total");
  var hidden = document.getElementById("pos-hidden-lines");
  var form = document.getElementById("pos-form");
  var submitBtn = document.getElementById("pos-submit");
  var clearBtn = document.getElementById("pos-clear");
  var cart = [];
  var justAddedKey = null;
  var flashTimers = {};

  function money(n) {
    var s;
    if (window.SepkoMoney) s = window.SepkoMoney.format(n, 2);
    else s = (Math.round(n * 100) / 100).toFixed(2).replace(".", ",");
    return s + " €";
  }

  function lineKey(L) {
    return String(L.id) + "|" + String(L.price) + "|" + String(L.name);
  }

  function articleKey(a) {
    return String(a.id) + "|" + String(a.price) + "|" + String(a.name);
  }

  function qtyForArticle(a) {
    var L = findLine(a.id, a.price, a.name);
    return L ? L.qty : 0;
  }

  function matchQuery(a, q) {
    if (!q) return true;
    q = q.toLowerCase();
    return (
      (a.name || "").toLowerCase().indexOf(q) >= 0 ||
      (a.code || "").toLowerCase().indexOf(q) >= 0 ||
      (a.barcode || "").toLowerCase().indexOf(q) >= 0
    );
  }

  function flashTile(btn) {
    if (!btn) return;
    btn.classList.remove("is-added");
    void btn.offsetWidth;
    btn.classList.add("is-added");
    var badge = btn.querySelector(".pos-tile-badge");
    if (badge) {
      badge.classList.remove("is-pulse");
      void badge.offsetWidth;
      badge.classList.add("is-pulse");
    }
    var id = btn.getAttribute("data-akey");
    if (flashTimers[id]) clearTimeout(flashTimers[id]);
    flashTimers[id] = setTimeout(function () {
      btn.classList.remove("is-added");
      if (badge) badge.classList.remove("is-pulse");
    }, 850);
  }

  function updateTileBadges() {
    if (!grid) return;
    grid.querySelectorAll(".pos-tile").forEach(function (btn) {
      var aid = btn.getAttribute("data-aid");
      var price = Number(btn.getAttribute("data-price"));
      var name = btn.getAttribute("data-name") || "";
      var qty = 0;
      var L = findLine(aid, price, name);
      if (L) qty = L.qty;
      btn.classList.toggle("is-in-cart", qty > 0);
      var badge = btn.querySelector(".pos-tile-badge");
      if (qty > 0) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "pos-tile-badge";
          badge.innerHTML = '<i data-lucide="check" aria-hidden="true"></i><span class="pos-tile-badge-qty"></span>';
          btn.appendChild(badge);
          if (window.sepkoIcons) window.sepkoIcons();
        }
        var qEl = badge.querySelector(".pos-tile-badge-qty");
        if (qEl) qEl.textContent = qty === 1 ? "Dodato" : "×" + qty;
      } else if (badge) {
        badge.remove();
      }
    });
  }

  function renderGrid() {
    if (!grid) return;
    var q = (search && search.value) || "";
    var list = articles.filter(function (a) {
      return matchQuery(a, q);
    });
    grid.innerHTML = "";
    if (!list.length) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    list.forEach(function (a) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pos-tile";
      btn.setAttribute("data-aid", String(a.id));
      btn.setAttribute("data-price", String(a.price));
      btn.setAttribute("data-name", a.name || "");
      btn.setAttribute("data-akey", articleKey(a));
      var media =
        a.thumb
          ? '<span class="pos-tile-media"><img src="' +
            String(a.thumb).replace(/"/g, "") +
            '" alt="" loading="lazy"></span>'
          : '<span class="pos-tile-media pos-tile-media-fallback"><span>' +
            (a.initials || "?") +
            "</span></span>";
      btn.innerHTML =
        media +
        '<span class="pos-tile-body">' +
        '<span class="pos-tile-name"></span>' +
        '<span class="pos-tile-foot">' +
        '<span class="pos-tile-code"></span>' +
        '<strong class="pos-tile-price"></strong>' +
        "</span></span>";
      btn.querySelector(".pos-tile-name").textContent = a.name;
      btn.querySelector(".pos-tile-code").textContent = a.code || "";
      btn.querySelector(".pos-tile-price").textContent = money(a.price);
      btn.title = a.name + " · " + money(a.price);
      btn.addEventListener("click", function () {
        addLine(a, 1, btn);
      });
      grid.appendChild(btn);
    });
    updateTileBadges();
  }

  function findLine(id, price, name) {
    for (var i = 0; i < cart.length; i++) {
      var L = cart[i];
      if (String(L.id) === String(id) && Number(L.price) === Number(price) && L.name === name) return L;
    }
    return null;
  }

  function addLine(a, qty, tileBtn) {
    qty = qty || 1;
    var existing = findLine(a.id, a.price, a.name);
    if (existing) {
      existing.qty += qty;
    } else {
      cart.push({
        id: a.id,
        code: a.code || "POS",
        name: a.name,
        vat: a.vat,
        price: Number(a.price),
        qty: qty,
      });
    }
    justAddedKey = articleKey(a);
    renderCart();
    updateTileBadges();
    if (tileBtn) flashTile(tileBtn);
    else if (grid && justAddedKey) {
      grid.querySelectorAll(".pos-tile").forEach(function (el) {
        if (el.getAttribute("data-akey") === justAddedKey) flashTile(el);
      });
    }
  }

  function addCustom() {
    var name = window.prompt("Naziv / usluga", "Usluga");
    if (name == null) return;
    name = String(name).trim() || "Usluga";
    var raw = window.prompt("Iznos bruto (€)", "10,00");
    if (raw == null) return;
    var n = window.SepkoMoney
      ? window.SepkoMoney.parse(raw)
      : parseFloat(String(raw).replace(",", "."));
    if (!n || n <= 0) return;
    var vatRaw = window.prompt("PDV %", "21");
    var vat = parseFloat(String(vatRaw || "21").replace(",", ".")) || 21;
    addLine({ id: 0, code: "POS", name: name, vat: vat, price: n }, 1, null);
  }

  function renderCart() {
    if (!linesEl || !hidden) return;
    linesEl.innerHTML = "";
    hidden.innerHTML = "";
    var total = 0;
    cart.forEach(function (L, idx) {
      total += L.price * L.qty;
      var li = document.createElement("li");
      var key = lineKey(L);
      li.className = "pos-line" + (justAddedKey === key || justAddedKey === articleKey(L) ? " is-just-added" : "");
      li.setAttribute("data-lkey", key);
      li.innerHTML =
        '<div class="pos-line-top">' +
        '<span class="pos-line-name"></span>' +
        '<button type="button" class="pos-qty-btn danger" data-act="rm" aria-label="Ukloni">×</button>' +
        "</div>" +
        '<div class="pos-line-bottom">' +
        '<div class="pos-line-ctrl">' +
        '<button type="button" class="pos-qty-btn" data-act="dec" aria-label="-">−</button>' +
        '<span class="pos-qty"></span>' +
        '<button type="button" class="pos-qty-btn" data-act="inc" aria-label="+">+</button>' +
        "</div>" +
        '<span class="pos-line-meta">' +
        '<span class="pos-line-unit"></span>' +
        '<span class="pos-line-amt"></span>' +
        "</span>" +
        "</div>";
      li.querySelector(".pos-line-name").textContent = L.name;
      li.querySelector(".pos-line-amt").textContent = money(L.price * L.qty);
      li.querySelector(".pos-qty").textContent = String(L.qty);
      li.querySelector(".pos-line-unit").textContent =
        L.qty > 1 ? L.qty + " × " + money(L.price) : money(L.price) + "/kom";
      li.querySelectorAll(".pos-qty-btn").forEach(function (b) {
        b.addEventListener("click", function () {
          var act = b.getAttribute("data-act");
          if (act === "inc") {
            L.qty += 1;
            justAddedKey = articleKey(L);
          } else if (act === "dec") L.qty -= 1;
          else if (act === "rm") L.qty = 0;
          if (L.qty <= 0) {
            cart.splice(idx, 1);
            justAddedKey = null;
          }
          renderCart();
          updateTileBadges();
        });
      });
      linesEl.appendChild(li);

      function hid(name, val) {
        var inp = document.createElement("input");
        inp.type = "hidden";
        inp.name = name;
        inp.value = val;
        hidden.appendChild(inp);
      }
      hid("line_article_id", L.id || "0");
      hid("line_qty", String(L.qty));
      hid("line_price", String(L.price).replace(".", ","));
      hid("line_code", L.code || "POS");
      hid("line_name", L.name);
      hid("line_vat", String(L.vat));
    });
    if (totalEl) totalEl.textContent = money(total);
    if (cartEmpty) cartEmpty.hidden = cart.length > 0;
    var canSell = form && form.getAttribute("data-has-initial") === "1";
    if (submitBtn) submitBtn.disabled = !canSell || cart.length === 0;
    if (clearBtn) clearBtn.disabled = cart.length === 0;
    if (justAddedKey) {
      setTimeout(function () {
        justAddedKey = null;
      }, 900);
    }
  }

  if (search) {
    search.addEventListener("input", renderGrid);
    search.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var q = (search.value || "").trim();
      if (!q) return;
      var exact = articles.filter(function (a) {
        return (
          (a.barcode && a.barcode === q) ||
          (a.code && a.code.toLowerCase() === q.toLowerCase())
        );
      });
      if (exact.length === 1) {
        addLine(exact[0], 1, null);
        search.value = "";
        renderGrid();
        return;
      }
      var soft = articles.filter(function (a) {
        return matchQuery(a, q);
      });
      if (soft.length === 1) {
        addLine(soft[0], 1, null);
        search.value = "";
        renderGrid();
      }
    });
  }

  var customBtn = document.getElementById("pos-custom-btn");
  if (customBtn) customBtn.addEventListener("click", addCustom);

  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      if (!cart.length) return;
      cart = [];
      justAddedKey = null;
      renderCart();
      updateTileBadges();
    });
  }

  if (form) {
    form.addEventListener("submit", function (e) {
      if (!cart.length) {
        e.preventDefault();
        return;
      }
      if (form.getAttribute("data-has-initial") !== "1") {
        e.preventDefault();
        var blag = form.getAttribute("data-blagajna") || "/blagajna";
        window.location.href = blag;
        return;
      }
      e.preventDefault();
      if (submitBtn) submitBtn.disabled = true;
      // Otvori tab u istom gestu (klik) — inače browser blokira popup
      var receiptWin = window.open("about:blank", "sepko-racun");
      var fd = new FormData(form);
      fetch(form.getAttribute("action") || "/kasa/fiskalizuj", {
        method: "POST",
        body: fd,
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      })
        .then(function (r) {
          return r.json().then(function (data) {
            return { okHttp: r.ok, data: data };
          });
        })
        .then(function (res) {
          var data = res.data || {};
          if (data.redirect) {
            if (receiptWin) receiptWin.close();
            window.location.href = data.redirect;
            return;
          }
          if (!data.ok) {
            if (receiptWin) receiptWin.close();
            window.alert(data.error || "Fiskalizacija nije uspjela.");
            if (submitBtn) submitBtn.disabled = cart.length === 0;
            return;
          }
          if (data.receipt_url) {
            if (receiptWin && !receiptWin.closed) {
              receiptWin.location.href = data.receipt_url;
            } else {
              window.open(data.receipt_url, "sepko-racun", "noopener");
            }
          } else if (receiptWin) {
            receiptWin.close();
          }
          cart = [];
          justAddedKey = null;
          renderCart();
          updateTileBadges();
          // Osveži stanje blagajne / broj računa
          window.location.reload();
        })
        .catch(function () {
          if (receiptWin) receiptWin.close();
          window.alert("Greška pri fiskalizaciji. Pokušaj ponovo.");
          if (submitBtn) submitBtn.disabled = cart.length === 0;
        });
    });
  }

  if (!articles.length && empty) empty.hidden = false;
  renderGrid();
  renderCart();
  if (window.sepkoIcons) window.sepkoIcons();
})();
