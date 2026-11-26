"""Run a small harness benchmark in a disposable Monaw profile."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    """Configure isolation before importing runtime modules or opening SQLite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("fixture", "gemini", "openai"), default="fixture")
    parser.add_argument("--model", default="")
    parser.add_argument("--cases", nargs="+", default=None)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    model = args.model or ("gemini-flash-lite-latest" if args.provider == "gemini" else "gpt-6-luna")
    if args.provider == "openai" and model != "gpt-6-luna":
        parser.error("This benchmark's live OpenAI allowance is limited to gpt-6-luna.")
    key = (
        os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
        if args.provider == "gemini"
        else os.environ.get("OPENAI_API_KEY", "")
    )
    if args.provider != "fixture" and not key:
        parser.error(
            "Set the selected provider's API key in the environment; it is never written to the report."
        )
    root = Path(tempfile.mkdtemp(prefix="monaw-harness-eval-")).resolve()
    os.environ.update(
        MONAW_HOME=str(root),
        AGENT_RUNTIME_DIR=str(root / "runtime"),
        AGENT_WORKSPACE_DIR=str(root / "workspace"),
        AGENT_MEMORY_DIR=str(root / "memory"),
        MONAW_DISABLE_BUILTIN_MCP="1",
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from evals.harness import CASES, evaluate_case

    chosen = args.cases or CASES
    if set(chosen) - set(CASES):
        parser.error("Unknown evaluation case.")

    async def run():
        """Keep live requests sequential and report failures without exposing keys."""
        results = []
        for case in chosen:
            try:
                result = await evaluate_case(
                    case, root / "workspace", provider=args.provider, model=model, api_key=key
                )
            except Exception as exc:
                result = {"case": case, "passed": False, "error_type": type(exc).__name__}
                if args.provider == "fixture":
                    import traceback

                    traceback.print_exc()
            results.append(result)
            print(
                json.dumps(
                    {
                        "case": case,
                        "passed": result["passed"],
                        "checks": result.get("checks", {}),
                        "metrics": result.get("metrics", {}),
                    }
                ),
                flush=True,
            )
        return results

    results = asyncio.run(run())
    destination = (args.output or root / "results.json").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"provider": args.provider, "results": results}, indent=2), encoding="utf-8"
    )
    print(f"Report: {destination}")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
