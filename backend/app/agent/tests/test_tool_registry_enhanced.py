"""Tests for the simplified tool registry helpers."""

from app.agent.tool_registry import ToolRegistry, can_parallelize, coerce_tool_args
from app.agent.harness.tool_protocol import ToolCallRequest, ToolCallResult


def test_tool_protocol_models_serialize_to_dict():
    request = ToolCallRequest(
        call_id="call-1",
        name="demo",
        arguments={"count": 2},
        step_id="tool-1",
    )
    result = ToolCallResult.from_output(
        call_id="call-1",
        name="demo",
        output='{"status":"ok","value":2}',
    )

    assert request.to_dict() == {
        "call_id": "call-1",
        "name": "demo",
        "arguments": {"count": 2},
        "step_id": "tool-1",
    }
    assert result.to_dict()["normalized_output"] == {"status": "ok", "value": 2}
    assert result.to_dict()["status"] == "ok"


def test_coerce_string_to_int():
    schema = {"properties": {"count": {"type": "integer"}}}
    result = coerce_tool_args({"count": "42"}, schema)
    assert result["count"] == 42


def test_coerce_string_to_float():
    schema = {"properties": {"ratio": {"type": "number"}}}
    result = coerce_tool_args({"ratio": "3.14"}, schema)
    assert result["ratio"] == 3.14


def test_coerce_string_to_boolean():
    schema = {"properties": {"flag": {"type": "boolean"}}}
    assert coerce_tool_args({"flag": "true"}, schema)["flag"] is True
    assert coerce_tool_args({"flag": "false"}, schema)["flag"] is False


