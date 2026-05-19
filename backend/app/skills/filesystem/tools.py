from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from app.skills.computer_use import window_ops
from app.skills.filesystem import file_ops


def _explorer_reveal(path: str) -> str:
    path_obj = Path(path)
    return window_ops._ctrl_select_in_explorer(str(path_obj.parent), path_obj.name)


def _batch_results(items, fn):
    results = []
    for item in items or []:
        try:
            result = fn(item)
            decoded = json.loads(result) if isinstance(result, str) else result
        except Exception as exc:
            decoded = {"status": "error", "error": str(exc)}
        results.append(decoded)
        status = decoded.get("status")
        if status in {"blocked", "pending_approval", "pending_access_grant", "error"}:
            terminal = dict(decoded)
            terminal["results"] = results
            terminal["count"] = len(results)
            return json.dumps(terminal, ensure_ascii=False)
    return json.dumps({"status": "ok", "operation": "batch", "results": results, "count": len(results)}, ensure_ascii=False)


def _fs_copy_many(items: list[dict]) -> str:
    return _batch_results(
        items,
        lambda item: file_ops.fs_copy(
            str(item.get("source", "")),
            str(item.get("destination", "")),
            bool(item.get("overwrite", False)),
            bool(item.get("create_parents", True)),
            bool(item.get("dry_run", False)),
        ),
    )


def _fs_move_many(items: list[dict]) -> str:
    return _batch_results(
        items,
        lambda item: file_ops.fs_move(
            str(item.get("source", "")),
            str(item.get("destination", "")),
            bool(item.get("overwrite", False)),
            bool(item.get("create_parents", True)),
            bool(item.get("dry_run", False)),
        ),
    )


def _fs_delete_many(paths: list[str]) -> str:
    return _batch_results(paths, lambda path: file_ops.fs_delete(str(path)))


