"""Regenerate Monaw app icons and interaction animations from Logo.png.

Requires Pillow. Run from the repository root with:
    python scripts/generate-brand-assets.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
SOURCE = FRONTEND / "src" / "assets" / "Logo.png"
MASCOTS = FRONTEND / "src" / "assets" / "mascots"
RESAMPLE = Image.Resampling.LANCZOS
GREEN = (197, 255, 160, 255)
CREAM = (247, 255, 238, 255)
FRAME_SIZE = 352
DESIGN_SIZE = 176


def load_mark() -> Image.Image:
    source = Image.open(SOURCE).convert("RGBA")
    alpha = source.getchannel("A").point(lambda value: 255 if value > 32 else 0)
    bounds = alpha.getbbox()
    if bounds is None:
        raise ValueError(f"No visible mark in {SOURCE}")
    return source.crop(bounds)


def place_mark(mark: Image.Image, size: int, scale: float = 0.87, angle: float = 0, y: float = 0) -> Image.Image:
    canvas = Image.new("RGBA", (size, size))
    fitted = mark.copy()
    fitted.thumbnail((round(size * scale), round(size * scale)), RESAMPLE)
    if angle:
        fitted = fitted.rotate(angle, Image.Resampling.BICUBIC, expand=True)
    x = (size - fitted.width) // 2
    top = round((size - fitted.height) / 2 + y)
    canvas.alpha_composite(fitted, (x, top))
    return canvas


def gif_frame(mark: Image.Image, state: str, index: int, count: int) -> Image.Image:
    size = FRAME_SIZE
    pixel_scale = size / DESIGN_SIZE
    point = lambda values: tuple(round(value * pixel_scale) for value in values)
    line_width = lambda value: max(1, round(value * pixel_scale))
    phase = index / count
    bob = math.sin(phase * math.tau) * 2.5 * pixel_scale
    angle = 0.0
    scale = 0.83
    if state == "thinking":
        angle = math.sin(phase * math.tau) * 3.5
    elif state in ("curious", "alert"):
        angle = -7 + math.sin(phase * math.tau) * 4
    elif state == "success":
        scale += 0.035 * math.sin(phase * math.tau)
        bob -= max(0, math.sin(phase * math.tau)) * 3 * pixel_scale
    else:
        angle = math.sin(phase * math.tau) * 1.2

    frame = place_mark(mark, size, scale=scale, angle=angle, y=bob)
    draw = ImageDraw.Draw(frame)

    if state == "thinking":
        # A repeating thought trail remains visible at 40px avatar size.
        dots = ((29, 54, 4), (21, 40, 5), (25, 22, 6))
        for offset, (x, y, radius) in enumerate(dots):
            active = (index + offset * 3) % count
            brightness = 0.38 + 0.62 * max(0, math.sin(active / count * math.tau))
            color = (202, 255, 171, round(255 * brightness))
            draw.ellipse(point((x - radius, y - radius, x + radius, y + radius)), fill=color)
    elif state in ("curious", "alert"):
        # A small bubble distinguishes paused requests from failed runs.
        outline = GREEN if state == "curious" else (255, 178, 120, 255)
        draw.ellipse(point((13, 13, 53, 53)), fill=(36, 83, 70, 246), outline=outline, width=line_width(2))
        if state == "curious":
            draw.arc(point((24, 20, 42, 38)), 195, 355, fill=CREAM, width=line_width(3))
            draw.line(point((41, 29, 33, 37, 33, 40)), fill=CREAM, width=line_width(3), joint="curve")
            draw.ellipse(point((31, 44, 35, 48)), fill=CREAM)
        else:
            draw.line(point((33, 22, 33, 38)), fill=outline, width=line_width(4))
            draw.ellipse(point((31, 43, 35, 47)), fill=outline)
    elif state == "success":
        pulse = max(0.15, math.sin(phase * math.tau))
        for x, y, radius in ((148, 39, 9), (26, 116, 6)):
            reach = radius * (0.7 + 0.4 * pulse)
            color = (223, 255, 189, round(245 * pulse))
            draw.line(point((x, y - reach, x, y + reach)), fill=color, width=line_width(3))
            draw.line(point((x - reach, y, x + reach, y)), fill=color, width=line_width(3))
            draw.ellipse(point((x - 2, y - 2, x + 2, y + 2)), fill=color)
    return frame


def save_transparent_gif(frames: list[Image.Image], path: Path, duration: int, loop: int = 0) -> None:
    indexed: list[Image.Image] = []
    for frame in frames:
        # GIF has binary transparency. Keep original edge RGB (no dark matte),
        # then let the browser downsample the double-resolution frames.
        alpha = frame.getchannel("A")
        rgb = frame.convert("RGB")
        palette_frame = rgb.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        mask = alpha.point(lambda value: 255 if value < 176 else 0)
        palette_frame.paste(255, mask)
        palette_frame.info["transparency"] = 255
        indexed.append(palette_frame)
    indexed[0].save(
        path,
        save_all=True,
        append_images=indexed[1:],
        duration=duration,
        loop=loop,
        disposal=2,
        transparency=255,
        optimize=False,
    )


def save_animated_webp(frames: list[Image.Image], path: Path, duration: int, loop: int = 0) -> None:
    """Preserve smooth alpha in the animation displayed by Chromium/Electron."""
    frames[0].save(
        path,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=loop,
        quality=92,
        alpha_quality=100,
        method=3,
    )


def main() -> None:
    mark = load_mark()
    MASCOTS.mkdir(parents=True, exist_ok=True)

    for state, count, duration in (
        ("idle", 16, 90),
        ("thinking", 18, 85),
        ("success", 15, 90),
        ("curious", 16, 95),
        ("alert", 16, 95),
    ):
        frames = [gif_frame(mark, state, index, count) for index in range(count)]
        repeat = 1 if state == "success" else 0
        save_transparent_gif(frames, MASCOTS / f"{state}.gif", duration, loop=repeat)
        save_animated_webp(frames, MASCOTS / f"{state}.webp", duration, loop=repeat)

    # Solid app tiles preserve contrast against light and dark taskbars.
    tile = Image.new("RGBA", (1024, 1024))
    rounded = Image.new("L", tile.size)
    ImageDraw.Draw(rounded).rounded_rectangle((28, 28, 996, 996), radius=215, fill=255)
    background = Image.new("RGBA", tile.size, (18, 44, 39, 255))
    tile.paste(background, (0, 0), rounded)
    tile.alpha_composite(place_mark(mark, 1024, scale=0.77, y=20))
    assets = FRONTEND / "assets"
    tile.save(assets / "app-icon.png", optimize=True)
    tile.resize((512, 512), RESAMPLE).save(FRONTEND / "public" / "icon.png", optimize=True)
    tile.save(assets / "app-icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    tile.save(assets / "app-icon.icns")
    tile.save(FRONTEND / "public" / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])


if __name__ == "__main__":
    main()
