(function (global) {
  function b64(bytes) {
    var bin = "";
    var arr = new Uint8Array(bytes);
    for (var i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
    return btoa(bin);
  }

  function sendWs(url, job, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var ws;
      try {
        ws = new WebSocket(url);
      } catch (err) {
        reject(err);
        return;
      }
      var timer = setTimeout(function () {
        try { ws.close(); } catch (e) {}
        reject(new Error("print agent timeout"));
      }, timeoutMs || 4000);
      ws.onopen = function () {
        ws.send(JSON.stringify(job));
      };
      ws.onmessage = function (ev) {
        clearTimeout(timer);
        try {
          var msg = JSON.parse(ev.data);
          if (msg.event === "ready") return;
          ws.close();
          if (msg.ok) resolve(msg);
          else reject(new Error(msg.error || "print failed"));
        } catch (err) {
          ws.close();
          reject(err);
        }
      };
      ws.onerror = function () {
        clearTimeout(timer);
        reject(new Error("print agent offline"));
      };
    });
  }

  async function sendToAgent(opts) {
    var res = await fetch(opts.escposUrl, { credentials: "same-origin" });
    if (!res.ok) throw new Error("escpos " + res.status);
    var buf = await res.arrayBuffer();
    var job = {
      printer: opts.printerName || "",
      payload_b64: b64(buf),
      copies: 1
    };
    var url = opts.agentUrl || "ws://127.0.0.1:17890/ws";
    return sendWs(url, job);
  }

  async function printPage(opts) {
    opts = opts || {};
    var type = opts.printerType || "";
    if (type === "thermal" && opts.escposUrl) {
      try {
        await sendToAgent(opts);
        return { via: "agent" };
      } catch (err) {
        console.warn("Sepko print agent nedostupan, window.print()", err);
      }
    }
    global.print();
    return { via: "browser" };
  }

  global.SepkoPrint = { printPage: printPage, sendToAgent: sendToAgent };
})(window);
