"""visual_check — 前端视觉回归检查脚本。

通道 A：通过 Playwright 提取 DOM 结构化数据，使用规则引擎判定。
通道 B：截图接口预留（本阶段不调用 VLM）。
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

# 预留 Playwright 导入；若环境未安装，优雅降级
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore


# --------------------------------------------------------------------------- #
# 规则引擎
# --------------------------------------------------------------------------- #


def _hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def _relative_luminance(rgb):
    # WCAG 2.1 relative luminance
    def _channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else pow((c + 0.055) / 1.055, 2.4)

    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(color_a: str, color_b: str) -> float:
    l1 = _relative_luminance(_hex_to_rgb(color_a))
    l2 = _relative_luminance(_hex_to_rgb(color_b))
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


RULES = {
    "contrast": {"normal": 4.5, "large": 3.0},
    "spacing_grid": {4, 8, 16, 24, 32, 48},
    "font_sizes_max": 4,
    "line_height_min": 1.4,
    "line_height_max": 1.6,
    "primary_buttons_max": 1,
    "border_radius_values_max": 3,
    "shadow_values_max": 2,
}


def evaluate_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """对结构化 DOM 快照执行规则引擎判定。"""
    violations: List[Dict[str, Any]] = []
    info = {
        "font_sizes": snapshot.get("font_sizes", []),
        "spacings": snapshot.get("spacings", []),
        "colors": snapshot.get("colors", []),
        "primary_buttons": snapshot.get("primary_buttons", 0),
        "border_radius_values": snapshot.get("border_radius_values", []),
        "shadow_values": snapshot.get("shadow_values", []),
    }

    # 字号层级
    if len(info["font_sizes"]) > RULES["font_sizes_max"]:
        violations.append(
            {
                "rule": "font_sizes",
                "message": f"全页字号种类 {len(info['font_sizes'])} 超过阈值 {RULES['font_sizes_max']}",
            }
        )

    # 间距节奏
    for sp in info["spacings"]:
        if sp not in RULES["spacing_grid"]:
            violations.append(
                {"rule": "spacing_grid", "message": f"间距 {sp}px 不在推荐网格中"}
            )

    # 主按钮层级
    if info["primary_buttons"] > RULES["primary_buttons_max"]:
        violations.append(
            {
                "rule": "primary_buttons",
                "message": f"每屏主按钮数 {info['primary_buttons']} 超过阈值 {RULES['primary_buttons_max']}",
            }
        )

    # 圆角取值种类
    if len(info["border_radius_values"]) > RULES["border_radius_values_max"]:
        violations.append(
            {
                "rule": "border_radius",
                "message": f"圆角取值种类 {len(info['border_radius_values'])} 超过阈值 {RULES['border_radius_values_max']}",
            }
        )

    # 阴影取值种类
    if len(info["shadow_values"]) > RULES["shadow_values_max"]:
        violations.append(
            {
                "rule": "shadow",
                "message": f"阴影取值种类 {len(info['shadow_values'])} 超过阈值 {RULES['shadow_values_max']}",
            }
        )

    passed = len(violations) == 0
    return {
        "passed": passed,
        "violations": violations,
        "info": info,
    }


# --------------------------------------------------------------------------- #
# DOM 提取（Playwright 通道 A）
# --------------------------------------------------------------------------- #


def extract_dom_snapshot(url: str, wait_ms: int = 1000) -> Dict[str, Any]:
    """使用 Playwright 提取页面 DOM 结构化数据。"""
    if sync_playwright is None:
        raise RuntimeError("Playwright 未安装，无法执行 visual_check DOM 提取")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(wait_ms)

        data = page.evaluate(
            """() => {
                const elems = Array.from(document.querySelectorAll('*'));
                const fontSizes = new Set();
                const spacings = new Set();
                const colors = new Set();
                const borderRadii = new Set();
                const shadows = new Set();
                let primaryButtons = 0;
                elems.forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.fontSize) fontSizes.add(parseInt(style.fontSize));
                    if (style.margin) spacings.add(parseInt(style.margin));
                    if (style.padding) spacings.add(parseInt(style.padding));
                    if (style.color) colors.add(style.color);
                    if (style.backgroundColor) colors.add(style.backgroundColor);
                    if (style.borderRadius) borderRadii.add(style.borderRadius);
                    if (style.boxShadow && style.boxShadow !== 'none') shadows.add(style.boxShadow);
                    if (el.tagName === 'BUTTON' && (el.classList.contains('ant-btn-primary') || style.backgroundColor !== 'transparent')) {
                        primaryButtons++;
                    }
                });
                return {
                    font_sizes: Array.from(fontSizes).sort((a,b)=>a-b),
                    spacings: Array.from(spacings).sort((a,b)=>a-b),
                    colors: Array.from(colors),
                    border_radius_values: Array.from(borderRadii),
                    shadow_values: Array.from(shadows),
                    primary_buttons: primaryButtons,
                    url: location.href
                };
            }"""
        )
        browser.close()
        return data


# --------------------------------------------------------------------------- #
# 基线快照
# --------------------------------------------------------------------------- #


def load_baseline(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_baseline(path: str, snapshot: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(description="visual_check frontend visual regression checker")
    parser.add_argument("--url", required=True, help="页面 URL")
    parser.add_argument("--baseline", help="基线快照路径；缺失时生成基线")
    parser.add_argument("--out", default="visual_report.json", help="输出报告路径")
    parser.add_argument("--screenshot", help="截图保存路径（通道 B 预留）")
    args = parser.parse_args()

    snapshot = extract_dom_snapshot(args.url)

    # 基线处理
    if args.baseline:
        if os.path.exists(args.baseline):
            baseline = load_baseline(args.baseline)
            comparison = {"baseline": baseline, "current": snapshot}
        else:
            save_baseline(args.baseline, snapshot)
            comparison = {"baseline_created": args.baseline, "current": snapshot}
    else:
        comparison = {"current": snapshot}

    result = evaluate_snapshot(snapshot)
    report = {
        "passed": result["passed"],
        "violations": result["violations"],
        "info": result["info"],
        "comparison": comparison,
        "screenshot_path": args.screenshot,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
