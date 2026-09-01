#!/usr/bin/env python3
"""Render an SVG file to PNG.

Backends (tried in order):
1. Playwright + headless Chromium  — best fidelity (full CSS/font support)
2. cairosvg                        — lightweight fallback

Usage:
    python render_svg.py input.svg output.png [--scale N]

The PNG size = SVG viewBox size * scale (default scale=1).
Exit codes: 0 success, 1 all backends failed.
"""
import argparse
import re
import sys
from pathlib import Path


def svg_viewbox_size(svg_path: Path) -> tuple[int, int]:
    """Extract width/height from the SVG viewBox (or width/height attrs)."""
    text = svg_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'viewBox\s*=\s*["\']([\d.\s,\-]+)["\']', text)
    if m:
        parts = m.group(1).replace(",", " ").split()
        if len(parts) == 4:
            return max(1, int(float(parts[2]))), max(1, int(float(parts[3])))
    m = re.search(r'width\s*=\s*["\']([\d.]+)', text)
    m2 = re.search(r'height\s*=\s*["\']([\d.]+)', text)
    if m and m2:
        return max(1, int(float(m.group(1)))), max(1, int(float(m2.group(1))))
    return 1200, 800


def render_playwright(svg_path: Path, png_path: Path, scale: float) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    # Bundled chromium first; system Edge/Chrome as fallback (no download needed).
    channels = (None, "msedge", "chrome")
    last_exc: Exception | None = None
    for channel in channels:
        try:
            w, h = svg_viewbox_size(svg_path)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
                    page.goto(svg_path.resolve().as_uri())
                    # Wait until fonts/layout settle.
                    page.wait_for_timeout(300)
                    svg_el = page.locator("svg")
                    svg_el.screenshot(path=str(png_path))
                finally:
                    browser.close()
            return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    print(f"[render_svg] playwright backend failed: {last_exc}", file=sys.stderr)
    return False


def render_cairosvg(svg_path: Path, png_path: Path, scale: float) -> bool:
    try:
        import cairosvg
    except ImportError:
        return False
    try:
        cairosvg.svg2png(
            url=str(svg_path), write_to=str(png_path), scale=scale
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[render_svg] cairosvg backend failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Render SVG to PNG")
    parser.add_argument("svg", type=Path)
    parser.add_argument("png", type=Path)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()

    if not args.svg.exists():
        print(f"[render_svg] SVG not found: {args.svg}", file=sys.stderr)
        return 1

    for backend in (render_playwright, render_cairosvg):
        if backend(args.svg, args.png, args.scale):
            print(f"[render_svg] OK -> {args.png}")
            return 0

    print(
        "[render_svg] All backends failed. Try:\n"
        "  python -m playwright install chromium\n"
        "  python -m pip install cairosvg",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
