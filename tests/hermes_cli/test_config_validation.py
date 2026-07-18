"""Tests for config.yaml structure validation (validate_config_structure)."""


from hermes_cli.config import _KNOWN_ROOT_KEYS, validate_config_structure, ConfigIssue


class TestCustomProvidersValidation:
    """custom_providers must be a YAML list, not a dict."""

    def test_dict_instead_of_list(self):
        """The exact Discord user scenario — custom_providers as flat dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "Generativelanguage.googleapis.com",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "xxx",
                "model": "models/gemini-2.5-flash",
                "rate_limit_delay": 2.0,
                "fallback_model": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-plus:free",
                },
            },
            "fallback_providers": [],
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("dict" in i.message and "list" in i.message for i in errors), (
            "Should detect custom_providers as dict instead of list"
        )

    def test_dict_detects_misplaced_fields(self):
        """When custom_providers is a dict, detect fields that look misplaced."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "base_url": "https://example.com",
                "api_key": "xxx",
            },
        })
        warnings = [i for i in issues if i.severity == "warning"]
        # Should flag base_url, api_key as looking like custom_providers entry fields
        misplaced = [i for i in warnings if "custom_providers entry fields" in i.message]
        assert len(misplaced) == 1

    def test_dict_detects_nested_fallback(self):
        """When fallback_model gets swallowed into custom_providers dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "fallback_model": {"provider": "openrouter", "model": "test"},
            },
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("fallback_model" in i.message and "inside" in i.message for i in errors)

    def test_valid_list_no_issues(self):
        """Properly formatted custom_providers should produce no issues."""
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "gemini", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test"},
        })
        assert len(issues) == 0

    def test_list_entry_missing_name(self):
        """List entry without name should warn."""
        issues = validate_config_structure({
            "custom_providers": [{"base_url": "https://example.com/v1"}],
            "model": {"provider": "custom"},
        })
        assert any("missing 'name'" in i.message for i in issues)

    def test_list_entry_missing_base_url(self):
        """List entry without base_url should warn."""
        issues = validate_config_structure({
            "custom_providers": [{"name": "test"}],
            "model": {"provider": "custom"},
        })
        assert any("missing 'base_url'" in i.message for i in issues)

    def test_list_entry_not_dict(self):
        """Non-dict list entries should warn."""
        issues = validate_config_structure({
            "custom_providers": ["not-a-dict"],
            "model": {"provider": "custom"},
        })
        assert any("not a dict" in i.message for i in issues)

    def test_none_custom_providers_no_issues(self):
        """No custom_providers at all should be fine."""
        issues = validate_config_structure({
            "model": {"provider": "openrouter"},
        })
        assert len(issues) == 0


class TestFallbackModelValidation:
    """fallback_model should be a top-level dict with provider + model."""

    def test_missing_provider(self):
        issues = validate_config_structure({
            "fallback_model": {"model": "anthropic/claude-sonnet-4"},
        })
        assert any("missing 'provider'" in i.message for i in issues)

    def test_missing_model(self):
        issues = validate_config_structure({
            "fallback_model": {"provider": "openrouter"},
        })
        assert any("missing 'model'" in i.message for i in issues)

    def test_valid_fallback(self):
        issues = validate_config_structure({
            "fallback_model": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4",
            },
        })
        # Only fallback-related issues should be absent
        fb_issues = [i for i in issues if "fallback" in i.message.lower()]
        assert len(fb_issues) == 0

    def test_non_dict_fallback(self):
        issues = validate_config_structure({
            "fallback_model": "openrouter:anthropic/claude-sonnet-4",
        })
        assert any("should be a dict" in i.message for i in issues)

    def test_empty_fallback_dict_no_issues(self):
        """Empty fallback_model dict means disabled — no warnings needed."""
        issues = validate_config_structure({
            "fallback_model": {},
        })
        fb_issues = [i for i in issues if "fallback" in i.message.lower()]
        assert len(fb_issues) == 0

    def test_valid_fallback_list(self):
        """List-form fallback_model (chain) should validate when every entry has provider+model."""
        issues = validate_config_structure({
            "fallback_model": [
                {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            ],
        })
        fb_issues = [i for i in issues if "fallback" in i.message.lower()]
        assert len(fb_issues) == 0

    def test_fallback_list_entry_missing_provider(self):
        issues = validate_config_structure({
            "fallback_model": [
                {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
                {"model": "claude-sonnet-4-6"},
            ],
        })
        assert any("fallback_model[1]" in i.message and "provider" in i.message for i in issues)

    def test_fallback_list_entry_missing_model(self):
        issues = validate_config_structure({
            "fallback_model": [
                {"provider": "openrouter"},
            ],
        })
        assert any("fallback_model[0]" in i.message and "model" in i.message for i in issues)

    def test_fallback_list_entry_not_a_dict(self):
        issues = validate_config_structure({
            "fallback_model": ["openrouter:anthropic/claude-sonnet-4"],
        })
        assert any("fallback_model[0]" in i.message and "should be a dict" in i.message for i in issues)


class TestMissingModelSection:
    """Warn when custom_providers exists but model section is missing."""

    def test_custom_providers_without_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
        })
        assert any("no 'model' section" in i.message for i in issues)

    def test_custom_providers_with_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test-model"},
        })
        # Should not warn about missing model section
        assert not any("no 'model' section" in i.message for i in issues)


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b


class TestCodeExecutionValidation:
    def test_code_execution_is_a_known_root_section(self):
        assert "code_execution" in _KNOWN_ROOT_KEYS

    def test_valid_code_execution_config_has_no_issues(self):
        assert validate_config_structure({
            "code_execution": {
                "mode": "strict",
                "persistent": False,
                "timeout": 300,
                "max_tool_calls": 50,
                "kernel_idle_ttl": 900,
                "max_stdout_bytes": 50_000,
                "max_stderr_bytes": 10_000,
                "artifact_dir": "/tmp/hermes-results",
            },
        }) == []

    def test_invalid_code_execution_types_and_ranges_are_reported(self):
        issues = validate_config_structure({
            "code_execution": {
                "mode": "banana",
                "persistent": "yes",
                "timeout": 0,
                "max_tool_calls": -1,
                "kernel_idle_ttl": -2,
                "max_stdout_bytes": 0,
                "max_stderr_bytes": True,
                "artifact_dir": 42,
            },
        })
        messages = "\n".join(issue.message for issue in issues)
        assert "code_execution.mode" in messages
        assert "code_execution.persistent" in messages
        assert "code_execution.timeout" in messages
        assert "code_execution.max_tool_calls" in messages
        assert "code_execution.kernel_idle_ttl" in messages
        assert "code_execution.max_stdout_bytes" in messages
        assert "code_execution.max_stderr_bytes" in messages
        assert "code_execution.artifact_dir" in messages

    def test_code_execution_must_be_a_mapping(self):
        issues = validate_config_structure({"code_execution": []})
        assert any("code_execution must be a mapping" in issue.message for issue in issues)

    def test_nested_code_execution_defaults_match_plan_contract(self):
        from hermes_cli.config import DEFAULT_CONFIG

        section = DEFAULT_CONFIG["code_execution"]
        assert section["sessions"] == {
            "enabled": False,
            "idle_timeout_seconds": 900,
        }
        assert section["tools"] == {"include": [], "exclude": []}
        assert section["artifacts"] == {
            "max_bytes": 10_485_760,
            "max_total_bytes": 52_428_800,
        }

    def test_nested_code_execution_validation_rejects_bad_shapes(self):
        issues = validate_config_structure({
            "code_execution": {
                "sessions": {"enabled": "yes", "idle_timeout_seconds": 0},
                "tools": {"include": "read_file", "exclude": [1]},
                "artifacts": {"max_bytes": 0, "max_total_bytes": -1},
            },
        })
        messages = "\n".join(issue.message for issue in issues)
        assert "code_execution.sessions.enabled" in messages
        assert "code_execution.sessions.idle_timeout_seconds" in messages
        assert "code_execution.tools.include" in messages
        assert "code_execution.tools.exclude" in messages
        assert "code_execution.artifacts.max_bytes" in messages
        assert "code_execution.artifacts.max_total_bytes" in messages
