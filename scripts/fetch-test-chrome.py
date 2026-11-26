"""Fetch an isolated Windows Chrome for Testing build for contract verification."""

import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from urllib.request import urlopen
from zipfile import ZipFile


def configure_sandbox(binary: Path) -> None:
    """Grant Chrome's own sandbox capabilities access to this extracted build."""
    setup = binary.parent / "setup.exe"
    if os.name != "nt" or not setup.is_file():
        return  # Older supported Chrome builds do not ship the configuration tool.
    result = subprocess.run(
        [str(setup), "--configure-browser-in-directory=" + str(binary.parent)],
        capture_output=True, text=True, timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # Chrome's configuration-only mode returns this explicit success status;
    # it does not install a browser or change the user's default browser.
    if result.returncode != 78:  # CONFIGURE_APP_CONTAINER_SANDBOX_SUCCESS
        raise RuntimeError(f"Chrome sandbox configuration failed ({result.returncode}): {result.stderr[-2000:]}")


def fetch_browser(channel: str, destination: Path) -> Path:
    """Resolve the supported minimum or current stable build from Google's feed."""
    base = "https://googlechromelabs.github.io/chrome-for-testing/"
    feed = "latest-versions-per-milestone-with-downloads.json" if channel == "minimum" else "last-known-good-versions-with-downloads.json"
    with urlopen(base + feed, timeout=30) as response:
        metadata = json.load(response)
    # Keep the minimum aligned with browser-extension/manifest.json.
    minimum = json.loads((Path(__file__).resolve().parents[1] / "browser-extension/manifest.json").read_text())["minimum_chrome_version"].split(".")[0]
    build = metadata["milestones"][minimum] if channel == "minimum" else metadata["channels"]["Stable"]
    download = next(item["url"] for item in build["downloads"]["chrome"] if item["platform"] == "win64")
    parsed = urlsplit(download)
    if parsed.scheme != "https" or parsed.netloc != "storage.googleapis.com" or not parsed.path.startswith("/chrome-for-testing-public/"):
        raise ValueError("Unexpected Chrome download origin")
    version = build["version"]
    if not all(part.isdecimal() for part in version.split(".")):
        raise ValueError("Invalid Chrome version")
    root = (destination / version).resolve()
    binary = root / "chrome-win64/chrome.exe"
    if binary.is_file():
        configure_sandbox(binary)
        return binary
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "chrome.zip"
    with urlopen(download, timeout=60) as response, archive.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)
    with ZipFile(archive) as zipped:
        for name in zipped.namelist():
            if not (root / name).resolve().is_relative_to(root):
                raise ValueError("Chrome archive contains an unsafe path")
        zipped.extractall(root)
    if not binary.is_file():
        raise RuntimeError("Chrome download did not contain the expected executable")
    configure_sandbox(binary)
    return binary


def main() -> None:
    """Print only the selected binary path so CI can set its test environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("minimum", "stable"), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    print(fetch_browser(arguments.channel, arguments.destination))


if __name__ == "__main__":
    main()
