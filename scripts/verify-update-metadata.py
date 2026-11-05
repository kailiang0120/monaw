"""Verify that the Windows update feed and installer came from the same build."""
import base64
import hashlib
import json
from pathlib import Path
import sys

import yaml

root = Path(__file__).resolve().parent.parent
release = root / "frontend" / "release"
version = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
name = f"Monaw-Setup-{version}-x64.exe"
installer = release / name
metadata = yaml.safe_load((release / "latest.yml").read_text(encoding="utf-8"))
if not isinstance(metadata, dict) or metadata.get("version") != version:
    sys.exit("Update metadata version does not match the application version.")
files = [item for item in metadata.get("files", []) if isinstance(item, dict) and item.get("url") == name]
if len(files) != 1 or metadata.get("path") != name:
    sys.exit("Update metadata must reference exactly the expected installer.")
if not installer.is_file() or not (release / f"{name}.blockmap").is_file():
    sys.exit("The installer or its blockmap is missing.")
digest = hashlib.sha512()
with installer.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
checksum = base64.b64encode(digest.digest()).decode("ascii")
if files[0].get("sha512") != checksum or metadata.get("sha512") != checksum or files[0].get("size") != installer.stat().st_size:
    sys.exit("Update metadata does not match the installer checksum or size.")
print(f"Verified update metadata and installer for {version}; blockmap is present.")