def test_registry_dispatch_coerces_arguments():
    registry = ToolRegistry(
        [
            {
                "name": "demo",
                "description": "demo",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                "callable": lambda count: count,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )

    dispatched = registry.dispatch("demo", {"count": "7"})
    assert dispatched is not None
    assert dispatched["arguments"]["count"] == 7


def test_registry_dispatch_drops_unknown_arguments():
    registry = ToolRegistry(
        [
            {
                "name": "demo",
                "description": "demo",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                "callable": lambda count: count,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )

    dispatched = registry.dispatch("demo", {"count": "7", "mode": "auto"})

    assert dispatched is not None
    assert dispatched["arguments"] == {"count": 7}


def test_registry_dispatch_reports_validation_errors():
    registry = ToolRegistry(
        [
            {
                "name": "demo",
                "description": "demo",
                "parameters": {
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": ["auto"]}},
                    "required": ["mode"],
                },
                "callable": lambda mode: mode,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )

    dispatched = registry.dispatch("demo", {"mode": "manual"})

    assert dispatched is not None
    assert dispatched["validation_errors"]
    assert "mode" in dispatched["validation_errors"][0]


def test_can_parallelize_safe():
    calls = [
        {"name": "web_search", "arguments": {"query": "a"}},
        {"name": "file_reader", "arguments": {"path": "/a"}},
    ]
    batches = can_parallelize(calls)
    assert len(batches) == 1
    assert len(batches[0]) == 2


def test_can_parallelize_never_parallel():
    calls = [
        {"name": "exec", "arguments": {"command": "dir"}},
        {"name": "web_search", "arguments": {"query": "a"}},
    ]
    batches = can_parallelize(calls)
    assert len(batches) == 2


def test_computer_functions_actions_are_never_parallelized():
    calls = [
        {"name": "computer_functions_act", "arguments": {"actions": [{"type": "click", "x": 10, "y": 20}]}},
        {"name": "computer_functions_activate_window", "arguments": {"hwnd": 123}},
    ]
    batches = can_parallelize(calls)
    assert batches == [[calls[0]], [calls[1]]]


def test_can_parallelize_uses_tool_metadata_resource_locks():
    registry = ToolRegistry(
        [
            {
                "name": "a",
                "description": "a",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "a",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": True, "resource_locks": ["same"]},
            },
            {
                "name": "b",
                "description": "b",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "b",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": True, "resource_locks": ["same"]},
            },
        ]
    )
    calls = [{"name": "a", "arguments": {}}, {"name": "b", "arguments": {}}]

    assert can_parallelize(calls, registry.get_tool) == [[calls[0]], [calls[1]]]


def test_can_parallelize_preserves_order_around_serial_barriers():
    registry = ToolRegistry(
        [
            {
                "name": "safe_a",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "a",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True},
            },
            {
                "name": "unsafe",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "unsafe",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": False, "resource_locks": ["state"]},
            },
            {
                "name": "safe_b",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "b",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True},
            },
            {
                "name": "safe_c",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "c",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True},
            },
        ]
    )
    calls = [
        {"name": "safe_a", "arguments": {}},
        {"name": "unsafe", "arguments": {}},
        {"name": "safe_b", "arguments": {}},
        {"name": "safe_c", "arguments": {}},
    ]

    assert can_parallelize(calls, registry.get_tool) == [
        [calls[0]],
        [calls[1]],
        [calls[2], calls[3]],
    ]


def test_can_parallelize_treats_conflicting_resources_and_affinity_as_barriers():
    registry = ToolRegistry(
        [
            {
                "name": "resource_a",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "a",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": ["shared"]},
            },
            {
                "name": "resource_b",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "b",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": ["shared"]},
            },
            {
                "name": "affine",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "affine",
                "execution_mode": "sync_thread_affine",
                "affinity_group": "same-thread",
                "metadata": {"parallel_safe": True},
            },
        ]
    )
    calls = [
        {"name": "resource_a", "arguments": {}},
        {"name": "resource_b", "arguments": {}},
        {"name": "affine", "arguments": {}},
    ]

    assert can_parallelize(calls, registry.get_tool) == [[calls[0]], [calls[1]], [calls[2]]]


def test_can_parallelize_unknown_lookup_and_high_risk_metadata_are_serial():
    registry = ToolRegistry(
        [
            {
                "name": "safe",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "safe",
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True},
            },
            {
                "name": "risky",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "risky",
                "execution_mode": "sync_stateless",
                "domain": "desktop",
                "metadata": {"parallel_safe": True, "risk_level": "high"},
            },
        ]
    )
    calls = [
        {"name": "safe", "arguments": {}},
        {"name": "missing", "arguments": {}},
        {"name": "risky", "arguments": {}},
    ]

    assert can_parallelize(calls, registry.get_tool) == [[calls[0]], [calls[1]], [calls[2]]]


def test_get_all_tools_can_hide_model_invisible_aliases():
    registry = ToolRegistry(
        [
            {
                "name": "canonical",
                "description": "Canonical tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "legacy_alias",
                "description": "Legacy alias",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "deprecated_alias_for": "canonical",
            },
        ]
    )

    all_tools = {tool["name"] for tool in registry.get_all_tools()}
    visible_tools = {tool["name"] for tool in registry.get_all_tools(visible_only=True)}

    assert all_tools == {"canonical", "legacy_alias"}
    assert visible_tools == {"canonical"}


def test_dispatch_resolves_hidden_aliases_even_when_excluded_from_schema():
    registry = ToolRegistry(
        [
            {
                "name": "legacy_alias",
                "description": "Legacy alias",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                "callable": lambda count: count,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "deprecated_alias_for": "canonical",
            }
        ]
    )

    dispatched = registry.dispatch("legacy_alias", {"count": "3"})
    schema_names = {entry["function"]["name"] for entry in registry.get_schemas()}

    assert dispatched is not None
    assert dispatched["arguments"]["count"] == 3
    assert "legacy_alias" not in schema_names


def test_registry_search_can_activate_hidden_tool():
    registry = ToolRegistry(
        [
            {
                "name": "scheduled_task_create",
                "description": "Create cron scheduled tasks.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "metadata": {"dynamic_load": True},
            }
        ]
    )

    matches = registry.search_tools("cron schedule")
    changed = registry.set_visibility([matches[0]["name"]], True)

    assert matches[0]["name"] == "scheduled_task_create"
    assert changed == ["scheduled_task_create"]
    assert {tool["name"] for tool in registry.get_all_tools(visible_only=True)} == {"scheduled_task_create"}


def test_search_tags_improve_matching():
    registry = ToolRegistry(
        [
            {
                "name": "scheduled_task_create",
                "description": "Create a local scheduled agent task.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "metadata": {
                    "dynamic_load": True,
                    "search_tags": ["remind", "timer", "cron", "recurring"],
                },
            },
        ]
    )

    matches = registry.search_tools("remind me later")
    assert matches
    assert matches[0]["name"] == "scheduled_task_create"


def test_search_does_not_activate_deprecated_alias():
    """tool_search should only activate tools with dynamic_load=True."""
    import json
    from app.agent.skill_loader import _register_tool_search

    registry = ToolRegistry(
        [
            {
                "name": "file_reader",
                "description": "Read a file from disk.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "ok",
                "domain": "filesystem",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "deprecated_alias_for": "file_read",
            },
            {
                "name": "scheduled_task_create",
                "description": "Create cron scheduled tasks.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "metadata": {"dynamic_load": True},
            },
        ]
    )

    _register_tool_search(registry)
    tool_search_fn = registry.get_tool("tool_search")["callable"]

    result = json.loads(tool_search_fn("file read"))
    assert "file_reader" not in result["activated"]
    assert not registry.get_tool("file_reader").get("visible_to_model", True)

    result = json.loads(tool_search_fn("cron schedule"))
    assert "scheduled_task_create" in result["activated"]


def test_dynamic_load_full_cycle():
    """End-to-end: hidden tool -> tool_search activates -> visible in schemas -> dispatch works."""
    import json
    from app.agent.skill_loader import _register_tool_search

    registry = ToolRegistry(
        [
            {
                "name": "visible_tool",
                "description": "Always visible.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: "visible_ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "hidden_dynamic",
                "description": "Hidden dynamic tool for scheduling.",
                "parameters": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
                "callable": lambda title: f"created: {title}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "visible_to_model": False,
                "metadata": {"dynamic_load": True, "search_tags": ["schedule", "cron"]},
            },
        ]
    )

    _register_tool_search(registry)
    initial_revision = registry.revision

    schema_names_before = {s["function"]["name"] for s in registry.get_schemas()}
    assert "hidden_dynamic" not in schema_names_before
    assert "visible_tool" in schema_names_before

    tool_search_fn = registry.get_tool("tool_search")["callable"]
    result = json.loads(tool_search_fn("schedule cron"))

    assert "hidden_dynamic" in result["activated"]
    assert registry.revision > initial_revision

    schema_names_after = {s["function"]["name"] for s in registry.get_schemas()}
    assert "hidden_dynamic" in schema_names_after

    dispatched = registry.dispatch("hidden_dynamic", {"title": "test task"})
    assert dispatched is not None
    assert dispatched["arguments"]["title"] == "test task"
