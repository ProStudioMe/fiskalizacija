"""Lokalni print agent (POS računar) — ESC/POS preko WebSocket/HTTP.

Preglednik nema USB/Serial. Pokreni na kasi:

    python -m sepko.print_agent

Web UI šalje job na ws://127.0.0.1:17890/ws (ili POST /print).
Ako agent nije pokrenut, UI pada na window.print() + @media print.
"""
from __future__ import annotations

import argparse
import base64
import sys
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from sepko.config import get_settings


class PrintJob(BaseModel):
    printer: str = ""
    payload_b64: str
    copies: int = Field(default=1, ge=1, le=5)


def write_escpos(data: bytes, printer: str) -> str:
    """Pošalji RAW ESC/POS. `printer`: prazno=stdout hex, `file:putanja`, `tcp:host:port`."""
    target = (printer or "").strip()
    if not target:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return "stdout"
    if target.startswith("file:"):
        path = target.removeprefix("file:")
        with open(path, "ab") as fh:
            fh.write(data)
        return path
    if target.startswith("tcp:"):
        import socket

        rest = target.removeprefix("tcp:")
        host, _, port_s = rest.rpartition(":")
        port = int(port_s or "9100")
        with socket.create_connection((host or "127.0.0.1", port), timeout=8) as sock:
            sock.sendall(data)
        return f"{host}:{port}"
    if sys.platform == "win32":
        return _win_raw(target, data)
    raise RuntimeError(f"Nepoznat printer target: {target!r}")


def _win_raw(printer_name: str, data: bytes) -> str:
    import ctypes
    from ctypes import wintypes

    winspool = ctypes.WinDLL("winspool.drv")
    hprinter = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(hprinter), None):
        raise RuntimeError(f"OpenPrinter nije uspio: {printer_name}")
    try:
        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [
                ("pDocName", wintypes.LPWSTR),
                ("pOutputFile", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
            ]

        doc = DOC_INFO_1("SEPKO ESC/POS", None, "RAW")
        if not winspool.StartDocPrinterW(hprinter, 1, ctypes.byref(doc)):
            raise RuntimeError("StartDocPrinter nije uspio")
        try:
            winspool.StartPagePrinter(hprinter)
            written = wintypes.DWORD()
            buf = ctypes.create_string_buffer(data, len(data))
            ok = winspool.WritePrinter(hprinter, buf, len(data), ctypes.byref(written))
            winspool.EndPagePrinter(hprinter)
            if not ok:
                raise RuntimeError("WritePrinter nije uspio")
        finally:
            winspool.EndDocPrinter(hprinter)
    finally:
        winspool.ClosePrinter(hprinter)
    return printer_name


def _run_job(job: PrintJob) -> dict[str, Any]:
    raw = base64.b64decode(job.payload_b64)
    last = ""
    for _ in range(job.copies):
        last = write_escpos(raw, job.printer)
    return {"ok": True, "bytes": len(raw) * job.copies, "target": last}


app = FastAPI(title="Sepko print agent", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sepko-print-agent"}


@app.post("/print")
def print_http(job: PrintJob) -> dict[str, Any]:
    return _run_job(job)


@app.websocket("/ws")
async def print_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"ok": True, "event": "ready"})
    try:
        while True:
            data = await websocket.receive_json()
            job = PrintJob.model_validate(data)
            await websocket.send_json(_run_job(job))
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await websocket.send_json({"ok": False, "error": str(exc)})


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Sepko lokalni ESC/POS agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17890)
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("uvicorn je potreban za print agent") from exc
    print(f"Sepko print agent ws://{args.host}:{args.port}/ws  (POST /print)")
    _ = settings
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
