#!/usr/bin/env python3
"""Receive browser MediaRecorder chunks over loopback and append them durably."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Queue
import signal
import threading
import time
from urllib.parse import parse_qs, urlparse


@dataclass
class CaptureState:
    output: Path
    token: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    chunks: int = 0
    bytes_written: int = 0
    last_sequence: int = -1
    started_at: float = field(default_factory=time.time)
    last_chunk_at: float | None = None
    subscribers: list[Queue[bytes | None]] = field(default_factory=list)

    def append(self, payload: bytes, sequence: int) -> None:
        if not payload:
            raise ValueError("empty chunk")
        with self.lock:
            if sequence != self.last_sequence + 1:
                raise ValueError(
                    f"unexpected sequence {sequence}; expected {self.last_sequence + 1}"
                )
            with self.output.open("ab", buffering=0) as handle:
                handle.write(payload)
            self.chunks += 1
            self.bytes_written += len(payload)
            self.last_sequence = sequence
            self.last_chunk_at = time.time()
            for subscriber in self.subscribers:
                subscriber.put_nowait(payload)

    def subscribe(self) -> tuple[int, Queue[bytes | None]]:
        with self.lock:
            backlog_size = self.output.stat().st_size
            subscriber: Queue[bytes | None] = Queue()
            self.subscribers.append(subscriber)
            return backlog_size, subscriber

    def unsubscribe(self, subscriber: Queue[bytes | None]) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    def close_subscribers(self) -> None:
        with self.lock:
            subscribers = list(self.subscribers)
            self.subscribers.clear()
        for subscriber in subscribers:
            subscriber.put_nowait(None)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "chunks": self.chunks,
                "bytes_written": self.bytes_written,
                "last_sequence": self.last_sequence,
                "started_at": self.started_at,
                "last_chunk_at": self.last_chunk_at,
                "output": str(self.output),
                "subscribers": len(self.subscribers),
            }


class CaptureServer(ThreadingHTTPServer):
    state: CaptureState
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server: CaptureServer

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == f"/{self.server.state.token}/stream":
            self._stream()
            return
        if parsed.path != f"/{self.server.state.token}/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._json(HTTPStatus.OK, self.server.state.snapshot())

    def _stream(self) -> None:
        backlog_size, subscriber = self.server.state.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/webm")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            with self.server.state.output.open("rb") as handle:
                remaining = backlog_size
                while remaining:
                    block = handle.read(min(64 * 1024, remaining))
                    if not block:
                        raise OSError("capture backlog ended before snapshot boundary")
                    self.wfile.write(block)
                    remaining -= len(block)
            self.wfile.flush()
            while True:
                payload = subscriber.get()
                if payload is None:
                    return
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.server.state.unsubscribe(subscriber)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != f"/{self.server.state.token}/chunk":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            sequence = int(parse_qs(parsed.query)["seq"][0])
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024 * 1024:
                raise ValueError("invalid content length")
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise ValueError("truncated request body")
            self.server.state.append(payload, sequence)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(exist_ok=True)
    args.output.chmod(0o600)

    state = CaptureState(args.output.resolve(), args.token)
    server = CaptureServer(("127.0.0.1", args.port), Handler)
    server.state = state

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(json.dumps({"ready": True, "port": args.port, **state.snapshot()}), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        state.close_subscribers()
        server.server_close()
        manifest = args.output.with_suffix(args.output.suffix + ".json")
        manifest.write_text(json.dumps(state.snapshot(), ensure_ascii=False, indent=2) + "\n")
        manifest.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
