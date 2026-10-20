"""Loaded only by the application test subprocess through its private PYTHONPATH."""
import os

if os.environ.get("MONAW_E2E_ROOT"):
    from monaw_fixture import install
    install()
