import os
from pathlib import Path

import pytest

os.environ.setdefault("MONAW_CONTROL_SECRET", "test-control-secret-that-is-at-least-32-characters")
# Never launch the built-in Chrome DevTools MCP server through npx from tests.
os.environ["MONAW_DISABLE_BUILTIN_MCP"] = "1"

TEST_GROUPS = {
    "api": {
        "test_conversation_workspace.py",
        "test_control_plane_auth.py",
        "test_main_config.py",
        "test_support_report.py",
        "test_mcp_runtime_routes.py",
        "test_mcp_settings_integration.py",
        "test_mcp_credentials.py",
        "test_response_attachments.py",
    },
    "runtime": {
        "test_architecture_boundaries.py",
        "test_codex_account_provider.py",
        "test_execution_gate.py",
        "test_approval_review.py",
        "test_execution_resume.py",
        "test_iteration_budget.py",
        "test_job_manager.py",
        "test_llm_client_schema.py",
        "test_retry.py",
        "test_run_events.py",
        "test_ui_events.py",
        "test_run_state_machine.py",
        "test_runtime_identity.py",
        "test_settings_store.py",
        "test_skill_loader.py",
        "test_skill_prompt.py",
        "test_speech_to_text.py",
        "test_tool_executor.py",
        "test_tool_batch.py",
        "test_run_outcome.py",
        "test_live_context_budget.py",
        "test_tool_registry_enhanced.py",
        "test_builtin_mcp.py",
        "test_turn_loop.py",
    },
    "policy": {
        "test_access_grant_broker_discovery.py",
        "test_approval_broker.py",
        "test_controller_policy.py",
        "test_core_tools.py",
    },
    "sandbox": {
        "test_sandbox_adversarial.py",
        "test_sandbox_disclosure.py",
        "test_sandbox.py",
        "test_sandbox_docker.py",
        "test_sandbox_environment.py",
        "test_sandbox_image_inventory.py",
        "test_sandbox_manager.py",
        "test_sandbox_path_policy.py",
        "test_sandbox_sessions.py",
    },
    "skills": {
        "test_computer_state.py",
        "test_computer_input.py",
        "test_browser_fetch.py",
        "test_browser_extension_bridge.py",
        "test_browser_extension_install.py",
        "test_browser_extension_tools.py",
        "test_local_chrome_launch.py",
        "test_exec_tool.py",
        "test_filesystem_tools.py",
        "test_mcp_bridge.py",
        "test_skill_creator_hardening.py",
        "test_windows_controller.py",
        "test_screen_redaction.py",
    },
    "memory": {
        "test_memory_review.py",
        "test_memory_embeddings.py",
        "test_storage_usage.py",
        "test_memory_store.py",
        "test_memory_archive.py",
        "test_memory_context_management.py",
        "test_context_compression.py",
        "test_memory_migrations.py",
        "test_data_lifecycle.py",
        "test_observability_recorder.py",
    },
    "integrations": {
        "test_scheduler_dispatch.py",
        "test_scheduler.py",
        "test_telegram_integration.py",
    },
}

_GROUP_BY_FILE = {filename: group for group, filenames in TEST_GROUPS.items() for filename in filenames}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    unclassified: set[str] = set()
    for item in items:
        filename = Path(str(item.path)).name
        group = _GROUP_BY_FILE.get(filename)
        if group is None:
            unclassified.add(filename)
            continue
        item.add_marker(getattr(pytest.mark, group))

    if unclassified:
        names = ", ".join(sorted(unclassified))
        raise pytest.UsageError(f"Backend tests must be assigned to one TEST_GROUPS entry in conftest.py: {names}")


@pytest.fixture(autouse=True)
def isolate_mcp_credentials(monkeypatch: pytest.MonkeyPatch):
    import app.security.mcp_credentials as credentials
    monkeypatch.setattr(credentials, "_VALUES", {})
    monkeypatch.setattr(credentials, "_REDACTIONS", [])


@pytest.fixture(autouse=True)
def isolate_approval_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep approval tickets out of the developer's real ~/.monaw runtime dir.

    Tickets created by a test would otherwise persist as pending and be
    replayed as live approval prompts the next time the app boots.
    """
    import app.agent.approval_broker as broker

    monkeypatch.setattr(broker, "_TICKETS_FILE", tmp_path / "tickets.jsonl")
    monkeypatch.setattr(broker, "_APPROVAL_LOG", tmp_path / "approval_log.md")
    broker._all_tickets.clear()
    broker._pending_index.clear()
    broker._resume_events.clear()
    broker._resume_decisions.clear()
    yield


@pytest.fixture(autouse=True)
def isolate_browser_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's installed Monaw extension from changing the browser connection."""
    import app.skills.browser_use.extension.common as extension_common

    monkeypatch.setattr(extension_common, "installed_extension_dir", lambda: tmp_path / "browser-extension")
    monkeypatch.setattr(extension_common, "status_file", lambda: tmp_path / "browser-extension-status.json")
    yield


@pytest.fixture(autouse=True)
def isolate_user_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep skills the developer created in ~/.monaw/skills out of the catalog under test."""
    import app.agent.skill_loader as skill_loader
    import app.skills.skill_creator.tools as skill_creator_tools

    user_skills = tmp_path / "user-skills"
    monkeypatch.setattr(skill_loader, "USER_SKILLS_DIR", user_skills)
    monkeypatch.setattr(skill_creator_tools, "SKILLS_DIR", user_skills)


@pytest.fixture(autouse=True)
def isolate_memory_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep memory files out of the developer's real ~/.monaw/memory folder."""
    from app.agent.memory_store import reset_memory_store

    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path / "memory"))
    reset_memory_store()
    yield
    reset_memory_store()


@pytest.fixture(autouse=True)
def isolate_sandbox_image_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep per-image capability probes deterministic and out of user runtime data."""
    import app.agent.sandbox.image_inventory as image_inventory

    monkeypatch.setattr(
        image_inventory,
        "_INVENTORY_PATH",
        tmp_path / "image-inventories.json",
    )


@pytest.fixture(autouse=True)
def authenticate_test_client_requests(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from app.security.control_plane import ALL_SCOPES, mint_control_token

    original_request = TestClient.request
    token = mint_control_token(os.environ["MONAW_CONTROL_SECRET"], scopes=ALL_SCOPES)

    def authenticated_request(self, method, url, *args, **kwargs):
        if str(url).startswith("/api"):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Authorization", f"Bearer {token}")
            kwargs["headers"] = headers
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", authenticated_request)
