"""Real PluginManager and HTTP AIAgent Stage 1 foundation invariants."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import _clear_hermes_path_and_config_caches
from hermes_cli.plugins import get_plugin_manager
from plugins.auto_routing.auto_routing.storage import RoutingStore
from plugins.auto_routing.auto_routing.models import RuntimeKey
from plugins.auto_routing.auto_routing.profile_key import (
    ProfileKeyError,
    ensure_profile_credential_fingerprint_key,
    read_profile_credential_fingerprint_key_if_present,
)


@pytest.mark.skipif(os.name != "posix", reason="fchmod path is POSIX-only")
def test_profile_key_creation_closes_raw_descriptor_when_fchmod_fails(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import profile_key

    key_path = isolated_home / "auto-routing" / "injected-profile-key"
    key_path.parent.mkdir(parents=True)
    opened: list[tuple[int, Path]] = []
    real_mkstemp = profile_key.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        descriptor, temporary = real_mkstemp(*args, **kwargs)
        opened.append((descriptor, Path(temporary)))
        return descriptor, temporary

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected profile-key fchmod failure")

    monkeypatch.setattr(profile_key.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(profile_key.os, "fchmod", fail_fchmod, raising=False)

    with pytest.raises(OSError, match="injected profile-key fchmod failure"):
        profile_key._create_key_without_replacement(
            key_path,
            home=isolated_home,
        )

    assert len(opened) == 1
    descriptor, temporary = opened[0]
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not temporary.exists()
    assert not key_path.exists()


class _ProviderHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(request)
        if request.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = (
                {
                    "id": "response",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": request.get("model", "test-model"),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ok"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "response",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": request.get("model", "test-model"),
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                },
            )
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        response = {
            "id": "response",
            "object": "chat.completion",
            "created": 1,
            "model": request.get("model", "test-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "total_tokens": 6,
            },
        }
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def fake_provider():
    _ProviderHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", _ProviderHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_real_agent_once(base_url: str) -> tuple[tuple[str, str], str]:
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url=base_url,
        provider="openai-compat",
        model="test-model",
        max_iterations=4,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        platform="cli",
    )
    try:
        result = agent.run_conversation(
            "reply with ok",
            conversation_history=[],
            task_id="auto-routing-foundation-e2e",
        )
        assert result["final_response"].strip() == "ok"
        request = _ProviderHandler.requests[-1]
        return (agent.provider, agent.model), str(request["model"])
    finally:
        agent.close()


def _run_registered_cli(
    manager,
    *arguments: str,
) -> tuple[int, dict[str, Any]]:
    command = manager._cli_commands["auto-routing"]
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    args = parser.parse_args(list(arguments))
    output = io.StringIO()
    exit_code = 0
    with redirect_stdout(output):
        try:
            command["handler_fn"](args)
        except SystemExit as error:
            exit_code = int(error.code)
    return exit_code, json.loads(output.getvalue())


def test_foundation_never_changes_agent_runtime(
    isolated_home: Path,
    fake_provider,
    request: pytest.FixtureRequest,
) -> None:
    config_path = isolated_home / "config.yaml"
    config_path.write_text(
        "model:\n  context_length: 65536\n"
        "plugins:\n  enabled:\n    - auto-routing\n",
        encoding="utf-8",
    )
    _clear_hermes_path_and_config_caches()
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins["auto-routing"]
    assert loaded.enabled is True
    assert loaded.error is None
    assert "auto-routing" in manager._cli_commands
    assert "auto-routing:auto-routing" in manager._plugin_skills
    assert loaded.tools_registered == []
    assert loaded.hooks_registered == ["pre_api_request", "post_turn_outcome"]
    assert loaded.middleware_registered == []
    assert loaded.runtime_resolver_registered is True
    registered_resolver = manager.agent_runtime_resolver
    assert registered_resolver is not None
    request.addfinalizer(registered_resolver.close)

    base_url, handler = fake_provider
    before = _run_real_agent_once(base_url)
    requests_before_setup = len(handler.requests)
    proposal_file = isolated_home / "approved-proposal.json"
    fixture = Path(__file__).with_name("fixtures") / "approved_proposal.json"
    proposal_file.write_bytes(fixture.read_bytes())
    preview_code, preview = _run_registered_cli(
        manager,
        "setup",
        "--proposal",
        str(proposal_file),
        "--json",
    )
    assert preview_code == 0
    apply_code, applied = _run_registered_cli(
        manager,
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    assert apply_code == 0
    assert applied["activation"]["mode"] == "shadow"
    assert len(handler.requests) == requests_before_setup

    after = _run_real_agent_once(base_url)
    assert before == after == (("openai-compat", "test-model"), "test-model")
    chat_requests = [request for request in handler.requests if "messages" in request]
    assert len(chat_requests) == 2
    with RoutingStore.open(home=isolated_home) as store:
        assert store.count_decisions() == 0


def test_verify_runtime_preview_and_apply_survive_true_process_boundary(
    isolated_home: Path,
) -> None:
    secret = "subprocess-secret-must-never-appear"
    proposal = json.loads(
        (Path(__file__).with_name("fixtures") / "approved_proposal.json").read_text(
            encoding="utf-8"
        )
    )
    proposal["policy"]["allow_paid_access_probes"] = True
    config_path = isolated_home / "config.yaml"
    config_path.write_text(
        json.dumps({"plugins": {"entries": {"auto-routing": proposal}}}),
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    runtime_id = RuntimeKey(
        provider="subprocess-provider",
        model="subprocess-model",
        auth_identity="api-key:subprocess",
        credential_pool_identity="pool:subprocess",
        endpoint_identity="endpoint:subprocess",
        api_mode="chat_completions",
        local_backend="",
        inventory_revision="subprocess-inventory",
    ).stable_id()
    harness = Path(__file__).with_name("verify_cli_subprocess.py")
    environment = os.environ.copy()
    environment.update(
        {
            "HERMES_HOME": str(isolated_home),
            "AUTO_ROUTING_SUBPROCESS_SECRET": secret,
            "PYTHONUTF8": "1",
        }
    )

    preview = subprocess.run(
        [
            sys.executable,
            str(harness),
            "verify-runtime",
            runtime_id,
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert preview.returncode == 0, (preview.stdout, preview.stderr)
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["applied"] is False
    assert secret not in preview.stdout
    assert secret not in preview.stderr

    rotated_secret = f"{secret}-rotated"
    drifted_environment = {
        **environment,
        "AUTO_ROUTING_SUBPROCESS_SECRET": rotated_secret,
    }
    drifted = subprocess.run(
        [
            sys.executable,
            str(harness),
            "verify-runtime",
            runtime_id,
            "--apply",
            "--expect-hash",
            preview_payload["precondition_hash"],
            "--ack-billable",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=drifted_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert drifted.returncode == 2
    assert json.loads(drifted.stdout)["error"] == (
        "resolved runtime precondition changed"
    )
    assert secret not in drifted.stdout
    assert rotated_secret not in drifted.stdout
    assert rotated_secret not in drifted.stderr

    applied = subprocess.run(
        [
            sys.executable,
            str(harness),
            "verify-runtime",
            runtime_id,
            "--apply",
            "--expect-hash",
            preview_payload["precondition_hash"],
            "--ack-billable",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert applied.returncode == 0, (applied.stdout, applied.stderr)
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["applied"] is True
    assert applied_payload["state"] == "verified"
    assert secret not in applied.stdout
    assert secret not in applied.stderr
    assert config_path.read_bytes() == config_before

    key_path = isolated_home / "auto-routing" / "credential-selection.key"
    assert key_path.read_bytes() != secret.encode("utf-8")
    secret_bytes = secret.encode("utf-8")
    for path in (
        key_path,
        isolated_home / "auto-routing" / "state.db",
        isolated_home / "auto-routing" / "state.db-wal",
        isolated_home / "auto-routing" / "state.db-shm",
    ):
        if path.exists():
            assert secret_bytes not in path.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_profile_credential_fingerprint_key_creation_is_process_safe_and_isolated(
    isolated_home: Path,
) -> None:
    config_path = isolated_home / "config.yaml"
    config_path.write_text("plugins: {}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update({"HERMES_HOME": str(isolated_home), "PYTHONUTF8": "1"})
    worker = (
        "from plugins.auto_routing.auto_routing.profile_key import "
        "ensure_profile_credential_fingerprint_key as ensure; "
        "assert len(ensure()) == 32"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker],
            cwd=Path(__file__).resolve().parents[3],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(2)
    ]
    results = [process.communicate(timeout=30) for process in processes]
    for process, (stdout, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, (stdout, stderr)
        assert stdout == ""

    key_path = isolated_home / "auto-routing" / "credential-selection.key"
    first_key = key_path.read_bytes()
    assert len(first_key) == 32
    assert not list(key_path.parent.glob(f".{key_path.name}.*.tmp"))
    if os.name == "posix":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    other_home = isolated_home.parent / "other-profile"
    other_home.mkdir()
    other_config = other_home / "config.yaml"
    other_config.write_text("plugins: {}\n", encoding="utf-8")
    second_key = ensure_profile_credential_fingerprint_key(
        other_home,
        config_path=other_config,
    )
    assert first_key != second_key
    assert read_profile_credential_fingerprint_key_if_present(other_home) == second_key


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_profile_credential_fingerprint_key_rejects_broad_permissions(
    isolated_home: Path,
) -> None:
    config_path = isolated_home / "config.yaml"
    config_path.write_text("plugins: {}\n", encoding="utf-8")
    ensure_profile_credential_fingerprint_key(
        isolated_home,
        config_path=config_path,
    )
    key_path = isolated_home / "auto-routing" / "credential-selection.key"
    key_path.chmod(0o640)

    with pytest.raises(ProfileKeyError, match="owner-only permissions"):
        read_profile_credential_fingerprint_key_if_present(isolated_home)
