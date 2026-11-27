"""Import portable Agent Skills without executing package code during installation."""

from __future__ import annotations

import base64
import binascii
import io
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

import httpx
import yaml

from app.agent.runtime_paths import USER_SKILLS_DIR
from app.agent.skill_loader import _skill_catalog, _split_frontmatter

MAX_BYTES = 25 * 1024 * 1024
MAX_FILES = 2000
TTL_SECONDS = 15 * 60
_IGNORED = {".git", "node_modules", ".venv", "__pycache__"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_PREPARED: dict[str, dict] = {}
_LOCK = threading.RLock()


def _safe_relative(value: str) -> str:
    """Reject archive paths that cannot remain contained on Windows or POSIX."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value:
        raise ValueError("The skill contains an unsafe file path.")
    for part in path.parts:
        if part in {"..", "."} or any(char in part for char in ':<>"|?*') or part.endswith((".", " ")):
            raise ValueError("The skill contains an unsafe file path.")
        if part.split(".")[0].lower() in _RESERVED or any(ord(char) < 32 for char in part):
            raise ValueError("The skill contains a reserved file name.")
    return path.as_posix()


def _read_zip(data: bytes, *, github_folder: str | None = None) -> dict[str, bytes]:
    """Read bounded regular archive members without extracting arbitrary paths."""
    if len(data) > MAX_BYTES:
        raise ValueError("The archive exceeds the 25 MB limit.")
    files: dict[str, bytes] = {}
    size = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > MAX_FILES:
            raise ValueError("The archive contains too many files. Import a smaller skill folder.")
        skill_roots = [str(PurePosixPath(info.filename).parent) + "/" for info in archive.infolist()
                       if PurePosixPath(info.filename).name == "SKILL.md"]
        for member in archive.infolist():
            name = _safe_relative(member.filename.rstrip("/"))
            if github_folder is not None:
                # Repository packaging may contain unrelated symlinks. Read only skill trees.
                if not any(name.startswith(root) for root in skill_roots):
                    continue
                if "/" not in name:
                    continue
                name = name.split("/", 1)[1]
                prefix = github_folder.rstrip("/") + "/" if github_folder else ""
                if not name.startswith(prefix):
                    continue
                name = name[len(prefix):]
                if not name:
                    continue
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Symbolic links are not supported in imported skills.")
            if member.is_dir() or any(part in _IGNORED for part in PurePosixPath(name).parts):
                continue
            size += member.file_size
            if size > MAX_BYTES:
                raise ValueError("Extracted skill files exceed the 25 MB limit.")
            key = name.casefold()
            if any(existing.casefold() == key for existing in files):
                raise ValueError("The archive contains duplicate file names.")
            files[name] = archive.read(member)
    return files


def _read_folder(source: str) -> dict[str, bytes]:
    """Snapshot a local folder while refusing symlinks, junctions, and oversized trees."""
    root = Path(source).expanduser()
    if root.name == "SKILL.md" and root.is_file():
        root = root.parent
    if not root.is_dir():
        raise ValueError("Choose a skill folder containing SKILL.md, or upload a ZIP file.")
    if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("Choose the actual folder instead of a symbolic link or junction.")
    files: dict[str, bytes] = {}
    size = 0
    entries = 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in _IGNORED]
        entries += len(dirs) + len(names)
        if entries > MAX_FILES:
            raise ValueError("The folder has too many entries. Choose a smaller skill folder.")
        for name in dirs + names:
            path = Path(parent) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Symbolic links and junctions are not supported in imported skills.")
        for name in names:
            path = Path(parent) / name
            if not path.is_file():
                raise ValueError("Only regular files can be imported.")
            size += path.stat().st_size
            if len(files) >= MAX_FILES or size > MAX_BYTES:
                raise ValueError("The folder is too large. Choose a smaller skill folder.")
            files[_safe_relative(path.relative_to(root).as_posix())] = path.read_bytes()
    return files


def _github_source(source: str) -> tuple[str, str]:
    """Resolve GitHub shorthand and tree/blob URLs to an archive and optional subfolder."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
        source = "https://github.com/" + source
    parsed = urlparse(source)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise ValueError("Use a public github.com repository or skill folder URL.")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in parts[:2]):
        raise ValueError("The GitHub repository URL is invalid.")
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    ref, folder = "HEAD", ""
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
            raise ValueError("Use a repository URL or a GitHub tree/blob URL.")
        ref = parts[3]
        folder = "/".join(parts[4:]).removesuffix("/SKILL.md")
        if folder == "SKILL.md":
            folder = ""
        if folder:
            folder = _safe_relative(folder)
    return f"https://api.github.com/repos/{owner}/{repo}/zipball/{quote(ref, safe='')}", folder


def _read_github(source: str) -> dict[str, bytes]:
    """Download only from GitHub's archive hosts, applying limits before decompression."""
    url, folder = _github_source(source)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(4):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "codeload.github.com"}:
                raise ValueError("GitHub redirected to an unsupported download host.")
            with client.stream("GET", url, headers={"Accept": "application/vnd.github+json"}) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("The repository archive exceeds 25 MB. Upload only the skill folder as a ZIP.")
                return _read_zip(bytes(data), github_folder=folder)
    raise ValueError("Too many GitHub archive redirects.")


