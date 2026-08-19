#!/bin/zsh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CSS="$DIR/.pdf-export.css"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

files=(
  "作品简介.md"
  "方案设计.md"
  "Skill清单.md"
  "AgentIdentity清单.md"
)

for f in "${files[@]}"; do
  src="$DIR/$f"
  base="${f%.md}"
  html="$DIR/.tmp_$base.html"
  pdf="$DIR/$base.pdf"

  echo "→ Exporting $f ..."

  pandoc "$src" \
    -f markdown+smart \
    -t html5 \
    --css "$CSS" \
    --standalone \
    --embed-resources \
    --resource-path="$DIR" \
    -o "$html"

  "$CHROME" \
    --headless=new \
    --disable-gpu \
    --no-pdf-header-footer \
    --print-to-pdf-no-header \
    --run-all-compositor-stages-before-draw \
    --print-to-pdf="$pdf" \
    --hide-scrollbars \
    --no-sandbox \
    "file://$html"

  python3 -c "import os; os.remove('$html')" 2>/dev/null || true
  echo "✓ $pdf"
done
