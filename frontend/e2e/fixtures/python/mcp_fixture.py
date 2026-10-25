"""Real local MCP transport; only disposable digests are persisted."""
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP

control = Path(sys.argv[1])
value = os.environ.get("API_KEY", "")
digest = hashlib.sha256(value.encode()).hexdigest()
version = 2 if (control / "mcp-schema-v2").exists() else 1
assert value, "MCP must not start before credential hydration"
assert "OPENAI_API_KEY" not in os.environ and "MONAW_CONTROL_SECRET" not in os.environ
with (control / "mcp-starts.jsonl").open("a", encoding="utf-8") as file:
    file.write(json.dumps({"digest": digest, "version": version}) + "\n")
# Deliberately exercise credential-bearing stderr without writing raw data to a file.
sys.stderr.write("fixture stderr echo: " + value + "\n")
sys.stderr.flush()
server = FastMCP("Monaw application credential fixture", log_level="ERROR")


def result():
    with (control / "mcp-calls.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps({"digest": digest, "version": version}) + "\n")
    return {"digest": digest, "version": version}


if version == 1:
    @server.tool()
    def get_credential_status(label: str = "") -> dict:
        return result()
else:
    @server.tool()
    def get_credential_status(label: str = "", extra: str = "") -> dict:
        return result()

    @server.tool()
    def get_schema_version() -> int:
        return version


server.run(transport="stdio")