def _candidates(files: dict[str, bytes]) -> list[dict]:
    """Find portable skill roots and validate their required metadata."""
    candidates = []
    for path, data in sorted(files.items()):
        if PurePosixPath(path).name != "SKILL.md":
            continue
        frontmatter, body = _split_frontmatter(data.decode("utf-8-sig"))
        name, description = frontmatter.get("name"), frontmatter.get("description")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64 or name in _RESERVED:
            raise ValueError(f"{path}: name must use lowercase letters, numbers, and hyphens (up to 64 characters).")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError(f"{path}: description is required and must be at most 1024 characters.")
        root = str(PurePosixPath(path).parent)
        prefix = "" if root == "." else root + "/"
        selected = {key[len(prefix):]: value for key, value in files.items() if key.startswith(prefix)}
        candidates.append({"id": path, "name": name, "description": description.strip(), "body": body,
                           "frontmatter": frontmatter, "files": selected})
    if not candidates:
        raise ValueError("No SKILL.md files found. Full store plugins need their skill source folders; hooks and MCP settings are not imported.")
    return candidates


def prepare_import(session_id: str, *, source: str = "", filename: str = "", data_base64: str = "") -> dict:
    """Prepare an immutable preview bound to the authenticated desktop session."""
    if data_base64:
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid uploaded file data.") from exc
        if len(data) > 6 * 1024 * 1024:
            raise ValueError("Uploaded files must be 6 MB or smaller. Use a local folder for larger skills.")
        files = {"SKILL.md": data} if filename == "SKILL.md" else _read_zip(data)
        source_label = filename
    elif source.startswith("https://") or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
        files = _read_github(source)
        source_label = source
    else:
        files = _read_folder(source)
        source_label = source
    candidates = _candidates(files)
    catalog = _skill_catalog()
    existing = {entry.name for entry in catalog} | {entry.slug for entry in catalog}
    token = uuid.uuid4().hex
    with _LOCK:
        _prune_previews()
        for key, item in list(_PREPARED.items()):
            if item["session"] == session_id:
                del _PREPARED[key]
        if len(_PREPARED) >= 4:
            raise ValueError("Too many pending imports. Finish an import or try again in 15 minutes.")
        _PREPARED[token] = {"session": session_id, "created": time.monotonic(), "source": source_label, "candidates": candidates}
    return {"token": token, "skills": [{"id": item["id"], "name": item["name"], "description": item["description"],
        "files": len(item["files"]), "installed": item["name"] in existing} for item in candidates]}


def _prune_previews() -> None:
    """Discard expired previews to bound memory retained by source inspection."""
    for token, item in list(_PREPARED.items()):
        if time.monotonic() - item["created"] > TTL_SECONDS:
            del _PREPARED[token]


def install_import(session_id: str, token: str, selected: list[str]) -> list[str]:
    """Install only previewed skills, without replacing existing names or executing tools.py."""
    from app.agent.runtime import reset_runtime_cache
    from app.agent.settings_store import load_agent_settings, save_agent_settings

    with _LOCK:
        _prune_previews()
        preview = _PREPARED.get(token)
        if not preview or preview["session"] != session_id:
            raise ValueError("The import preview expired. Check the source again.")
        candidates = {item["id"]: item for item in preview["candidates"]}
        if not selected or len(selected) != len(set(selected)) or any(key not in candidates for key in selected):
            raise ValueError("Choose at least one skill from this preview.")
        items = [candidates[key] for key in selected]
        names = [item["name"] for item in items]
        catalog = _skill_catalog()
        existing = {entry.name for entry in catalog} | {entry.slug for entry in catalog}
        if len(names) != len(set(names)) or existing.intersection(names):
            raise ValueError("A selected skill name already exists. Existing skills are never overwritten.")
        USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        installed: list[Path] = []
        try:
            with tempfile.TemporaryDirectory(prefix=".import-", dir=USER_SKILLS_DIR) as temporary:
                for item in items:
                    target = USER_SKILLS_DIR / item["name"]
                    if target.exists() or target.is_symlink():
                        raise ValueError(f"{item['name']} already exists.")
                    staged = Path(temporary) / item["name"]
                    for relative, data in item["files"].items():
                        path = staged / _safe_relative(relative)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                    # External runtime flags must not become Monaw policy or loader configuration.
                    metadata = {
                        key: value
                        for key, value in item["frontmatter"].items()
                        if key in {"name", "description", "license", "compatibility", "allowed-tools"}
                        and isinstance(value, str)
                    }
                    metadata.update({"always": False, "enabled_by_default": False, "tier": "optional"})
                    original_metadata = item["frontmatter"].get("metadata")
                    metadata["metadata"] = {
                        **({key: value for key, value in original_metadata.items() if isinstance(key, str) and isinstance(value, str)}
                           if isinstance(original_metadata, dict) else {}),
                        "monaw_instruction_only": True,
                        "source": preview["source"],
                    }
                    document = "---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
                    document += "---\n\n" + item["body"] + "\n"
                    (staged / "SKILL.md").write_text(document, encoding="utf-8")
                    staged.rename(target)
                    installed.append(target)
            settings = load_agent_settings()
            settings.tools.skills.update({name: True for name in names})
            save_agent_settings(settings)
        except Exception:
            for target in installed:
                shutil.rmtree(target)
            raise
        del _PREPARED[token]
        reset_runtime_cache(reset_mcp=False)
        return names
