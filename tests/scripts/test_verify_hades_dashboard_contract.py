"""Contract tests for the Hades dashboard/TUI sync verifier.

These tests intentionally use small source fixtures so each contract failure is
isolated and the verifier remains a semantic, bounded check rather than a full
TypeScript/Python parser.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.verify_hades_dashboard_contract as verifier
from scripts.verify_hades_dashboard_contract import verify


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_hades_dashboard_contract.py"

VALID_API = r'''\
const apiGet = <T>(url: string) => fetchJSON<T>(url);
export const api = {
  getAutonomyStatus: () => apiGet("/api/autonomy/status"),
  getAutonomyRules: () => apiGet(`/api/autonomy/rules${qs ? `?${qs}` : ""}`),
  explainAutonomyRule: (ruleId: string) => apiGet(`/api/autonomy/rules/${encodeURIComponent(ruleId)}`),
  previewAutonomyChange: () => fetchJSON("/api/autonomy/preview", { method: "POST" }),
  applyAutonomyPreview: () => fetchJSON("/api/autonomy/apply", { method: "POST" }),
  acceptAutonomySuggestion: (suggestionId: string) => fetchJSON(`/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/accept`, { method: "POST" }),
  rejectAutonomySuggestion: (suggestionId: string) => fetchJSON(`/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/reject`, { method: "POST" }),
  getAutonomyMandates: () => apiGet(`/api/autonomy/mandates${state ? `?state=${encodeURIComponent(state)}` : ""}`),
  revokeAutonomyMandate: (ruleId: string) => fetchJSON(`/api/autonomy/mandates/${encodeURIComponent(ruleId)}/revoke`, { method: "POST" }),
  getAutonomyAudit: () => apiGet(`/api/autonomy/audit?limit=${limit}${verdict ? `&verdict=${encodeURIComponent(verdict)}` : ""}`),
  getReceipts: () => apiGet(`/api/receipts${qs ? `?${qs}` : ""}`),
  getReceipt: (receiptId: string) => apiGet(`/api/receipts/${encodeURIComponent(receiptId)}`),
  getReceiptObservations: (receiptId: string) => apiGet(`/api/receipts/${encodeURIComponent(receiptId)}/observations`),
};
'''

VALID_SERVER = '''\
from gateway import method

_LONG_HANDLERS = frozenset(
    {
        "autonomy.exec",
        "receipt.exec",
        "transaction.exec",
    }
)

@method("autonomy.exec")
def autonomy_exec(rid, params):
    return {"result": {}}

@method("receipt.exec")
def receipt_exec(rid, params):
    return {"result": {}}

@method("transaction.exec")
def transaction_exec(rid, params):
    return {"result": {}}
'''

VALID_TYPES = '''\
export interface AutonomyExecResponse { ok: boolean }
export interface ReceiptExecResponse { ok: boolean }
export interface TransactionExecResponse { ok: boolean }
'''

REQUIRED_RPC_TESTS = (
    "tests/tui_gateway/test_autonomy_rpc.py",
    "tests/tui_gateway/test_receipt_rpc.py",
    # Keep this explicit: transaction RPC verification has a same-sync dependency.
    "tests/tui_gateway/test_transaction_rpc.py",
)

VALID_PROMPT = (
    "## Integration Manifest + Handler Verification\n"
    "```bash\n"
    "cd ~/.hermes/hermes-agent\n"
    "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
    "./venv/bin/python3 scripts/post-sync-verify.py --fix\n"
    "./venv/bin/python3 scripts/post-sync-verify.py\n"
    "```\n"
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_fixture(tmp_path: Path) -> tuple[Path, Path]:
    _write(tmp_path, "web/src/lib/api.ts", VALID_API)
    _write(tmp_path, "tui_gateway/server.py", VALID_SERVER)
    _write(tmp_path, "ui-tui/src/gatewayTypes.ts", VALID_TYPES)
    for relative in REQUIRED_RPC_TESTS:
        _write(tmp_path, relative, "def test_contract_fixture():\n    assert True\n")
    cron_jobs = tmp_path / "jobs.json"
    cron_jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "integration",
                        "name": "hades-fork-integration",
                        "prompt": VALID_PROMPT,
                        "skills": ["github-operations"],
                        "schedule": {"kind": "cron", "expr": "0 */4 * * *"},
                        "schedule_display": "0 */4 * * *",
                        "enabled": True,
                        "state": "scheduled",
                        "deliver": "local",
                    },
                    {"id": "other", "name": "other-job", "prompt": "noop"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return tmp_path, cron_jobs


def _messages(root: Path, cron_jobs: Path) -> str:
    return "\n".join(verify(root, cron_jobs))


def test_fully_valid_minimal_fixture_has_no_failures(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)

    assert verify(root, cron_jobs) == []


@pytest.mark.parametrize(
    ("relative", "content", "needle"),
    [
        ("web/src/lib/api.ts", "", "api.ts"),
        ("web/src/lib/api.ts", VALID_API.replace("getAutonomyStatus", "missingStatus"), "getAutonomyStatus"),
        ("web/src/lib/api.ts", VALID_API.replace('method: "POST"', 'method: "GET"', 1), "POST"),
        ("web/src/lib/api.ts", VALID_API.replace("/api/autonomy/status", "/api/autonomy/wrong", 1), "route"),
    ],
)
def test_api_contract_reports_missing_file_method_verb_or_route(
    tmp_path: Path, relative: str, content: str, needle: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / relative).write_text(content, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "api" in failures.lower()
    assert needle.lower() in failures.lower()


def test_api_duplicate_method_is_rejected(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    duplicate = '  getAutonomyStatus: () => apiGet("/api/autonomy/status"),\n'
    (root / "web/src/lib/api.ts").write_text(
        VALID_API.replace("};\n", duplicate + "};\n", 1),
        encoding="utf-8",
    )

    failures = _messages(root, cron_jobs)

    assert "getAutonomyStatus" in failures
    assert "duplicate" in failures.lower()


def test_static_route_near_miss_is_rejected(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / "web/src/lib/api.ts").write_text(
        VALID_API.replace("/api/autonomy/status", "/api/autonomy/status-v2", 1),
        encoding="utf-8",
    )

    failures = _messages(root, cron_jobs)

    assert "getAutonomyStatus" in failures
    assert "route" in failures.lower()


@pytest.mark.parametrize(
    ("method", "valid_route", "invalid_route"),
    [
        (
            "getAutonomyRules",
            r'`/api/autonomy/rules${qs ? `?${qs}` : ""}`',
            r'`/api/autonomy/rules${qs ? `?${qs}` : ""}/extra`',
        ),
        (
            "getAutonomyMandates",
            r'`/api/autonomy/mandates${state ? `?state=${encodeURIComponent(state)}` : ""}`',
            r'`/api/autonomy/mandates${state ? `?state=${encodeURIComponent(state)}` : ""}/extra`',
        ),
        (
            "getReceipts",
            r'`/api/receipts${qs ? `?${qs}` : ""}`',
            r'`/api/receipts${qs ? `?${qs}` : ""}/extra`',
        ),
        (
            "getAutonomyAudit",
            r'`/api/autonomy/audit?limit=${limit}${verdict ? `&verdict=${encodeURIComponent(verdict)}` : ""}`',
            r'`/api/autonomy/audit?limit=${limit}${verdict ? `&verdict=${encodeURIComponent(verdict)}` : ""}/extra`',
        ),
    ],
)
def test_query_route_requires_complete_outer_template_expression(
    tmp_path: Path, method: str, valid_route: str, invalid_route: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(valid_route, invalid_route, 1)
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert method in failures
    assert "route" in failures.lower()


def test_static_route_marker_unrelated_to_transport_call_does_not_satisfy_contract(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'getAutonomyStatus: () => apiGet("/api/autonomy/status"),',
        'getAutonomyStatus: () => { const marker = "/api/autonomy/status"; '
        'return fetchJSON("/api/autonomy/wrong"); },',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "getAutonomyStatus" in failures
    assert "route" in failures.lower()


def test_dynamic_route_marker_unrelated_to_transport_call_does_not_satisfy_contract(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'explainAutonomyRule: (ruleId: string) => '
        'apiGet(`/api/autonomy/rules/${encodeURIComponent(ruleId)}`),',
        'explainAutonomyRule: (ruleId: string) => { '
        'const marker = `/api/autonomy/rules/${encodeURIComponent(ruleId)}`; '
        'return fetchJSON(`/api/autonomy/rules/${encodeURIComponent(ruleId)}/wrong`); },',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "explainAutonomyRule" in failures
    assert "route" in failures.lower()


def test_post_marker_unrelated_to_transport_call_does_not_satisfy_verb_contract(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'previewAutonomyChange: () => fetchJSON("/api/autonomy/preview", '
        '{ method: "POST" }),',
        'previewAutonomyChange: () => { const marker = \'method: "POST"\'; '
        'return fetchJSON("/api/autonomy/preview"); },',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "previewAutonomyChange" in failures
    assert "verb" in failures.lower()


def test_nested_canonical_post_and_direct_wrong_route_cannot_satisfy_contract(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'previewAutonomyChange: () => fetchJSON("/api/autonomy/preview", { method: "POST" }),',
        'previewAutonomyChange: () => { '
        'const nested = () => fetchJSON("/api/autonomy/preview", { method: "POST" }); '
        'return fetchJSON("/api/autonomy/wrong", { method: "POST" }); },',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "previewAutonomyChange" in failures
    assert "exactly one direct" in failures.lower()


def test_duplicate_post_method_properties_are_rejected(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'previewAutonomyChange: () => fetchJSON("/api/autonomy/preview", { method: "POST" }),',
        'previewAutonomyChange: () => fetchJSON('
        '"/api/autonomy/preview", { method: "POST", method: "GET" }),',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "previewAutonomyChange" in failures
    assert "duplicate" in failures.lower()


@pytest.mark.parametrize(
    "options",
    (
        "requestOptions",
        "{ ...requestOptions, method: \"POST\" }",
        "{ method }",
    ),
)
def test_post_options_must_be_a_static_object_with_literal_method(
    tmp_path: Path, options: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'previewAutonomyChange: () => fetchJSON("/api/autonomy/preview", { method: "POST" }),',
        f'previewAutonomyChange: () => fetchJSON("/api/autonomy/preview", {options}),',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "previewAutonomyChange" in failures
    assert "post" in failures.lower()


def test_get_matching_route_rejects_dynamic_request_options_argument(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'getAutonomyStatus: () => apiGet("/api/autonomy/status"),',
        'getAutonomyStatus: () => apiGet("/api/autonomy/status", requestOptions),',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "getAutonomyStatus" in failures
    assert "exactly one argument" in failures.lower()


def test_two_direct_transport_calls_fail_even_when_one_is_canonical(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'getAutonomyStatus: () => apiGet("/api/autonomy/status"),',
        'getAutonomyStatus: () => { '
        'apiGet("/api/autonomy/status"); '
        'return apiGet("/api/autonomy/status"); },',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "getAutonomyStatus" in failures
    assert "exactly one direct" in failures.lower()


def test_malformed_repeated_transport_tokens_are_scanned_once_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    repeated_tokens = " ".join("fetchJSON(" for _ in range(6000))
    source = VALID_API.replace(
        'getAutonomyStatus: () => apiGet("/api/autonomy/status"),',
        f'getAutonomyStatus: () => {{ {repeated_tokens} }},',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    original = verifier._extract_call_arguments
    malformed_attempts = 0

    def counted_extract(text: str, opening: int):
        nonlocal malformed_attempts
        result = original(text, opening)
        if text.count("fetchJSON(") > 1000:
            malformed_attempts += 1
        return result

    monkeypatch.setattr(verifier, "_extract_call_arguments", counted_extract)
    failures = _messages(root, cron_jobs)

    assert malformed_attempts <= 1
    assert "getAutonomyStatus" in failures
    assert "malformed" in failures.lower()


def test_repo_asset_final_symlink_is_rejected_with_safety_diagnostic(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-api.ts"
    outside.write_text(VALID_API, encoding="utf-8")
    api_path = root / "web/src/lib/api.ts"
    api_path.unlink()
    api_path.symlink_to(outside)

    failures = _messages(root, cron_jobs)

    assert "api" in failures.lower()
    assert "symlink" in failures.lower() or "root" in failures.lower()


def test_repo_asset_symlink_for_second_contract_file_is_rejected(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-server.py"
    outside.write_text(VALID_SERVER, encoding="utf-8")
    server_path = root / "tui_gateway/server.py"
    server_path.unlink()
    server_path.symlink_to(outside)

    failures = _messages(root, cron_jobs)

    assert "server" in failures.lower()
    assert "symlink" in failures.lower() or "root" in failures.lower()


@pytest.mark.parametrize("comment", ('// "/api/autonomy/status"', '/* "/api/autonomy/status" */'))
def test_static_route_in_comment_does_not_satisfy_method_contract(
    tmp_path: Path, comment: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        'getAutonomyStatus: () => apiGet("/api/autonomy/status"),',
        f'getAutonomyStatus: () => apiGet("/api/autonomy/wrong"), {comment}',
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "getAutonomyStatus" in failures
    assert "route" in failures.lower()


@pytest.mark.parametrize(
    ("method", "route"),
    [
        (
            "acceptAutonomySuggestion",
            "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}",
        ),
        (
            "rejectAutonomySuggestion",
            "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}",
        ),
        (
            "revokeAutonomyMandate",
            "/api/autonomy/mandates/${encodeURIComponent(ruleId)}",
        ),
        (
            "getReceiptObservations",
            "/api/receipts/${encodeURIComponent(receiptId)}",
        ),
    ],
)
def test_dynamic_route_without_required_suffix_is_rejected(
    tmp_path: Path, method: str, route: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    api_path = root / "web/src/lib/api.ts"
    source = api_path.read_text(encoding="utf-8")
    if method == "acceptAutonomySuggestion":
        source = source.replace(
            "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/accept", route, 1
        )
    elif method == "rejectAutonomySuggestion":
        source = source.replace(
            "/api/autonomy/suggestions/${encodeURIComponent(suggestionId)}/reject", route, 1
        )
    elif method == "revokeAutonomyMandate":
        source = source.replace(
            "/api/autonomy/mandates/${encodeURIComponent(ruleId)}/revoke", route, 1
        )
    else:
        source = source.replace(
            "/api/receipts/${encodeURIComponent(receiptId)}/observations", route, 1
        )
    api_path.write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert method in failures
    assert "route" in failures.lower()


def test_dynamic_route_in_comment_does_not_satisfy_method_contract(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_API.replace(
        "`/api/receipts/${encodeURIComponent(receiptId)}/observations`",
        "`/api/receipts/${encodeURIComponent(receiptId)}/wrong` // "
        "`/api/receipts/${encodeURIComponent(receiptId)}/observations`",
        1,
    )
    (root / "web/src/lib/api.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "getReceiptObservations" in failures
    assert "route" in failures.lower()


@pytest.mark.parametrize("relative", REQUIRED_RPC_TESTS)
def test_each_required_rpc_test_file_is_required(tmp_path: Path, relative: str) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / relative).unlink()

    failures = _messages(root, cron_jobs)

    assert "rpc" in failures.lower()
    assert relative in failures


def test_empty_required_rpc_test_file_is_rejected(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / REQUIRED_RPC_TESTS[2]).write_text("", encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert REQUIRED_RPC_TESTS[2] in failures
    assert "empty" in failures.lower()


@pytest.mark.parametrize(
    ("mutator", "needles"),
    [
        (
            lambda text: text.replace('@method("autonomy.exec")\n', "", 1),
            ("server", "autonomy.exec"),
        ),
        (
            lambda text: text.replace(
                '@method("receipt.exec")\n',
                '@method("receipt.exec")\n@method("receipt.exec")\n',
                1,
            ),
            ("server", "duplicate"),
        ),
        (
            lambda text: text.replace('        "transaction.exec",\n', "", 1),
            ("long", "transaction.exec"),
        ),
    ],
)
def test_server_handler_and_long_handler_contracts(
    tmp_path: Path, mutator, needles: tuple[str, ...]
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / "tui_gateway/server.py").write_text(mutator(VALID_SERVER), encoding="utf-8")

    failures = _messages(root, cron_jobs)

    for needle in needles:
        assert needle.lower() in failures.lower()


def test_commented_handler_registration_does_not_satisfy_server_contract(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_SERVER.replace(
        '@method("autonomy.exec")\ndef autonomy_exec',
        '# @method("autonomy.exec")\ndef autonomy_exec',
        1,
    )
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "autonomy.exec" in failures
    assert "registration" in failures.lower()


def test_commented_duplicate_handler_registration_does_not_count_as_duplicate(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_SERVER.replace(
        '@method("receipt.exec")\ndef receipt_exec',
        '@method("receipt.exec")\n# @method("receipt.exec")\ndef receipt_exec',
        1,
    )
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    assert verify(root, cron_jobs) == []


def test_commented_long_handler_does_not_satisfy_membership(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_SERVER.replace(
        '        "transaction.exec",',
        '        # "transaction.exec",',
        1,
    )
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "transaction.exec" in failures
    assert "long" in failures.lower()


def test_commented_duplicate_long_handler_does_not_count_as_duplicate(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_SERVER.replace(
        '        "receipt.exec",',
        '        "receipt.exec",\n        # "receipt.exec",',
        1,
    )
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    assert verify(root, cron_jobs) == []


def test_handler_registration_alone_cannot_satisfy_long_handler_check(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    server_without_long_block = VALID_SERVER.split("_LONG_HANDLERS", 1)[0] + "\n"
    (root / "tui_gateway/server.py").write_text(server_without_long_block, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "long" in failures.lower()
    assert "_LONG_HANDLERS" in failures


def test_decorated_handler_under_false_branch_does_not_count_as_registration(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    live = (
        '@method("autonomy.exec")\n'
        "def autonomy_exec(rid, params):\n"
        '    return {"result": {}}\n\n'
    )
    dead = (
        "if False:\n"
        '    @method("autonomy.exec")\n'
        "    def autonomy_exec(rid, params):\n"
        '        return {"result": {}}\n'
    )
    source = VALID_SERVER.replace(live, "", 1) + "\n" + dead
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "autonomy.exec" in failures
    assert "registration" in failures.lower()


def test_long_handlers_false_conditional_branch_does_not_count_membership(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    old_assignment = """_LONG_HANDLERS = frozenset(
    {
        "autonomy.exec",
        "receipt.exec",
        "transaction.exec",
    }
)"""
    new_assignment = (
        '_LONG_HANDLERS = ({"autonomy.exec"} if False else '
        '{"receipt.exec", "transaction.exec"})'
    )
    source = VALID_SERVER.replace(old_assignment, new_assignment, 1)
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "autonomy.exec" in failures
    assert "long" in failures.lower()


def test_long_handlers_constant_true_branch_and_frozenset_are_inspectable(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    old_assignment = """_LONG_HANDLERS = frozenset(
    {
        "autonomy.exec",
        "receipt.exec",
        "transaction.exec",
    }
)"""
    new_assignment = (
        '_LONG_HANDLERS = frozenset(\n'
        '    ({"autonomy.exec", "receipt.exec", "transaction.exec"} '
        'if True else set())\n'
        ')'
    )
    source = VALID_SERVER.replace(old_assignment, new_assignment, 1)
    (root / "tui_gateway/server.py").write_text(source, encoding="utf-8")

    assert verify(root, cron_jobs) == []


@pytest.mark.parametrize("name", ("AutonomyExecResponse", "ReceiptExecResponse", "TransactionExecResponse"))
def test_each_ts_response_export_is_required(tmp_path: Path, name: str) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / "ui-tui/src/gatewayTypes.ts").write_text(
        VALID_TYPES.replace(f"export interface {name}", f"export interface Missing{name}"),
        encoding="utf-8",
    )

    failures = _messages(root, cron_jobs)

    assert "types" in failures.lower()
    assert name in failures


def test_duplicate_ts_response_export_is_rejected(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / "ui-tui/src/gatewayTypes.ts").write_text(
        VALID_TYPES + "export type ReceiptExecResponse = ReceiptExecResponse;\n",
        encoding="utf-8",
    )

    failures = _messages(root, cron_jobs)

    assert "ReceiptExecResponse" in failures
    assert "duplicate" in failures.lower()


def test_response_export_inside_multiline_block_comment_does_not_count(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_TYPES.replace(
        "export interface AutonomyExecResponse { ok: boolean }",
        "/*\nexport interface AutonomyExecResponse { ok: boolean }\n*/",
        1,
    )
    (root / "ui-tui/src/gatewayTypes.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "AutonomyExecResponse" in failures
    assert "missing" in failures.lower()


def test_response_export_inside_multiline_template_does_not_count(
    tmp_path: Path,
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    source = VALID_TYPES.replace(
        "export interface AutonomyExecResponse { ok: boolean }",
        "const fakeTypes = `\n"
        "export interface AutonomyExecResponse { ok: boolean }\n"
        "`;",
        1,
    )
    (root / "ui-tui/src/gatewayTypes.ts").write_text(source, encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "AutonomyExecResponse" in failures
    assert "missing" in failures.lower()


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        {"jobs": "not-a-list"},
        {"not_jobs": []},
    ],
)
def test_invalid_cron_manifest_is_rejected(tmp_path: Path, payload) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    cron_jobs.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "cron" in failures.lower()


@pytest.mark.parametrize(
    "mutation",
    (
        "disabled",
        "paused",
        "missing_schedule",
        "wrong_schedule_kind",
        "wrong_schedule_expr",
        "wrong_schedule_display",
        "missing_skill",
        "wrong_delivery",
    ),
)
def test_cron_operational_invariants_are_required(tmp_path: Path, mutation: str) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    payload = json.loads(cron_jobs.read_text(encoding="utf-8"))
    job = payload["jobs"][0]
    if mutation == "disabled":
        job["enabled"] = False
    elif mutation == "paused":
        job["state"] = "paused"
    elif mutation == "missing_schedule":
        del job["schedule"]
    elif mutation == "wrong_schedule_kind":
        job["schedule"]["kind"] = "interval"
    elif mutation == "wrong_schedule_expr":
        job["schedule"]["expr"] = "0 * * * *"
    elif mutation == "wrong_schedule_display":
        job["schedule_display"] = "0 * * * *"
    elif mutation == "missing_skill":
        job["skills"] = []
    elif mutation == "wrong_delivery":
        job["deliver"] = "origin"
    cron_jobs.write_text(json.dumps(payload), encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "cron" in failures.lower()
    assert {
        "disabled": "enabled",
        "paused": "scheduled",
        "missing_schedule": "schedule",
        "wrong_schedule_kind": "schedule",
        "wrong_schedule_expr": "schedule",
        "wrong_schedule_display": "schedule",
        "missing_skill": "github-operations",
        "wrong_delivery": "deliver",
    }[mutation].lower() in failures.lower()


@pytest.mark.parametrize(
    "prompt",
    (
        (
            "## Integration Manifest + Handler Verification\n"
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
            "./venv/bin/python3 scripts/post-sync-verify.py --fix\n"
        ),
        (
            "## Integration Manifest + Handler Verification\n"
            "```python\n"
            "cd ~/.hermes/hermes-agent\n"
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
            "```\n"
            "scripts/post-sync-verify.py\n"
        ),
    ),
)
def test_cron_verifier_command_must_be_in_shell_fenced_block(
    tmp_path: Path, prompt: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    payload = json.loads(cron_jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["prompt"] = prompt
    cron_jobs.write_text(json.dumps(payload), encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "cron" in failures.lower()
    assert "fenced" in failures.lower() or "shell" in failures.lower()


@pytest.mark.parametrize(
    "prompt",
    (
        (
            "## Integration Manifest + Handler Verification\n"
            "```bash\n"
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
            "./venv/bin/python3 scripts/post-sync-verify.py --fix\n"
            "```\n"
        ),
        (
            "## Integration Manifest + Handler Verification\n"
            "```bash\n"
            "cd ~/.hermes/hermes-agent\n"
            "./venv/bin/python3 scripts/post-sync-verify.py --fix\n"
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n"
            "```\n"
        ),
    ),
)
def test_cron_verifier_command_requires_repo_cd_before_post_sync(
    tmp_path: Path, prompt: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    payload = json.loads(cron_jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["prompt"] = prompt
    cron_jobs.write_text(json.dumps(payload), encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "cron" in failures.lower()
    assert "cd" in failures.lower() or "before" in failures.lower()


@pytest.mark.parametrize(
    ("jobs", "needle"),
    [
        ([{"name": "other", "prompt": VALID_PROMPT}], "integration"),
        (
            [
                {"name": "hades-fork-integration", "prompt": VALID_PROMPT},
                {"name": "hades-fork-integration", "prompt": VALID_PROMPT},
            ],
            "exactly one",
        ),
    ],
)
def test_cron_integration_job_must_exist_exactly_once(
    tmp_path: Path, jobs: list[dict], needle: str
) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    cron_jobs.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    failures = _messages(root, cron_jobs)

    assert "cron" in failures.lower()
    assert needle.lower() in failures.lower()


@pytest.mark.parametrize(
    "prompt",
    [
        VALID_PROMPT.replace("## Integration Manifest + Handler Verification", "## Wrong Heading"),
        VALID_PROMPT.replace(
            "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py",
            "./venv/bin/python3 scripts/other.py",
        ),
        "scripts/post-sync-verify.py\n## Integration Manifest + Handler Verification\n"
        "./venv/bin/python3 scripts/verify_hades_dashboard_contract.py\n",
    ],
)
def test_cron_heading_command_and_order_are_verified(tmp_path: Path, prompt: str) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    cron_jobs.write_text(
        json.dumps({"jobs": [{"name": "hades-fork-integration", "prompt": prompt}]}),
        encoding="utf-8",
    )

    failures = _messages(root, cron_jobs)

    assert "cron" in failures.lower()
    assert any(term in failures.lower() for term in ("heading", "command", "before", "order"))


def test_cli_failure_is_exit_one_with_actionable_category_and_path(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)
    (root / "web/src/lib/api.ts").write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(root),
            "--cron-jobs",
            str(cron_jobs),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    output = completed.stdout + completed.stderr
    assert "FAIL" in output
    assert "api" in output.lower()
    assert "web/src/lib/api.ts" in output
    assert "-" in output


def test_actual_repo_assets_pass_with_valid_cron_fixture(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cron_jobs = tmp_path / "jobs.json"
    cron_jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "hades-fork-integration",
                        "prompt": VALID_PROMPT,
                        "skills": ["github-operations"],
                        "schedule": {"kind": "cron", "expr": "0 */4 * * *"},
                        "schedule_display": "0 */4 * * *",
                        "enabled": True,
                        "state": "scheduled",
                        "deliver": "local",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert verify(repo_root, cron_jobs) == []


def test_cli_passes_for_valid_fixture(tmp_path: Path) -> None:
    root, cron_jobs = _make_fixture(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(root),
            "--cron-jobs",
            str(cron_jobs),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "PASS" in completed.stdout
    assert completed.stderr == ""
