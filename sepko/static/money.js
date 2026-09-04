/** Crnogorski format iznosa: 1.234,56 */
(function (global) {
  function formatAmount(n, ndigits) {
    ndigits = (ndigits == null) ? 2 : ndigits;
    var x = Number(n);
    if (!isFinite(x)) x = 0;
    var neg = x < 0;
    x = Math.abs(x);
    var fixed = x.toFixed(ndigits);
    var parts = fixed.split('.');
    var intPart = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    var out = ndigits > 0 ? (intPart + ',' + parts[1]) : intPart;
    return neg ? ('-' + out) : out;
  }

  function parseAmount(raw) {
    if (raw == null) return NaN;
    if (typeof raw === 'number') return raw;
    var t = String(raw).trim().replace(/\u00a0/g, '').replace(/\s/g, '');
    if (!t) return NaN;
    t = t.replace(/(?:€|eur)$/i, '').trim();
    if (!t) return NaN;
    if (t.indexOf(',') >= 0 && t.indexOf('.') >= 0) {
      t = t.replace(/\./g, '').replace(',', '.');
    } else if (t.indexOf(',') >= 0) {
      t = t.replace(',', '.');
    } else if ((t.match(/\./g) || []).length > 1) {
      t = t.replace(/\./g, '');
    }
    var n = Number(t);
    return isFinite(n) ? n : NaN;
  }

  global.SepkoMoney = { format: formatAmount, parse: parseAmount };
})(typeof window !== 'undefined' ? window : globalThis);
