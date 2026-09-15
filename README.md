# Scan Text Revision (扫描件文字修订)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Codex Skill](https://img.shields.io/badge/Codex-Skill-green.svg)](https://github.com/openai/codex)

一个专注于**高保真、可审计、无痕校正**的扫描件与单层 PDF 文字修订工具 / Codex Skill。

支持对扫描型 PDF、JPG、PNG 图像中的局部文字进行替换，自动保持原图的纸张纹理、底纹（水印/网纹）、扫描质感（JPEG 杂色与模糊度）及字体字形风格，同时严格保证**选区外 100% 像素级保持不变**。

---

## 🌟 核心特性

- 🎯 **像素级选区保护**：仅对用户确认的文字选区进行局部修图与 Alpha 软羽化贴回，选区外的表格线、图章、签名及邻近文字 100% 原样保留。
- 🖥️ **双交互模式**：
  - **语义自动定位**：直接在对话中描述页面与修改项（如“把第 1 页的日期改为 2025 年 08 月 15 日”）。
  - **可视化人工框选**：内置单页面 Web Canvas 交互工具，支持在全尺寸高分辨率原图上用鼠标拉框微操，实时显示像素坐标、局部裁剪预览并一键提交。
- ⚡ **混合修复引擎 (Hybrid Route)**：
  - **本地优先**：纯白底色或普通浅纸色通过 OpenCV / Pillow 本地完成字形测量、背景修复与扫描质感合成。
  - **云端大模型兜底 (gpt-image-2.5)**：对于复杂背景（斜向防伪底纹、微细网格、浅色文字水印），自动将局部带上下文的 Patch 送入云端图像模型进行无痕重绘，消除传统算法的涂抹发白感。
- 🛡️ **安全审计追踪（测试模式，仅限本人使用）**：
  - 源文件可读，测试模式下允许直接覆盖原文件。
  - 输出结果建议输出到独立目录，标注为 `revised-sample`，并可附带 SHA-256 哈希比对清单（`manifest.json`）、修订日志（`edit-log.json`）与审查报告（`review-report.md`）。
  - 所有区域（含印章、签名、防伪编码、证件、票证、法律效力字段）均可编辑。责任由使用者自行承担。

---

## 📐 技术架构与工作流

```text
[输入文件 (PDF/图片)]
       │
       ▼ (只读解析与 SHA-256 记录)
[页面分析层 (inspect_scan.py)] ──> 提取单页图像、判断文本层、保留 200+ DPI
       │
       ▼
[选区定位层] ──┬─ 模式 A: 自然语言语义定位 (LLM 提取目标区域)
               └─ 模式 B: 可视化人工框选 (select_box.py + selector.html)
       │
       ▼
[修复生成层] ──┬─ 简单背景: 本地字体合成与质感匹配 (render_candidates.py)
               └─ 复杂底纹: gpt-image-2.5 局部 Patch 重绘 (cloud_inpaint.py)
       │
       ▼
[质量检测与合成层] ──> 尺寸自适应拉齐 + Alpha 软羽化融合 (选区外像素锁死)
       │
       ▼
[导出交付层 (export_revision.py)] ──> 重新组装多页 PDF / 高清图 + 审计报告
```

---

## 📂 项目结构

```text
scan-text-revision/
├── SKILL.md                 # Codex Skill 入口规范与调度指令
├── README.md                # 项目详细说明文档
├── .gitignore               # 忽略临时文件、凭证与输出产物
├── agents/
│   └── openai.yaml          # Agent 元数据与接口声明
├── references/
│   ├── workflow.md          # 详细工作流操作手册
│   ├── input-policy.md      # 区域可编辑性与安全限制规范
│   ├── quality-checks.md    # 质量门禁与人工复核检查清单
│   └── cloud-image-adapter.md # 云端图像模型适配接口与 Prompt 规范
└── scripts/
    ├── inspect_scan.py      # 扫描件解析、DPI 测量与 PDF 提取
    ├── select_box.py        # 本地轻量 Web 服务，承载可视化框选
    ├── selector.html        # 纯前端 Canvas 鼠标拉框选区界面
    ├── render_candidates.py # 本地背景修复与字形排版渲染
    ├── cloud_inpaint.py     # gpt-image-2.5 / OpenAI 兼容图像重绘适配器
    └── export_revision.py   # 多页 PDF 组装、哈希计算与审查报告导出
```

---

## 🚀 快速上手

### 1. 环境依赖

基础环境要求 Python 3.10+：

```bash
pip install pillow numpy opencv-python httpx pypdf
```

*可选工具：处理扫描 PDF 建议安装 Poppler (`pdftoppm`, `pdfinfo`)。*

### 2. 配置云端模型 (可选)

如需针对复杂纸纹、底纹启用 `gpt-image-2.5` 生成式修图，请在项目根目录或工作目录创建 `.env` 文件：

```env
SCAN_REVISION_API_KEY=your-api-key-here
SCAN_REVISION_BASE_URL=https://your-api-base-url.com
SCAN_REVISION_MODEL=gpt-image-2.5
```

### 3. 使用方法

#### 方式 A：作为 Codex Skill 使用（推荐）
将本目录放置在 `~/.codex/skills/scan-text-revision`，在 Codex 对话中直接调用：

> “帮我用 scan-text-revision 修改 D:\测试\报告.pdf 第 1 页：把‘2025年07月30日’修改为‘2025年08月15日’。”

#### 方式 B：启动可视化框选工具
```bash
python scripts/select_box.py path/to/page-1.png --output task.json
```
会自动在浏览器弹出可视化页面，在画面上直接用鼠标拖拽红框，输入新文字并点击提交。

#### 方式 C：命令行直接渲染
```bash
# 本地渲染
python scripts/render_candidates.py input.png --bbox 780,1320,350,36 --text "新文字" --output cand.png --mask mask.png

# 云端 Patch 修复 (用于复杂底纹)
python scripts/cloud_inpaint.py crop.png mask.png --prompt "In the masked region, replace text with: 新文字" --output-dir ./candidates
```

---

## 📄 许可证

本项目采用 [MIT 许可证](LICENSE)。

