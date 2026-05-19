import json
import sys
from types import SimpleNamespace

from app.agent.tool_registry import ToolRegistry
from app.skills.core import system_controls
from app.skills.core import tools as core_tools


def test_calculator_uses_ast_math_only():
    assert core_tools._calculator("sqrt(16) + 2 ** 3") == "12.0"
    assert core_tools._calculator("__import__('os').system('echo no')").startswith("Error:")


def test_datetime_returns_structured_timezone_payload():
    result = json.loads(core_tools._datetime_tool())

    assert result["local"]
    assert result["utc"]
    assert "epoch_seconds" in result


def test_core_web_search_registration_sets_tavily_env(monkeypatch):
    class FakeTavilySearch:
        def __init__(self, max_results=5):
            self.max_results = max_results

        def invoke(self, query):
            return [{"title": "ok", "query": query}]

    fake_module = SimpleNamespace(TavilySearch=FakeTavilySearch)
    monkeypatch.setitem(sys.modules, "langchain_tavily", fake_module)

    settings = SimpleNamespace(tavily_api_key="test-key")
    registry = ToolRegistry()

    core_tools.register_tools(registry, settings)

    assert registry.get_tool("web_search") is not None
    assert core_tools.os.environ["TAVILY_API_KEY"] == "test-key"


def test_core_does_not_register_filesystem_compat_aliases():
    registry = ToolRegistry()
    core_tools.register_tools(registry, SimpleNamespace(tavily_api_key=""))

    all_names = {tool["name"] for tool in registry.get_all_tools()}

    assert "file_reader" not in all_names
    assert "create_file" not in all_names


def test_core_registers_system_control_tools():
    registry = ToolRegistry()
    core_tools.register_tools(registry, SimpleNamespace(tavily_api_key=""))

    names = {tool["name"] for tool in registry.get_all_tools()}

    assert {"system_volume", "screen_brightness", "desktop_configuration"} <= names
    assert json.loads(system_controls.desktop_configuration(action="list_pages"))["status"] == "ok"