def _params(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    callable_: Callable,
    *,
    domain: str = "filesystem",
    visible: bool = True,
    deprecated_alias_for: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool = {
        "name": name,
        "description": description,
        "parameters": parameters,
        "callable": callable_,
        "domain": domain,
        "execution_mode": "sync_stateless",
        "affinity_group": None,
        "visible_to_model": visible,
        "metadata": metadata or {"parallel_safe": False, "resource_locks": ["filesystem"], "mutates_state": True},
    }
    if deprecated_alias_for:
        tool["deprecated_alias_for"] = deprecated_alias_for
    return tool


def _legacy_alias(
    name: str,
    canonical_name: str,
    parameters: dict[str, Any],
    callable_: Callable,
    *,
    domain: str = "filesystem",
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return _tool(
        name,
        f"Legacy alias for {canonical_name}.",
        parameters,
        callable_,
        domain=domain,
        visible=False,
        deprecated_alias_for=canonical_name,
        metadata=metadata,
    )


_PATH = {"path": {"type": "string", "description": "Local filesystem path."}}
_PATH_PARAMS = _params(_PATH, ["path"])
_TREE_PARAMS = _params(
    {**_PATH, "depth": {"type": "integer", "default": 2}, "limit": {"type": "integer", "default": 200}},
    ["path"],
)
_HASH_PARAMS = _params({**_PATH, "algorithm": {"type": "string", "default": "sha256"}}, ["path"])
_COPY_MOVE_PARAMS = _params(
    {
        "source": {"type": "string"},
        "destination": {"type": "string"},
        "overwrite": {"type": "boolean", "default": False},
        "create_parents": {"type": "boolean", "default": True},
        "dry_run": {"type": "boolean", "default": False},
    },
    ["source", "destination"],
)
_LEGACY_COPY_MOVE_PARAMS = _params(
    {"source": {"type": "string"}, "destination": {"type": "string"}},
    ["source", "destination"],
)
_READ_METADATA = {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"}
_MUTATE_METADATA = {"parallel_safe": False, "resource_locks": ["filesystem"], "mutates_state": True, "risk_level": "medium"}
_DELETE_METADATA = {"parallel_safe": False, "resource_locks": ["filesystem"], "mutates_state": True, "risk_level": "high"}
_EXPLORER_METADATA = {"parallel_safe": False, "resource_locks": ["desktop", "explorer"], "mutates_state": True, "risk_level": "low"}
_BATCH_METADATA = {
    "parallel_safe": False,
    "resource_locks": ["filesystem"],
    "mutates_state": True,
    "risk_level": "medium",
    "dynamic_load": True,
    "search_tags": ["batch", "multiple files", "bulk", "copy many", "move many", "delete many"],
}


def register_tools(registry, _settings=None) -> None:
    registry.extend(
        [
            _tool(
                "fs_stat",
                "Return existence and metadata for a file or directory.",
                _PATH_PARAMS,
                file_ops.file_stat,
                metadata=_READ_METADATA,
            ),
            _tool(
                "fs_list",
                "List files in a directory. Use pattern and recursive=true for glob-style discovery.",
                _params(
                    {
                        **_PATH,
                        "pattern": {"type": "string", "default": "*"},
                        "recursive": {"type": "boolean", "default": False},
                        "limit": {"type": "integer", "default": 200},
                    },
                    ["path"],
                ),
                file_ops.file_list,
                metadata=_READ_METADATA,
            ),
            _tool(
                "fs_tree",
                "Return a shallow directory tree.",
                _TREE_PARAMS,
                file_ops.file_tree,
                metadata=_READ_METADATA,
            ),
            _tool(
                "fs_read",
                "Read a size-limited slice of a local file.",
                _params(
                    {
                        **_PATH,
                        "start": {"type": "integer", "default": 0},
                        "max_bytes": {"type": "integer", "default": 65536},
                        "encoding": {"type": "string", "default": "utf-8"},
                    },
                    ["path"],
                ),
                file_ops.file_read,
                metadata=_READ_METADATA,
            ),
            _tool(
                "fs_search",
                "Search text files under a file or directory.",
                _params(
                    {
                        **_PATH,
                        "query": {"type": "string"},
                        "glob": {"type": "string", "default": "**/*"},
                        "max_results": {"type": "integer", "default": 50},
                        "case_sensitive": {"type": "boolean", "default": False},
                    },
                    ["path", "query"],
                ),
                file_ops.file_search,
                metadata=_READ_METADATA,
            ),
            _tool(
                "fs_hash",
                "Hash a local file.",
                _HASH_PARAMS,
                file_ops.file_hash,
                metadata=_READ_METADATA,
            ),
            _tool(
                "fs_write",
                "Write text to a local file.",
                _params(
                    {
                        **_PATH,
                        "content": {"type": "string", "default": ""},
                        "overwrite": {"type": "boolean", "default": False},
                        "create_parents": {"type": "boolean", "default": True},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "content"],
                ),
                file_ops.file_write,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_append",
                "Append text to a local file.",
                _params(
                    {
                        **_PATH,
                        "content": {"type": "string", "default": ""},
                        "create_parents": {"type": "boolean", "default": True},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "content"],
                ),
                file_ops.file_append,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_patch",
                "Replace text in a local file.",
                _params(
                    {
                        **_PATH,
                        "old": {"type": "string"},
                        "new": {"type": "string", "default": ""},
                        "count": {"type": "integer", "default": 1},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "old", "new"],
                ),
                file_ops.file_patch,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_mkdir",
                "Create a directory.",
                _params(
                    {
                        **_PATH,
                        "parents": {"type": "boolean", "default": True},
                        "exist_ok": {"type": "boolean", "default": True},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path"],
                ),
                file_ops.fs_mkdir,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_copy",
                "Copy a file or directory.",
                _COPY_MOVE_PARAMS,
                file_ops.fs_copy,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_move",
                "Move a file or directory.",
                _COPY_MOVE_PARAMS,
                file_ops.fs_move,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_rename",
                "Rename a file or directory within its current parent directory.",
                _params(
                    {
                        **_PATH,
                        "new_name": {"type": "string"},
                        "overwrite": {"type": "boolean", "default": False},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "new_name"],
                ),
                file_ops.fs_rename,
                metadata=_MUTATE_METADATA,
            ),
            _tool(
                "fs_delete",
                "Delete a file or directory.",
                _params(
                    {
                        **_PATH,
                        "recursive": {"type": "boolean", "default": True},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path"],
                ),
                file_ops.fs_delete,
                metadata=_DELETE_METADATA,
            ),
            _tool(
                "explorer_open",
                "Open a folder in Windows Explorer.",
                _PATH_PARAMS,
                window_ops._ctrl_open_folder,
                domain="desktop",
                metadata=_EXPLORER_METADATA,
            ),
            _tool(
                "explorer_reveal",
                "Open Windows Explorer and reveal a specific file or folder.",
                _PATH_PARAMS,
                _explorer_reveal,
                domain="desktop",
                metadata=_EXPLORER_METADATA,
            ),
            _tool(
                "fs_copy_many",
                "Copy multiple files or directories, stopping on policy prompts or errors.",
                _params({"items": {"type": "array", "items": {"type": "object"}}}, ["items"]),
                _fs_copy_many,
                visible=False,
                metadata=_BATCH_METADATA,
            ),
            _tool(
                "fs_move_many",
                "Move multiple files or directories, stopping on policy prompts or errors.",
                _params({"items": {"type": "array", "items": {"type": "object"}}}, ["items"]),
                _fs_move_many,
                visible=False,
                metadata=_BATCH_METADATA,
            ),
            _tool(
                "fs_delete_many",
                "Delete multiple files or directories, stopping on policy prompts or errors.",
                _params({"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"]),
                _fs_delete_many,
                visible=False,
                metadata=_BATCH_METADATA,
            ),
            _legacy_alias("file_reader", "fs_read", _PATH_PARAMS, file_ops.read_file_compat, metadata=_READ_METADATA),
            _legacy_alias("file_stat", "fs_stat", _PATH_PARAMS, file_ops.file_stat, metadata=_READ_METADATA),
            _legacy_alias("file_exists", "fs_stat", _PATH_PARAMS, file_ops.file_exists, metadata=_READ_METADATA),
            _legacy_alias("file_tree", "fs_tree", _TREE_PARAMS, file_ops.file_tree, metadata=_READ_METADATA),
            _legacy_alias("file_hash", "fs_hash", _HASH_PARAMS, file_ops.file_hash, metadata=_READ_METADATA),
            _legacy_alias("open_folder", "explorer_open", _PATH_PARAMS, window_ops._ctrl_open_folder, domain="desktop", metadata=_EXPLORER_METADATA),
            _legacy_alias("select_in_explorer", "explorer_reveal", _PATH_PARAMS, _explorer_reveal, domain="desktop", metadata=_EXPLORER_METADATA),
            _legacy_alias("create_folder", "fs_mkdir", _PATH_PARAMS, file_ops.fs_mkdir, metadata=_MUTATE_METADATA),
            _legacy_alias("copy", "fs_copy", _LEGACY_COPY_MOVE_PARAMS, file_ops.fs_copy, metadata=_MUTATE_METADATA),
            _legacy_alias("move", "fs_move", _LEGACY_COPY_MOVE_PARAMS, file_ops.fs_move, metadata=_MUTATE_METADATA),
            _legacy_alias("delete", "fs_delete", _PATH_PARAMS, file_ops.fs_delete, metadata=_DELETE_METADATA),
            _legacy_alias(
                "create_file",
                "fs_write",
                _params(
                    {
                        **_PATH,
                        "content": {"type": "string", "default": ""},
                        "file_type": {"type": "string", "enum": ["text", "xlsx", "docx"], "default": "text"},
                    },
                    ["path"],
                ),
                file_ops.create_file_compat,
                metadata=_MUTATE_METADATA,
            ),
            _legacy_alias(
                "file_list",
                "fs_list",
                _params(
                    {
                        **_PATH,
                        "pattern": {"type": "string", "default": "*"},
                        "recursive": {"type": "boolean", "default": False},
                        "limit": {"type": "integer", "default": 200},
                    },
                    ["path"],
                ),
                file_ops.file_list,
                metadata=_READ_METADATA,
            ),
            _legacy_alias(
                "file_glob",
                "fs_list",
                _params({**_PATH, "pattern": {"type": "string", "default": "**/*"}, "limit": {"type": "integer", "default": 200}}, ["path"]),
                file_ops.file_glob,
                metadata=_READ_METADATA,
            ),
            _legacy_alias(
                "file_read",
                "fs_read",
                _params(
                    {
                        **_PATH,
                        "start": {"type": "integer", "default": 0},
                        "max_bytes": {"type": "integer", "default": 65536},
                        "encoding": {"type": "string", "default": "utf-8"},
                    },
                    ["path"],
                ),
                file_ops.file_read,
                metadata=_READ_METADATA,
            ),
            _legacy_alias(
                "file_write",
                "fs_write",
                _params(
                    {
                        **_PATH,
                        "content": {"type": "string", "default": ""},
                        "overwrite": {"type": "boolean", "default": False},
                        "create_parents": {"type": "boolean", "default": True},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "content"],
                ),
                file_ops.file_write,
                metadata=_MUTATE_METADATA,
            ),
            _legacy_alias(
                "file_append",
                "fs_append",
                _params(
                    {
                        **_PATH,
                        "content": {"type": "string", "default": ""},
                        "create_parents": {"type": "boolean", "default": True},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "content"],
                ),
                file_ops.file_append,
                metadata=_MUTATE_METADATA,
            ),
            _legacy_alias(
                "file_patch",
                "fs_patch",
                _params(
                    {
                        **_PATH,
                        "old": {"type": "string"},
                        "new": {"type": "string", "default": ""},
                        "count": {"type": "integer", "default": 1},
                        "encoding": {"type": "string", "default": "utf-8"},
                        "dry_run": {"type": "boolean", "default": False},
                    },
                    ["path", "old", "new"],
                ),
                file_ops.file_patch,
                metadata=_MUTATE_METADATA,
            ),
            _legacy_alias(
                "file_search",
                "fs_search",
                _params(
                    {
                        **_PATH,
                        "query": {"type": "string"},
                        "glob": {"type": "string", "default": "**/*"},
                        "max_results": {"type": "integer", "default": 50},
                        "case_sensitive": {"type": "boolean", "default": False},
                    },
                    ["path", "query"],
                ),
                file_ops.file_search,
                metadata=_READ_METADATA,
            ),
            _legacy_alias(
                "rename",
                "fs_rename",
                _params({**_PATH, "new_name": {"type": "string"}}, ["path", "new_name"]),
                file_ops.fs_rename,
                metadata=_MUTATE_METADATA,
            ),
        ]
    )
