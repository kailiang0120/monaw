"""Entry point for the packaged (frozen) backend executable.

A frozen build has no separate Python interpreter, and ``sys.executable`` is
this program. The backend still starts helpers with ``sys.executable -m <module>``
(the built-in browser server) and ``sys.executable -c <code>`` (launching Chrome
outside Monaw's process tree), so this entry point accepts those forms too.
Otherwise it serves the API, like ``uvicorn app.main:app --host H --port P``.
"""

from __future__ import annotations

import argparse
import multiprocessing
import runpy
import sys


def _run_module(module: str, args: list[str]) -> None:
    sys.argv = [module, *args]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


def _run_code(code: str, args: list[str]) -> None:
    sys.argv = ["-c", *args]
    exec(compile(code, "<string>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})


def _serve(args: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="monaw-backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args(args)

    import uvicorn

    from app.main import app

    uvicorn.run(app, host=options.host, port=options.port)


def main(argv: list[str]) -> None:
    multiprocessing.freeze_support()
    if len(argv) >= 2 and argv[0] == "-m":
        _run_module(argv[1], argv[2:])
    elif len(argv) >= 2 and argv[0] == "-c":
        _run_code(argv[1], argv[2:])
    else:
        _serve(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
