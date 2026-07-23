"""Runtime contracts for Hades' canonical durable conversation state."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hades_state import SessionDB
from run_agent import AIAgent


@pytest.fixture()
def db(tmp_path):
    state = SessionDB(db_path=tmp_path / "state.db")
    yield state
    state.close()


def test_api_content_reconciles_persists_and_replays_verbatim(db):
    db.create_session("s", "cli")
    sent = "  question\n\n<memory-context>cached</memory-context>\n"
    db.append_message("s", "user", "question", api_content=sent)
    assert db.get_messages_as_conversation("s")[0]["api_content"] == sent
    db.replace_messages(
        "s",
        [
            {"role": "user", "content": "replacement", "api_content": "replacement\ud800ctx"},
            {"role": "assistant", "content": "answer"},
        ],
    )

    rows = db.get_messages("s")
    conversation = db.get_messages_as_conversation("s")

    assert rows[0]["api_content"] == "replacement�ctx"
    assert conversation[0]["content"] == "replacement"
    assert conversation[0]["api_content"] == "replacement�ctx"
    assert "api_content" not in conversation[1]


def test_api_content_replays_the_previous_provider_bytes_on_the_next_turn(db):
    """A resumed turn sends the first turn's stored wire content verbatim."""
    session_id = "cache-round-trip"
    db.create_session(session_id, "cli")
    provider = MagicMock()
    provider.chat.completions.create.side_effect = lambda **_kwargs: SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant", content="done", tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        model="gpt-4o-mini",
        usage=None,
    )

    def make_agent():
        agent = AIAgent(
            api_key="test-key",
            base_url="http://provider.invalid/v1",
            provider="openai",
            model="gpt-4o-mini",
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=session_id,
        )
        agent.client = provider
        return agent

    sent = "first question\n<memory-context>cached</memory-context>"
    make_agent().run_conversation(sent, conversation_history=[], task_id="turn-1")

    first_request = provider.chat.completions.create.call_args.kwargs["messages"]
    first_wire_content = next(
        message["content"] for message in first_request if message["role"] == "user"
    )
    history = db.get_messages_as_conversation(session_id)
    assert history[0]["api_content"] == first_wire_content

    provider.reset_mock(side_effect=False)
    make_agent().run_conversation("second question", conversation_history=history, task_id="turn-2")

    second_request = provider.chat.completions.create.call_args.kwargs["messages"]
    replayed_content = next(
        message["content"] for message in second_request if message["role"] == "user"
    )
    assert replayed_content == first_wire_content


def test_api_content_backfill_targets_only_the_matching_newest_active_user(db):
    db.create_session("s", "cli")
    db.append_message("s", "user", "older")
    db.append_message("s", "assistant", "answer")
    db.append_message("s", "user", "latest")

    assert db.set_latest_user_api_content("s", "older", "wrong target") == 0
    assert db.set_latest_user_api_content("s", "latest", "latest\nctx") == 1

    users = [row for row in db.get_messages("s") if row["role"] == "user"]
    assert [row["api_content"] for row in users] == [None, "latest\nctx"]


def test_existing_database_reconciles_nullable_api_content_sidecar(tmp_path):
    path = tmp_path / "legacy.db"
    current = SessionDB(db_path=path)
    current.create_session("s", "cli")
    current.close()

    legacy = sqlite3.connect(path)
    legacy.execute("ALTER TABLE messages DROP COLUMN api_content")
    legacy.commit()
    legacy.close()

    db = SessionDB(db_path=path)
    try:
        db.append_message("s", "user", "clean", api_content="wire bytes")
        assert db.get_messages_as_conversation("s")[-1]["api_content"] == "wire bytes"
    finally:
        db.close()


def test_resume_projections_keep_tip_model_history_and_ancestor_display_prefix(db):
    db.create_session("root", "tui")
    db.append_message("root", "user", "ancestor question")
    db.append_message("root", "assistant", "ancestor answer")
    db.create_session("tip", "tui", parent_session_id="root")
    db.append_message("tip", "user", "tip question")
    db.append_message("tip", "assistant", "candidate", finish_reason="verification_required")
    db.append_message("tip", "assistant", "tip answer")

    model_history, display_history = db.get_resume_conversations("tip")
    prefix = db.get_ancestor_display_prefix("tip")

    assert [message["content"] for message in model_history] == ["tip question", "tip answer"]
    assert [message["content"] for message in display_history] == [
        "ancestor question", "ancestor answer", "tip question", "candidate", "tip answer",
    ]
    assert [message["content"] for message in prefix] == ["ancestor question", "ancestor answer"]


def test_children_inherit_workspace_and_only_compression_children_inherit_origin(db):
    db.create_session(
        "parent", "telegram", user_id="u", session_key="telegram:u:c",
        chat_id="c", chat_type="private", thread_id="thread",
    )
    db.update_session_cwd(
        "parent", "/repo", git_branch="feature/state", git_repo_root="/repo",
    )
    db.record_gateway_session_peer(
        "parent", source="telegram", user_id="u", session_key="telegram:u:c",
        chat_id="c", chat_type="private", thread_id="thread",
        display_name="Chat", origin_json='{"platform":"telegram"}',
    )
    db.create_session("delegate", "telegram", parent_session_id="parent")
    db.end_session("parent", "compression")
    db.create_session("rotated", "telegram", parent_session_id="parent")

    delegate = db.get_session("delegate")
    rotated = db.get_session("rotated")
    assert {key: delegate[key] for key in ("cwd", "git_branch", "git_repo_root")} == {
        "cwd": "/repo", "git_branch": "feature/state", "git_repo_root": "/repo",
    }
    assert all(delegate[key] is None for key in ("user_id", "session_key", "chat_id", "thread_id"))
    assert {key: rotated[key] for key in ("user_id", "session_key", "chat_id", "thread_id", "display_name", "origin_json")} == {
        "user_id": "u", "session_key": "telegram:u:c", "chat_id": "c", "thread_id": "thread",
        "display_name": "Chat", "origin_json": '{"platform":"telegram"}',
    }


def test_cjk_like_search_keeps_compaction_archive_but_hides_rewound_rows(db):
    db.create_session("s", "cli")
    archived = db.append_message("s", "user", "短语 archived")
    rewound = db.append_message("s", "user", "短语 rewound")
    db._conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (archived,))
    db._conn.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (rewound,))
    db._conn.commit()

    assert [hit["id"] for hit in db.search_messages("短语")] == [archived]
    assert {hit["id"] for hit in db.search_messages("短语", include_inactive=True)} == {
        archived, rewound,
    }
