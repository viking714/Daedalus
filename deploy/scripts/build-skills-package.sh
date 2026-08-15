#!/bin/bash
# 构建 rd-defect-skills AgentTeams 技能包 ZIP
#
# 用法:   ./build-package.sh [-o output_dir]
# 输出:   deploy/packages/rd-defect-skills-v<VERSION>.zip
#
# 规范对齐:
#   - AgentSkills 通用格式: manifest.json + SKILL.md (+ references/ + scripts/)
#   - AgentTeams/openclaw 要求: SKILL.md 必须含 YAML front matter (name, description)
#   - Worker YAML 通过 spec.package 字段引用此 ZIP 包
#   - 版本号从 manifest.json 读取

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PACKAGE_DIR="$REPO_ROOT/deploy/packages/rd-defect-skills"

# ––– 版本 –––
VERSION=$(python3 -c "import json; print(json.load(open('$PACKAGE_DIR/manifest.json'))['version'])")
OUT_DIR="$REPO_ROOT/deploy/packages"
ZIP_NAME="rd-defect-skills-v${VERSION}.zip"
ZIP_FILE="$OUT_DIR/$ZIP_NAME"

# ––– 仅当源码比产物新时重新打包 –––
if [[ -f "$ZIP_FILE" ]]; then
  NEWEST_SRC=$(find "$PACKAGE_DIR/skills" -type f -name "SKILL.md" -newer "$ZIP_FILE" 2>/dev/null | wc -l)
  if [[ "$NEWEST_SRC" -eq 0 ]]; then
    echo "=== Skills 包已是最新 ($ZIP_NAME)，跳过打包 ==="
    exit 0
  fi
fi

echo "=== 构建 $ZIP_NAME ==="

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# 1. 复制包内容到临时目录
cp -r "$PACKAGE_DIR" "$TMPDIR/rd-defect-skills"
PKG_TMP="$TMPDIR/rd-defect-skills"

# 2. 清理开发残留
find "$PKG_TMP" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$PKG_TMP" -name "*.pyc" -delete 2>/dev/null || true
find "$PKG_TMP" -name ".DS_Store" -delete 2>/dev/null || true

# 3. 验证 SKILL.md 必须有 YAML front matter
echo "  验证 SKILL.md 文件..."
INVALID_COUNT=0
while IFS= read -r -d '' md; do
  first_line=$(head -1 "$md" 2>/dev/null || echo "")
  if [[ "$first_line" != "---" ]]; then
    echo "  [WARN] 缺少 YAML front matter: $(basename "$(dirname "$md")")/SKILL.md"
    INVALID_COUNT=$((INVALID_COUNT + 1))
  fi
done < <(find "$PKG_TMP" -name "SKILL.md" -print0)
echo "  有效 SKILL.md: $(find "$PKG_TMP" -name "SKILL.md" | wc -l) ($INVALID_COUNT 缺少 front matter)"

# 4. 打包 ZIP（保留内部目录结构）
cd "$TMPDIR"
rm -f "$ZIP_FILE"
zip -r "$ZIP_FILE" "rd-defect-skills" -x "*__pycache__*" "*.pyc" ".DS_Store" > /dev/null
echo "  ✅ $ZIP_FILE"

# 5. 输出产物摘要
SIZE=$(ls -lh "$ZIP_FILE" | awk '{print $5}')
SKILL_COUNT=$(find "$PKG_TMP" -name "SKILL.md" | wc -l)
SCRIPT_COUNT=$(find "$PKG_TMP" -name "*.py" -not -path "*__pycache__*" | wc -l)
echo ""
echo "=== 构建完成 ==="
echo "  文件:    deploy/packages/$ZIP_NAME"
echo "  大小:    $SIZE"
echo "  Skills:  $SKILL_COUNT 个 SKILL.md"
echo "  脚本:    $SCRIPT_COUNT 个 .py"
echo "  版本:    v$VERSION"
