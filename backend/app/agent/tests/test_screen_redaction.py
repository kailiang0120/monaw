from PIL import Image

from app.skills.computer_use import screen_redaction


def test_redaction_blacks_out_blocked_windows_relative_to_capture_origin(tmp_path, monkeypatch):
    capture = tmp_path / "shot.png"
    Image.new("RGB", (100, 80), (255, 255, 255)).save(capture)
    # A blocked window at screen (1010, 2020)-(1030, 2040); the capture starts at (1000, 2000).
    monkeypatch.setattr(screen_redaction, "blocked_window_rects", lambda above_hwnd=0: [(1010, 2020, 1030, 2040)])

    count = screen_redaction.redact_capture(capture, origin_x=1000, origin_y=2000)

    with Image.open(capture) as image:
        assert count == 1
        assert image.getpixel((15, 25)) == (0, 0, 0)
        assert image.getpixel((5, 5)) == (255, 255, 255)
        assert image.getpixel((40, 50)) == (255, 255, 255)


def test_redaction_ignores_windows_outside_the_capture(tmp_path, monkeypatch):
    capture = tmp_path / "shot.png"
    Image.new("RGB", (50, 50), (255, 255, 255)).save(capture)
    monkeypatch.setattr(screen_redaction, "blocked_window_rects", lambda above_hwnd=0: [(500, 500, 600, 600)])

    assert screen_redaction.redact_capture(capture, origin_x=0, origin_y=0) == 0
