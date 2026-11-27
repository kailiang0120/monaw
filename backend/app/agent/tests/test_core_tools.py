import io
import json
import sys
import urllib.error
from types import SimpleNamespace

from app.agent.tool_registry import ToolRegistry
from app.skills.core import system_controls
from app.skills.core import tools as core_tools
from app.skills.web_search import tools as web_search_tools


def test_calculator_uses_ast_math_only():
    assert core_tools._calculator("sqrt(16) + 2 ** 3") == "12.0"
    assert core_tools._calculator("__import__('os').system('echo no')").startswith("Error:")


def test_datetime_returns_structured_timezone_payload():
    result = json.loads(core_tools._datetime_tool())

    assert result["local"]
    assert result["utc"]
    assert "epoch_seconds" in result


def test_web_search_registration_sets_tavily_env(monkeypatch):
    class FakeTavilySearch:
        def __init__(self, max_results=5):
            self.max_results = max_results

        def invoke(self, query):
            return [{"title": "ok", "query": query}]

    fake_module = SimpleNamespace(TavilySearch=FakeTavilySearch)
    monkeypatch.setitem(sys.modules, "langchain_tavily", fake_module)
    monkeypatch.setenv("TAVILY_API_KEY", "")

    settings = SimpleNamespace(tavily_api_key="test-key")
    registry = ToolRegistry()

    web_search_tools.register_tools(registry, settings)

    assert registry.get_tool("web_search") is not None
    assert web_search_tools.os.environ["TAVILY_API_KEY"] == "test-key"


def _fake_urlopen(monkeypatch, *, payload=None, error=None):
    sent = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        sent.append(request)
        if error is not None:
            raise error
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(web_search_tools.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_web_search_is_not_registered_without_any_key():
    registry = ToolRegistry()

    web_search_tools.register_tools(
        registry, SimpleNamespace(tavily_api_key="", keenable_api_key="  ")
    )

    assert registry.get_tool("web_search") is None


def test_web_search_uses_keenable_when_only_its_key_is_set(monkeypatch):
    sent = _fake_urlopen(
        monkeypatch,
        payload={
            "results": [
                {"title": "A", "url": "https://a.example/x", "description": "", "snippet": "text"},
                {"title": "no url", "url": "", "snippet": "dropped"},
            ]
        },
    )
    registry = ToolRegistry()
    web_search_tools.register_tools(
        registry, SimpleNamespace(tavily_api_key="", keenable_api_key="kn-test-key")
    )

    tool = registry.get_tool("web_search")
    result = json.loads(tool["callable"]("monaw agent"))

    assert result == [{"title": "A", "url": "https://a.example/x", "content": "text"}]
    request = sent[0]
    assert request.full_url == "https://api.keenable.ai/v1/search"
    assert request.get_method() == "POST"
    assert request.get_header("X-api-key") == "kn-test-key"
    assert request.get_header("X-keenable-title") == "Monaw"
    assert json.loads(request.data) == {
        "query": "monaw agent",
        "max_results": 5,
        "snippet_max_length": 500,
    }


def test_web_search_keeps_tavily_first_when_both_keys_are_set(monkeypatch):
    class FakeTavilySearch:
        def __init__(self, max_results=5):
            pass

        def invoke(self, query):
            return "tavily result"

    monkeypatch.setitem(
        sys.modules, "langchain_tavily", SimpleNamespace(TavilySearch=FakeTavilySearch)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "")
    sent = _fake_urlopen(monkeypatch, payload={"results": []})
    registry = ToolRegistry()

    web_search_tools.register_tools(
        registry, SimpleNamespace(tavily_api_key="tvly", keenable_api_key="kn-test-key")
    )

    assert registry.get_tool("web_search")["callable"]("q") == "tavily result"
    assert sent == []


def test_keenable_failures_name_the_problem_instead_of_returning_no_results(monkeypatch):
    for status, expected in [
        (429, "rate limiting"),
        (401, "check KEENABLE_API_KEY"),
        (403, "check KEENABLE_API_KEY"),
        (402, "out of credits"),
        (500, "try again later"),
    ]:
        error = urllib.error.HTTPError(
            web_search_tools._KEENABLE_URL, status, "x", {}, io.BytesIO(b"{}")
        )
        _fake_urlopen(monkeypatch, error=error)

        result = web_search_tools._keenable_search("kn-test-key", "q")

        assert result.startswith(f"Error: Keenable search failed (HTTP {status})")
        assert expected in result


def test_keenable_network_and_parse_errors_are_reported(monkeypatch):
    _fake_urlopen(monkeypatch, error=urllib.error.URLError("down"))
    assert "unavailable" in web_search_tools._keenable_search("kn-test-key", "q")

    _fake_urlopen(monkeypatch, payload=["not", "an", "object"])
    assert web_search_tools._keenable_search("kn-test-key", "q") == "[]"


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
