/* CG fiscal QR scanner — BarcodeDetector + jsQR fallback */
(function () {
  var video = document.getElementById("qr-video");
  var canvas = document.getElementById("qr-canvas");
  var statusEl = document.getElementById("scan-status");
  var rawInput = document.getElementById("qr-raw");
  var form = document.getElementById("qr-form");
  var btnStart = document.getElementById("btn-start-cam");
  var btnStop = document.getElementById("btn-stop-cam");
  if (!video || !btnStart) return;

  var stream = null;
  var raf = null;
  var detector = null;
  var stopped = true;

  function setStatus(msg) {
    if (statusEl) statusEl.textContent = msg;
  }

  function onCode(text) {
    if (!text) return;
    stopCam();
    if (rawInput) rawInput.value = text;
    setStatus("QR pročitan — šaljem…");
    if (form) form.submit();
  }

  function stopCam() {
    stopped = true;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    if (stream) {
      stream.getTracks().forEach(function (t) {
        t.stop();
      });
      stream = null;
    }
    video.srcObject = null;
    if (btnStop) btnStop.hidden = true;
    if (btnStart) btnStart.hidden = false;
  }

  async function tick() {
    if (stopped) return;
    try {
      if (detector && video.readyState >= 2) {
        var codes = await detector.detect(video);
        if (codes && codes.length) {
          onCode(codes[0].rawValue);
          return;
        }
      } else if (window.jsQR && canvas && video.readyState >= 2) {
        var w = video.videoWidth;
        var h = video.videoHeight;
        if (w && h) {
          canvas.width = w;
          canvas.height = h;
          var ctx = canvas.getContext("2d");
          ctx.drawImage(video, 0, 0, w, h);
          var img = ctx.getImageData(0, 0, w, h);
          var code = window.jsQR(img.data, img.width, img.height, {
            inversionAttempts: "dontInvert",
          });
          if (code && code.data) {
            onCode(code.data);
            return;
          }
        }
      }
    } catch (e) {
      /* continue */
    }
    raf = requestAnimationFrame(tick);
  }

  async function startCam() {
    stopCam();
    stopped = false;
    try {
      if ("BarcodeDetector" in window) {
        try {
          detector = new window.BarcodeDetector({ formats: ["qr_code"] });
        } catch (e) {
          detector = null;
        }
      }
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } },
        audio: false,
      });
      video.srcObject = stream;
      await video.play();
      if (btnStart) btnStart.hidden = true;
      if (btnStop) btnStop.hidden = false;
      setStatus("Usmjerite kameru na QR…");
      raf = requestAnimationFrame(tick);
    } catch (e) {
      setStatus("Kamera nije dostupna: " + (e && e.message ? e.message : e));
      stopped = true;
    }
  }

  btnStart.addEventListener("click", startCam);
  if (btnStop) btnStop.addEventListener("click", stopCam);
})();
