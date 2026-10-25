import json
from pathlib import Path

import pytest

import app.skills.browser_use.extension.common as common
from app.skills.browser_use.extension import install


def _source(tmp_path: Path, version: str = "1.0.0") -> Path:
    source = tmp_path / "bundled"
    (source / "icons").mkdir(parents=True)
    (source / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (source / "service_worker.js").write_text("// sw", encoding="utf-8")
    (source / "icons" / "icon16.png").write_bytes(b"png")
    return source


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    source = _source(tmp_path)
    monkeypatch.setenv(common.SOURCE_ENV, str(source))
    return source


def test_install_copies_the_extension_and_pairs_it(bundled):
    result = install.install_extension(8436)
    target = common.installed_extension_dir()
    assert result == {"folder": str(target), "version": "1.0.0"}
    assert (target / "service_worker.js").read_text(encoding="utf-8") == "// sw"
    pairing = common.read_pairing()
    assert pairing["port"] == 8436 and len(pairing["token"]) >= 32

    status = install.extension_status()
    assert status["installed"] and status["paired"] and not status["update_available"]
    assert status["connected"] is False and status["server_running"] is False


def test_update_keeps_the_token_and_removes_stale_files(bundled, tmp_path):
    install.install_extension(8436)
    target = common.installed_extension_dir()
    token = common.read_pairing()["token"]
    (target / "old.js").write_text("stale", encoding="utf-8")
    (bundled / "manifest.json").write_text(json.dumps({"version": "1.1.0"}), encoding="utf-8")
    assert install.extension_status()["update_available"] is True

    install.install_extension(9000)
    assert not (target / "old.js").exists()
    assert common.manifest_version(target) == "1.1.0"
    assert common.read_pairing() == {"protocol": common.PROTOCOL_VERSION, "port": 9000, "token": token}


def test_missing_bundle_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv(common.SOURCE_ENV, str(tmp_path / "nothing"))
    with pytest.raises(install.ExtensionInstallError) as excinfo:
        install.install_extension(8436)
    assert excinfo.value.reason_code == "extension_source_missing"


def test_pairing_port_follows_the_setting(bundled):
    install.install_extension(8436)
    install.ensure_pairing_port(8500)
    assert common.read_pairing()["port"] == 8500


def test_connection_status_requires_a_live_server(bundled, monkeypatch):
    install.write_connection_status(connected=True, version="1.0.0", error="")
    assert install.extension_status()["connected"] is True
    monkeypatch.setattr(install, "_pid_alive", lambda pid: False)
    assert install.extension_status()["connected"] is False


def test_bundled_extension_matches_the_fixed_id():
    """The manifest key must produce the extension id the server trusts."""
    import base64
    import hashlib

    manifest = json.loads((Path(common.__file__).resolve().parents[5] / "browser-extension" / "manifest.json")
                          .read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(char, 16)) for char in digest) == common.EXTENSION_ID
    assert "debugger" in manifest["permissions"]
    assert "host_permissions" not in manifest and "content_scripts" not in manifest
