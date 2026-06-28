import sqlite3

from app.agent.db_bootstrap import SCHEMA_VERSION, initialize_database
from app.agent.memory_repository import MemorySectionRepository
from app.agent.memory_documents import Section
from app.agent.observability.redaction import redact, safe_preview
from app.skills.browser_use.url_policy import (
    browser_url_policy,
    fetch_domain_allowed,
    normalize_fetch_url,
    validate_fetch_final_url,
)


def test_database_bootstrap_owns_schema_and_forward_upgrade_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    initialize_database(conn)

    version = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
    task_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    run_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }

    assert version["version"] == SCHEMA_VERSION
    assert {"owner_principal_id", "permission_profile_id", "permission_profile_snapshot"} <= task_columns
    assert {"idempotency_key", "lease_owner", "lease_expires_at"} <= run_columns


def test_memory_section_repository_round_trips_sectioned_documents(tmp_path):
    repo = MemorySectionRepository(tmp_path, ("fact", "preference"), now=lambda: "2026-06-28T00:00:00+00:00")

    repo.save_sections(
        "style",
        [
            Section(
                id="sec-tone",
                title="Tone",
                meta={"id": "sec-tone", "importance": 7},
                body="Prefer focused updates.",
            )
        ],
    )

    loaded = repo.load_sections("preference")
    assert repo.normalize_category("style") == "preference"
    assert loaded[0].id == "sec-tone"
    assert loaded[0].body == "Prefer focused updates."
    assert repo.remove_section_by_id("sec-tone") is True
    assert repo.load_sections("preference") == []


def test_observability_redaction_is_independent_of_recorder_storage():
    payload = redact({
        "api_key": "sk-secretsecretsecretsecret",
        "message": "Bearer abcdefghijklmnopqrstuvwxyz password=hunter2",
    })

    assert payload["api_key"] == "[REDACTED]"
    assert payload["message"] == "Bearer [REDACTED] password=[REDACTED]"
    assert safe_preview("x" * 12, limit=5) == "xxxxx\n[truncated]"


def test_browser_url_policy_is_separate_from_browser_execution(monkeypatch):
    class Manager:
        config = {"allowed_domains": ["example.com"]}

    assert normalize_fetch_url("example.com/path") == "https://example.com/path"
    assert fetch_domain_allowed("https://docs.example.com/page", ["example.com"])
    assert not fetch_domain_allowed("https://evil.test/page", ["example.com"])

    _url, policy_error = browser_url_policy(Manager(), "https://evil.test", action="browser_open")
    assert policy_error is not None
    assert policy_error["reason_code"] == "domain_not_allowed"

    monkeypatch.setattr("app.skills.browser_use.url_policy.assert_fetch_host_public", lambda host: (_ for _ in ()).throw(ValueError("blocked")))
    final_error = validate_fetch_final_url("https://example.com", "https://example.com/redirect", ["example.com"])
    assert final_error is not None
    assert final_error["reason_code"] == "blocked_redirect_host"
