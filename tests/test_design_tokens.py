"""Measured accessibility checks for the dashboard's colour tokens.

The palette is written in OKLCH, so contrast cannot be eyeballed from the CSS.
This converts the real tokens in styles.css to sRGB and asserts the WCAG ratios
the design depends on, in both themes, so a palette tweak that quietly breaks
legibility fails CI instead of shipping.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "src" / "job_agent" / "web" / "static" / "styles.css").read_text(encoding="utf-8")


def _block(selector: str) -> str:
    start = CSS.index("{", CSS.index(selector))
    depth = 0
    for i in range(start, len(CSS)):
        depth += CSS[i] == "{"
        depth -= CSS[i] == "}"
        if depth == 0:
            return CSS[start:i]
    raise AssertionError(f"unterminated block for {selector}")


def _tokens(selector: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", _block(selector))}


def _srgb(value: str) -> tuple[float, float, float]:
    m = re.match(r"oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", value)
    assert m, f"not an oklch colour: {value}"
    lightness, chroma, hue = float(m[1]), float(m[2]), math.radians(float(m[3]))
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(max(0.0, min(1.0, c)) for c in rgb)  # linear light


def _luminance(rgb: tuple[float, float, float]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


DARK = _tokens(":root {")
LIGHT = {**DARK, **_tokens(':root[data-theme="light"]')}
WHITE = (1.0, 1.0, 1.0)
SURFACES = ("bg", "bg-elevated", "bg-node")

TEXT_ON_SURFACE = [
    (fg, surface)
    for fg in ("text", "text-muted", "text-faint", "ok", "run", "warn", "err")
    for surface in SURFACES
] + [("accent", "bg"), ("accent", "bg-elevated")]


@pytest.mark.parametrize("theme_name,theme", [("dark", DARK), ("light", LIGHT)])
@pytest.mark.parametrize("fg,surface", TEXT_ON_SURFACE)
def test_text_meets_aa(theme_name: str, theme: dict[str, str], fg: str, surface: str) -> None:
    ratio = _ratio(_srgb(theme[fg]), _srgb(theme[surface]))
    assert ratio >= 4.5, f"{theme_name}: {fg} on {surface} is {ratio:.2f}:1 (needs 4.5)"


@pytest.mark.parametrize("theme_name,theme", [("dark", DARK), ("light", LIGHT)])
@pytest.mark.parametrize("surface", SURFACES)
def test_control_borders_are_visible(theme_name: str, theme: dict[str, str], surface: str) -> None:
    ratio = _ratio(_srgb(theme["control-border"]), _srgb(theme[surface]))
    assert ratio >= 3.0, f"{theme_name}: control border on {surface} is {ratio:.2f}:1 (needs 3.0)"


@pytest.mark.parametrize("theme_name,theme", [("dark", DARK), ("light", LIGHT)])
@pytest.mark.parametrize("token", ["accent-strong", "accent-strong-hover"])
def test_primary_button_label_is_readable(theme_name: str, theme: dict[str, str], token: str) -> None:
    ratio = _ratio(WHITE, _srgb(theme[token]))
    assert ratio >= 4.5, f"{theme_name}: white on {token} is {ratio:.2f}:1 (needs 4.5)"
