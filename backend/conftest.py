import os
from pathlib import Path

import pytest

os.environ.setdefault("MONAW_CONTROL_SECRET", "test-control-secret-that-is-at-least-32-characters")
# Never launch the built-in Chrome DevTools MCP server through npx from tests.
os.environ["MONAW_DISABLE_BUILTIN_MCP"] = "1"

TEST_GROUPS = {
    "api": {
        "test_chat_turn_detach.py",
        "test_conversation_workspace.py",
        "test_code_integrity.py",
        "test_control_plane_auth.py",
        "test_main_config.py",
        "test_support_report.py",
        "test_mcp_runtime_routes.py",
        "test_mcp_settings_integration.py",
        "test_execution_detail_settings.py",
        "test_mcp_credentials.py",
        "test_response_attachments.py",
        "test_file_mutations_api.py",
        "test_profile_attachment_api.py",
        "test_runs_api.py",
    },
    "runtime": {
        "test_architecture_boundaries.py",
        "test_chatgpt_account.py",
        "test_execution_gate.py",
        "test_approval_review.py",
        "test_approval_injection.py",
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
        "test_durable_journal.py",
        "test_durable_recovery.py",
        "test_live_context_budget.py",
        "test_tool_registry_enhanced.py",
        "test_builtin_mcp.py",
        "test_turn_loop.py",
    },
    "policy": {
        "test_data_scope.py",
        "test_destination_policy.py",
        "test_mcp_action_scope.py",
        "test_mcp_control_scope.py",
        "test_action_policy.py",
        "test_action_approval_binding.py",
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
        "test_browser_real_chrome.py",
        "test_browser_action_verification.py",
        "test_local_chrome_launch.py",
        "test_exec_tool.py",
        "test_filesystem_tools.py",
        "test_file_mutation_preview.py",
        "test_file_mutation_evidence.py",
        "test_file_mutation_integration.py",
        "test_file_mutation_undo.py",
        "test_p7_process_recovery.py",
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
        "test_durable_migrations.py",
        "test_local_data_retention.py",
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
def isolate_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep conversations out of the developer's real ~/.monaw/runtime/agent.db.

    Code that reaches the shared get_db() singleton would otherwise add
    conversations, such as an empty chat, to the app the developer uses.
    """
    import app.agent.database as database
    import app.agent.durable.recovery as durable_recovery

    database.reset_db()
    monkeypatch.setattr(database, "_RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "agent.db")
    # The legacy JSON import moves files it finds here into a backup folder.
    monkeypatch.setattr(database, "_JSON_CONVERSATIONS_DIR", tmp_path / "conversations")
    durable_recovery._PROCESS_IDENTITY.close()
    process_identity = durable_recovery.OwnerProcessIdentity(tmp_path / "owner_locks")
    monkeypatch.setattr(durable_recovery, "_OWNER_LOCK_DIR", tmp_path / "owner_locks")
    monkeypatch.setattr(durable_recovery, "_PROCESS_IDENTITY", process_identity)
    yield
    process_identity.close()
    database.reset_db()


@pytest.fixture(autouse=True)
def isolate_debug_log(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Keep chat-route debug lines out of the developer's ~/.monaw/runtime debug log."""
    import app.debug_ndjson as debug_ndjson

    monkeypatch.setattr(debug_ndjson, "_LOG_FILE", tmp_path_factory.mktemp("debug-log") / "debug.log")


@pytest.fixture(autouse=True)
def isolate_exec_artifacts(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Keep test command output files out of the developer's ~/.monaw/runtime/exec.

    A folder of its own, so tests that expect tmp_path to stay empty still can.
    """
    import app.agent.sandbox.artifacts as artifacts

    monkeypatch.setattr(artifacts, "EXEC_ARTIFACT_DIR", tmp_path_factory.mktemp("exec-artifacts"))


@pytest.fixture(scope="session")
def _test_observability_recorder(tmp_path_factory: pytest.TempPathFactory):
    from app.agent.observability.recorder import ObservabilityRecorder

    return ObservabilityRecorder(root=tmp_path_factory.mktemp("observability"))


@pytest.fixture(autouse=True)
def isolate_observability(_test_observability_recorder, monkeypatch: pytest.MonkeyPatch):
    """Keep test runs out of the developer's ~/.monaw/runtime/observability.

    One recorder for the session, since building one opens its database.
    """
    import app.agent.observability.recorder as recorder

    monkeypatch.setattr(recorder, "_RECORDER", _test_observability_recorder)


@pytest.fixture(autouse=True)
def isolate_account_model_list(monkeypatch: pytest.MonkeyPatch):
    """Keep the model catalog from fetching with the developer's signed-in ChatGPT account."""
    import app.agent.model_catalog as model_catalog

    monkeypatch.setattr(model_catalog, "_fetch_account_models", lambda: [])
    monkeypatch.setattr(model_catalog, "_account_cache", None)


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
