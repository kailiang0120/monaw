"""Core import behavior, source containment, and portable skill activation."""

import base64
import io
import os
import stat
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import skill_import
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.skill_loader import load_tools
from app.agent.skill_prompt import build_skill_prompt
from app.config import settings
from app.main import app
from app.security.control_plane import mint_control_token


def _archive(files: dict[str, bytes]) -> bytes:
    """Build an in-memory source archive for import tests."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _document(name="sample-skill") -> bytes:
    """Return portable frontmatter without any Monaw-specific fields."""
    return f"---\nname: {name}\ndescription: Summarize a document.\n---\nRead references/guide.md before using scripts/run.py.\n".encode()


def test_install_zip_activates_instructions_and_keeps_files_without_loading_code():
    client = TestClient(app)
    encoded = base64.b64encode(_archive({
        "bundle/sample-skill/SKILL.md": _document(),
        "bundle/sample-skill/references/guide.md": b"Use concise prose.",
        "bundle/sample-skill/scripts/run.py": b"print('explicit script')",
        "bundle/sample-skill/tools.py": b"raise RuntimeError('must never execute during skill import')",
    })).decode()
    preview = client.post("/api/skills/inspect", json={"filename": "bundle.zip", "data_base64": encoded})
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["skills"][0]["files"] == 4
    response = client.post("/api/skills/install", json={"token": payload["token"], "skills": [payload["skills"][0]["id"]]})
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["tools"]["skills"]["sample-skill"] is True
    root = skill_import.USER_SKILLS_DIR / "sample-skill"
    assert (root / "references/guide.md").read_text() == "Use concise prose."
    runtime = build_runtime_namespace(settings, load_agent_settings(settings))
    skills, _ = load_tools(runtime)
    imported = next(skill for skill in skills if skill.name == "sample-skill")
    assert imported.available
    assert str(root.resolve()) in build_skill_prompt([imported])


def test_local_folder_is_snapshotted_and_duplicate_import_does_not_replace_it(tmp_path):
    folder = tmp_path / "sample"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(_document())
    preview = skill_import.prepare_import("session", source=str(folder))
    (folder / "SKILL.md").write_bytes(_document("changed-after-preview"))
    assert skill_import.install_import("session", preview["token"], ["SKILL.md"]) == ["sample-skill"]
    preview = skill_import.prepare_import("session", filename="SKILL.md", data_base64=base64.b64encode(_document()).decode())
    assert preview["skills"][0]["installed"] is True
    with pytest.raises(ValueError, match="already exists"):
        skill_import.install_import("session", preview["token"], ["SKILL.md"])
    assert "changed-after-preview" not in (skill_import.USER_SKILLS_DIR / "sample-skill/SKILL.md").read_text()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "skill/NUL.txt", "skill/file:stream", "skill/trailing."])
def test_archive_rejects_paths_that_escape_or_alias_on_windows(path):
    with pytest.raises(ValueError):
        skill_import._read_zip(_archive({path: b"unsafe"}))


def test_archive_rejects_symlinks_and_expansion_limit(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("skill/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(ValueError, match="links"):
        skill_import._read_zip(buffer.getvalue())
    data = _archive({"large": b"x" * 300})
    assert len(data) < 200
    monkeypatch.setattr(skill_import, "MAX_BYTES", 200)
    with pytest.raises(ValueError, match="Extracted"):
        skill_import._read_zip(data)


def test_install_requires_the_same_session_and_an_unexpired_preview(monkeypatch):
    preview = skill_import.prepare_import("owner", filename="SKILL.md", data_base64=base64.b64encode(_document()).decode())
    with pytest.raises(ValueError, match="expired"):
        skill_import.install_import("other", preview["token"], ["SKILL.md"])
    monkeypatch.setattr(skill_import, "TTL_SECONDS", -1)
    with pytest.raises(ValueError, match="expired"):
        skill_import.install_import("owner", preview["token"], ["SKILL.md"])


def test_builtin_names_and_unselected_sources_cannot_be_installed():
    preview = skill_import.prepare_import("owner", filename="SKILL.md", data_base64=base64.b64encode(_document("core")).decode())
    with pytest.raises(ValueError, match="already exists"):
        skill_import.install_import("owner", preview["token"], ["SKILL.md"])
    with pytest.raises(ValueError, match="Choose"):
        skill_import.install_import("owner", preview["token"], ["unpreviewed/SKILL.md"])


def test_import_routes_require_settings_write_scope():
    token = mint_control_token(os.environ["MONAW_CONTROL_SECRET"], scopes=["agent:run"])
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    assert client.post("/api/skills/inspect", json={"source": "owner/repo"}, headers=headers).status_code == 403
    assert client.post("/api/skills/install", json={"token": "0" * 32, "skills": ["SKILL.md"]}, headers=headers).status_code == 403


def test_github_download_preserves_skill_subfolder_and_rejects_foreign_redirects(monkeypatch):
    original_client = httpx.Client
    data = _archive({"owner-repo-123/skills/sample/SKILL.md": _document(), "owner-repo-123/README.md": b"repo"})

    def handle(request):
        """Simulate GitHub's archive redirect and its codeload response."""
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/owner/repo/zip/main"})
        return httpx.Response(200, content=data)

    monkeypatch.setattr(skill_import.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    files = skill_import._read_github("https://github.com/owner/repo/tree/main/skills/sample")
    assert files == {"SKILL.md": _document()}
    assert skill_import._github_source("owner/repo")[0].endswith("/zipball/HEAD")
    monkeypatch.setattr(skill_import.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})), **kwargs))
    with pytest.raises(ValueError, match="unsupported"):
        skill_import._read_github("owner/repo")
