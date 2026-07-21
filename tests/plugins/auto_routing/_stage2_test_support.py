"""Shared loopback and resolver helpers for Stage 2 Auto Routing E2Es."""

from __future__ import annotations

import json
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class LoopbackProvider(AbstractContextManager["LoopbackProvider"]):
    """Small OpenAI-compatible endpoint with an entry-time assertion hook."""

    def __init__(
        self,
        *,
        response_text: str = "ok",
        on_request_entry: Callable[[], None] | None = None,
        on_chat_request_entry: Callable[[], None] | None = None,
        status_code: int = 200,
        status_codes: tuple[int, ...] = (),
    ) -> None:
        self.response_text = response_text
        self.on_request_entry = on_request_entry
        self.on_chat_request_entry = on_chat_request_entry
        self.status_code = status_code
        self.status_codes = status_codes
        self.requests: list[dict[str, Any]] = []
        self.authorization_headers: list[str | None] = []
        self.chat_authorization_headers: list[str | None] = []
        self.entry_errors: list[BaseException] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LoopbackProvider":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    if owner.on_request_entry is not None:
                        owner.on_request_entry()
                except BaseException as error:  # surfaced to the test thread
                    owner.entry_errors.append(error)
                    self.send_error(500)
                    return

                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.requests.append(request)
                owner.authorization_headers.append(self.headers.get("Authorization"))
                is_chat_request = "messages" in request
                if is_chat_request:
                    owner.chat_authorization_headers.append(
                        self.headers.get("Authorization")
                    )
                    try:
                        if owner.on_chat_request_entry is not None:
                            owner.on_chat_request_entry()
                    except BaseException as error:  # surfaced to the test thread
                        owner.entry_errors.append(error)
                        self.send_error(500)
                        return
                request_index = len(owner.chat_authorization_headers) - 1
                status_code = (
                    owner.status_codes[request_index]
                    if is_chat_request and request_index < len(owner.status_codes)
                    else owner.status_code
                )
                if status_code != 200:
                    payload = json.dumps(
                        {"error": {"message": "injected", "type": "test"}}
                    ).encode("utf-8")
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if request.get("stream") is True:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = (
                        {
                            "id": "stage2-response",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": request.get("model", "stage2-model"),
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": owner.response_text,
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "id": "stage2-response",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": request.get("model", "stage2-model"),
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        },
                    )
                    for chunk in chunks:
                        self.wfile.write(
                            f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return

                payload = json.dumps(
                    {
                        "id": "stage2-response",
                        "object": "chat.completion",
                        "created": 1,
                        "model": request.get("model", "stage2-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": owner.response_text,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 1,
                            "total_tokens": 6,
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __exit__(self, *_args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def install_runtime_resolver(monkeypatch, resolver: Any) -> PluginManager:
    """Install exactly one resolver without discovering unrelated plugins."""
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(
        PluginManifest(name="stage2-test-router", key="stage2-test-router"),
        manager,
    )
    context.register_agent_runtime_resolver(resolver)
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    return manager


def plugin_manifest(root: Path) -> PluginManifest:
    return PluginManifest(
        name="auto-routing",
        version="0.1.0",
        description="Stage 2 Auto Routing contract tests",
        source="bundled",
        path=root / "plugins" / "auto_routing",
    )
