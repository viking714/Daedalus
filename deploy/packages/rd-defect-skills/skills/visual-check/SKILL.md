---
name: visual-check
version: 0.2.0
description: 四角色共享：前端视觉回归检查，Playwright DOM 结构化提取 + 规则引擎，截图通道预留。
type: with_scripts
roles: [architect, developer, tester, reviewer]
---

# visual-check

## 用途

本技能提供工程化的前端视觉质量判定能力：

- 通道 A：文字化 DOM 提取 + 规则引擎（主力，零模型成本）。
- 通道 B：截图 + VLM 语义判断（本阶段预留接口，不自动判定）。

## 角色调用协议

| 角色 | 触发时机 | 用途 |
|---|---|---|
| Architect | 设计阶段（前端任务） | 提取存量页面视觉基线 |
| Developer | 每次前端改动后、提交前 | 自检 <=3 轮 |
| Tester | 回归测试阶段 | 视觉回归比对 |
| Reviewer | 审查阶段 | 核验 ui_spec / visual_acceptance |

## 可执行阈值

- 对比度：正文 >=4.5:1，大字 >=3:1
- 间距节奏：∈ {4,8,16,24,32,48}，组间距 > 组内间距
- 字号层级：全页 <=3-4 种，正文行高 1.4-1.6
- 色数：主色 <=1 + 中性色 + 语义色
- 对齐：同组元素共用对齐轴
- 层级：每屏 primary button <=1
- 溢出：文本容器声明溢出策略，内容不越界
- 圆角/阴影：圆角 <=2-3 种，阴影 <=2 档

## 执行约定

1. 仅在 `ADD.change_plan` 涉及前端文件或 PRD 含 `visual_acceptance` 时触发。
2. feature 基线优先用 `ui_spec.baseline_sources`；无则首次运行生成。
3. greenfield 基线由脚手架预装设计系统提供。
4. Developer 自检 >3 轮未全过时，附报告正常提交，最终由 Tester/Reviewer 判定。
5. bug 场景不触发 visual_check。

## 脚本

- `scripts/visual_check.py`：DOM 提取、规则引擎、基线快照生成/比对、报告输出。
