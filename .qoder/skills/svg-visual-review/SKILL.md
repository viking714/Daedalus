---
name: svg-visual-review
description: Renders SVG diagrams (architecture, flowcharts) to PNG so the agent can visually inspect them with the image-reading capability, then iteratively adjust layout, spacing, and aesthetics based on what is actually seen. Use when creating or editing SVG diagrams, when the user asks to check/improve how a diagram looks, or when diagram layout feels off after code-level edits.
---

# SVG Visual Review

## Core Idea

Editing SVG as text is blind — coordinates that look reasonable in code may render as overlapping boxes, crossing arrows, or cramped labels. This skill closes the loop:

```
edit SVG code → render to PNG → LOOK at the PNG → adjust → repeat
```

The agent CAN read images. Always render and look before finalizing any diagram.

## Workflow

1. Render the SVG:
   ```bash
   python .qoder/skills/svg-visual-review/scripts/render_svg.py <input.svg> <output.png> [--scale N]
   ```
2. Read the PNG with the file-reading tool (image mode) and evaluate:
   - **Overlap**: boxes, labels, or arrows colliding or touching
   - **Alignment**: elements in the same logical row/column not lining up
   - **Spacing**: uneven gaps, elements crammed against container edges
   - **Flow clarity**: arrows readable, not crossing each other or passing through boxes
   - **Whitespace balance**: large empty regions vs. crowded regions
3. Adjust the SVG source (Edit/SearchReplace on coordinates), re-render, re-read.
4. Iterate until clean. Typical diagrams need 2–3 rounds.
5. Clean up temp PNGs unless the user wants them kept.

## Rendering Script

`scripts/render_svg.py` tries backends in order:
1. **Playwright + headless Chromium** (best fidelity: full CSS/font support)
2. **cairosvg** (fallback, lighter install)

If both fail, run `python -m playwright install chromium` first, or fall back to cairosvg via `python -m pip install cairosvg`.

## Rules

- Never judge a diagram purely from SVG source — always render and look.
- When adjusting, move whole groups (all coordinates of an element together), not just boxes, so attached arrows/labels follow.
- After moving elements, check every arrow endpoint still touches the right element edge.
- Preserve the SVG's existing style conventions (colors, fonts, stroke widths); only fix layout.
- Report each iteration's finding briefly: what was wrong → what changed → re-render result.
