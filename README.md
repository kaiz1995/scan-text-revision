# Scan Text Revision (扫描件、图片文字高保真修改)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Codex Skill](https://img.shields.io/badge/Codex-Skill-green.svg)](https://github.com/openai/codex)

在扫描件、手机拍摄的文档/屏幕照片、或只有图像层的 PDF 上，替换**用户框选的一行文字**。

核心不是「贴一段新字」，而是先测量原行再合成：逐通道二次曲面背景、字形覆盖度、基线、满墨色、笔画宽度、模糊 σ 和噪声都从原图测得；原行里已有的字直接借用原像素，只有缺失的字才用模板匹配出的系统字体合成。每次导出强制过 `qa_gate`，门禁不过就是没完成。

产出是**修订样本 + 审计记录**（`manifest.json` / `edit-log.json` / `qa-report.json` / `review-report.md`），不声称是可作证据的原件。

## 它做什么

- **单行替换**：给定 `x,y,w,h` 和新文字，在测得基线上重新排版（`baseline y = a + b·(x − x0)`），不居中、不拉伸。
- **字形借用**：`--page-text` 提供原行文字后，每个字从原图抠出 alpha 遮蔽（按自身连通域裁剪、外扩 6px），平移到目标位置，因此继承原图的模糊、字重和局部噪声。
- **缺字合成**：识别字体/字号/σ/字重（模板匹配 NCC + 字体 advance 拟合 + 笔画宽校准），4× 超采样渲染后降采样。
- **背景重建**：选区内的旧墨被「拟合背景面 + 借用洁净区残差噪声」替换；噪声平铺时对齐 8px 的 JPEG 块相位。
- **强制门禁**：12 项检查（选区外像素 ×2、接缝台阶、满墨色亮度、墨色色度 ΔE、笔画宽、字高、数字基线、CJK 基线偏移、噪声、量化表、OCR 回读），支持 `pass` / `fail` / `skipped` 三态；字高与基线按「数字对数字、CJK 对同形状 CJK」分类比较。
- **编码保真**：JPEG 交付复用源量化表、色度子采样、渐进标志与 DPI；PDF 在有 `pikepdf` 时只替换页面图像 XObject。

## 它不做什么

- 不做版面分析：没有文本块/表格线/水印/印章/签名识别，也没有 `confidence`、`editable` 这类区域属性。
- 不做自动多候选与评分排序：每次调用产出一个候选，没有候选分数。
- 不做云端漂移检测，也不负责把云端候选贴回页面。
- 不测 DPI：PDF 渲染固定 `-r 200`，栅格图只读元数据里的 `dpi` 字段。
- 不给「选区外 100% 不变」的无条件承诺：该结论只对无损 PNG 层成立，JPEG 与 PDF 两种情况见下文。
- 不处理二值扫描件（CCITT G4 / JBIG2）。

## 技术架构与工作流

```text
[输入 PDF/图片]
   │ inspect_scan.py（只读）：SHA-256、格式、PDF 元数据/字体/文本层统计、栅格尺寸与 DPI 元数据
   │                         └─(PDF) pdftoppm 固定 -r 200 → page-*.png
   ▼
[选区] select_box.py + selector.html（单框、像素坐标、裁剪预览）
   │    或直接给 x,y,w,h + 原行文字（--page-text）
   ▼
[候选] render_candidates.py  →  glyph_engine.py
   │  measure              二次曲面背景 B、alpha 覆盖度、基线、满墨色(高光分位的向量中位色)、笔画宽、σ、噪声
   │  build_glyph_library  --page-text 命中的字 → 连通域裁剪的原始字形遮蔽
   │  render               基线排版；借不到的字用识别字体 4× 超采样合成
   │  compose              out = B + A·(I_ink − B)，仅在掩膜内写回；旧墨由 B+噪声场重建
   │  输出 cand.png + cand_mask.png + JSON（borrowed/synthesized、ink、baseline、σ、stroke_px）
   ▼
[人工复核] 原图 / 候选 / 掩膜 200% 比较 → 在 edit-log 里写 approved:true
   ▼
[导出 + 门禁] export_revision.py → qa_gate.run_gate
   │  图：复用源 JPEG 量化表重编码 + 无损 PNG 层
   │  PDF：pikepdf 仅替换页面图像 XObject；缺 pikepdf 则 lossy_fallback
   ▼
[产物] manifest.json / qa-report.json / edit-log.json / review-report.md / original-reference.*
```

> 术语更正：旧版文档写的「Alpha 软羽化贴回」并不存在——旧实现是整块 `paste`，新实现是上面这个覆盖率模型，边缘的柔和来自字形自身的 alpha，不是对矩形补丁做羽化。

## 项目结构

```text
scan-text-revision/
├── SKILL.md                     # Codex Skill 入口规范与调度指令
├── README.md                    # 项目详细说明文档
├── .gitignore
├── agents/
│   └── openai.yaml              # Agent 元数据
├── references/
│   ├── workflow.md              # 详细工作流操作手册
│   ├── input-policy.md          # 使用边界与编辑记录规范
│   ├── quality-checks.md        # 门禁指标与人工复核清单
│   └── cloud-image-adapter.md   # 云端图像模型适配接口
└── scripts/
    ├── inspect_scan.py          # 只读解析：哈希、格式、PDF 元数据/文本层、200 DPI 渲染
    ├── select_box.py            # 本地轻量 Web 服务，承载可视化框选（单框）
    ├── selector.html            # 纯前端 Canvas 单框选区界面
    ├── glyph_engine.py          # 测量 + 字形库 + 基线排版 + 覆盖率合成
    ├── qa_gate.py               # 12 项门禁（数字/CJK 分类比较、含 CIELAB 色差）与 pass/fail/skipped 报告
    ├── render_candidates.py     # 生成一个替换候选（默认走 glyph_engine，--legacy 走旧 inpaint）
    ├── cloud_inpaint.py         # 云端 /v1/images/edits 适配器（返回裁剪候选，不回填）
    └── export_revision.py       # 强制门禁 + 交付组装 + manifest/报告
```

## 安装

这个 skill 没有安装脚本、没有构建步骤，安装就是「把仓库目录放到客户端的 skills 目录下」。

> 约定的 skills 目录：Codex 用 `~/.codex/skills/`，ZCode 用 `~/.zcode/skills/`（Windows 下 `~` 展开为 `%USERPROFILE%`）。目录名保持 `scan-text-revision`，与 `SKILL.md` 的 `name` 字段一致。

### 手动安装

macOS / Linux：

```bash
git clone https://github.com/kaiz1995/scan-text-revision.git ~/.codex/skills/scan-text-revision
python3 -m pip install pillow numpy
```

Windows PowerShell：

```powershell
git clone https://github.com/kaiz1995/scan-text-revision.git "$env:USERPROFILE\.codex\skills\scan-text-revision"
py -m pip install pillow numpy
```

没有 git 或不想用 git 时，下载仓库 zip 解压到该目录、把顶层文件夹改名为 `scan-text-revision` 即可。

安装后自检（两条命令都应打印用法，说明代码与被依赖的 Pillow / numpy 都在位）：

```bash
python ~/.codex/skills/scan-text-revision/scripts/glyph_engine.py --help
python ~/.codex/skills/scan-text-revision/scripts/qa_gate.py --help
```

可选依赖按需补装，缺哪个只影响对应功能，不影响基本使用：`opencv-python`（加速）、`pikepdf`（PDF 无损替换）、`httpx`（云端适配器）、Poppler（PDF 栅格化）、`tesseract`（OCR 回读门禁）。

卸载就是删掉那个目录，没有注册表项或配置文件残留。

### 让 agent 安装

把下面这段直接发给能执行命令的 agent（Codex / ZCode 等）：

```text
请帮我安装 scan-text-revision 这个 skill：

1. 克隆到 skills 根目录：git clone https://github.com/kaiz1995/scan-text-revision.git <skills>/scan-text-revision
   skills 根目录：Codex 用 ~/.codex/skills，ZCode 用 ~/.zcode/skills；Windows 上展开成 %USERPROFILE%。
2. 安装必需依赖：python -m pip install pillow numpy（Windows 上 python 若不可用，用 py）。
3. 自检：分别运行 scripts/glyph_engine.py --help、scripts/qa_gate.py --help、
   scripts/render_candidates.py --help，三条都要成功。任何一条失败，把完整报错贴出来，
   不要安装替代品、不要跳过。
4. 汇报：安装路径、Python 版本、以及 opencv-python / pikepdf / tesseract 是否可用
   （缺失只影响对应功能，请如实说明缺哪些）。
```

装完后在对话里用自然语言触发即可，例如「用 scan-text-revision 把这张图上的日期改掉」，agent 会读取 `SKILL.md` 并按其中的流程执行。

## 环境依赖

```bash
# 必需：Pillow（图像 I/O 与字体渲染）+ numpy（全部数值计算）
pip install pillow numpy

# 可选：OpenCV，启用 cv2 快速路径（高斯/膨胀/连通域/距离变换/模板匹配）
pip install opencv-python

# 可选：仅云端适配器需要
pip install httpx

# 可选：仅 PDF 导出需要（缺失时整页重编码且无法做 PDF 像素门禁）
pip install pikepdf
```

外部工具均为可选：Poppler（`pdftoppm`、`pdfinfo`、`pdffonts`、`pdftotext`）用于 PDF 检查与栅格化；`tesseract` 用于 OCR 回读。

## 快速上手

### 1. 检查输入

```bash
# PDF 会以固定 200 DPI 渲染页面到 ./work
python scripts/inspect_scan.py input.pdf --json inspect.json --render-dir ./work
```

### 2. 框选（可选）

```bash
python scripts/select_box.py ./work/page-1.png --output task.json
```

浏览器会打开单框画布：拖出一个矩形，侧栏实时显示像素 X/Y/W/H、选区裁剪预览，可复制 `--bbox ... --text "..."` 参数或直接提交。

### 3. 生成候选

```bash
python scripts/render_candidates.py ./work/page-1.png \
  --bbox 780,1320,350,36 \
  --page-text "2025年07月30日" --text "2025年08月15日" \
  --output cand.png --mask cand_mask.png --font-id
```

- 不给 `--page-text` 时无法借用字形，所有字都走合成。
- 候选请存 PNG；存 `.jpg` 会以 quality=95 重新编码一次。
- 需要别的版本时改 `--sigma`（模糊）、`--weight`（笔画加粗 px）、`--font`（指定字体文件）再跑一次。

### 4. 预检（可选）

```bash
python scripts/qa_gate.py --source ./work/page-1.png --output cand.png \
  --bbox 2186,1890,190,68 --region 2186,1882,2376,1958 \
  --expected-text "10月13日" --json-out gate.json
```

### 5. 写 edit-log 并导出

每条编辑都必须 `"approved": true` 且带 `bbox`，否则导出直接拒绝。把第 3 步打印的 JSON 放进 `candidate` 字段（至少保留 `glyphs_borrowed` / `glyphs_synthesized`），manifest 才能记录字形来源；缺失时 `char_sources.origin` 会写成 `unknown`。

```json
{
  "edits": [
    {
      "page": 1,
      "bbox": [780, 1320, 350, 36],
      "original_text": "2025年07月30日",
      "replacement_text": "2025年08月15日",
      "approved": true,
      "candidate": {
        "mode": "glyph_engine",
        "glyphs_borrowed": ["2", "0", "5", "年", "月", "日"],
        "glyphs_synthesized": ["8", "1"]
      }
    }
  ]
}
```

```bash
# 图片源
python scripts/export_revision.py --source ./work/page-1.png --revised cand.png \
  --output-dir ./out --edit-log edit-log.json --mask cand_mask.png

# PDF 源（需按页提供已批准的页面图；没有 pikepdf 时必须另给 --source-pages）
python scripts/export_revision.py --source input.pdf \
  --page-images ./out/page-1.png --source-pages ./work/page-1.png \
  --output-dir ./out2 --edit-log edit-log.json --mask cand_mask.png
```

导出会强制运行 `qa_gate`：未通过的编辑仍会写出 `qa-report.json` 与 `review-report.md`，但 `status` 为 `qa_failed` 且退出码非零。

### 6. 云端候选（可选）

```bash
python scripts/cloud_inpaint.py crop.png mask.png \
  --prompt "Replace the masked text with 2025年08月15日. Preserve paper texture and lighting." \
  --output-dir ./candidates --n 1
```

返回的是**裁剪后的候选图**，贴回页面需要调用方自己完成；脚本不做回填、不做漂移检测。详见 `references/cloud-image-adapter.md`。

## 质量门禁

`qa_gate.py` 的 `THRESHOLDS` 就是下面这张表：

字高与基线不是「拿新字底边和邻近某个常数比」：门禁先在被粘连的连通域（如 `3`+`日`）的列投影谷值处切开，再按高度双峰（退化时用宽高比）分成数字与 CJK；数字用源行数字拟合出的基线逐字比较，CJK 只和同形状的源 CJK 比墨顶偏移——数字坐在基线上、CJK 会下沉几像素、整行还有轻微倾斜，这三件事必须分开算。

| 检查 | 算法 | 阈值 / 状态 |
| --- | --- | --- |
| `outside_max_diff` | 与源图逐像素比较，允许区域外的最大通道差 | `== 0` |
| `outside_changed_pixels` | 允许区域外发生变化的像素数 | `== 0` |
| `boundary_step` | 接缝处列/行均值的二阶差分增量 | `< 1.0` |
| `ink_delta` | 字形核心亮度中位数（感知色调；顶部十分位是 JPEG 振铃，重建字形没有，比它等于奖励"更白"） | `< 3` 级 |
| `ink_chroma_delta` | 面积加权墨色 RGB 转 CIELAB 后的 ΔE76，与邻近未改动字比较 | `< 3`；色偏的唯一定量防线 |
| `stroke_delta` | 距离变换中位数 ×2 的笔画宽 | `< 0.5 px` |
| `glyph_height_delta` | 数字对源行数字、CJK 对同形状 CJK（bbox 高度差）取最大 | `< 1 px` |
| `baseline_delta` | 用源行数字拟合 `y = a + b·x`，取每个新数字底边残差的中位数 | `< 1 px`；源数字不足 3 个时 `skipped` |
| `cjk_baseline_offset` | 新 CJK 与同形状源 CJK 的墨顶，相对同一条拟合基线的偏移 | `< 1 px`；没有同形状 CJK 配对或没有数字基线可依时 `skipped` |
| `noise_ratio` | 重建区与邻近背景的高频 std 比 | `0.7 ~ 1.4` |
| `qtables_equal` | 输出 JPEG 量化表 vs 源 | 相等；无损输出且无同级 JPEG 时 `skipped` |
| `ocr_text` | tesseract 回读 vs `expected_text` | 相等；无 tesseract 时 `skipped` |

三条必须如实转述的语义：

1. `skipped` 不等于通过。无 tesseract 时 OCR 项记为 `skipped`，不参与放行判定——它不会让门禁失败，也不代表文字已被机器确认可读。同理，源行数字不足 3 个时 `baseline_delta`、没有可比 CJK 时 `cjk_baseline_offset` 也是 `skipped`，同样不算已验证。
2. 门禁在「找不到可比对的字形」时直接判失败（`glyph metrics: no comparable glyphs ...`），不允许空过。
3. 是否放行取 `pass` 字段：所有检查无 `fail` 且无额外 `failures` 才是 `true`。

## 「选区外不变」成立在哪一层

旧文档给的是无条件承诺，实际分三种情况：

1. **无损 PNG 层（`revised-sample.png`，或无损源格式的 `revised-sample.<ext>`）**：与源图解码结果逐像素比较，选区外 100% 相同，`outside_max_diff == 0`。这是 `qa_gate` 实际测量的对象，也是唯一可以给出 `max|diff| = 0` 的产物。
2. **JPEG 交付（`revised-sample.jpg`）**：复用源量化表、子采样、渐进标志和 DPI 重新编码，未改动区域仍会经历一次再量化。实测 `mean|diff| ≈ 0.000`，但最大单通道差是个位数（不同量化表下实测在 6–8 之间）。它不是 0，只是肉眼不可见。
3. **PDF**：装有 `pikepdf` 时只替换页面图像 XObject，`DCTDecode` 直通，页面尺寸与其余对象不动；**未安装 `pikepdf` 时走 `lossy_fallback`**（Pillow 按 200 DPI 重排整页），无损承诺不适用，manifest 记 `pdf_repack: lossy_fallback`，且源页图像无法解码做像素门禁，除非另给 `--source-pages`。

## 使用边界与合规

本工具测量原图后借用原字形合成，产出可以做到肉眼难辨，因此必须保留可追溯性：

- `manifest.json` 记录 `source_sha256`、`revised_sha256`、每处改动的 `bbox` 与允许触达的 `region`、字形来源（`char_sources.borrowed` = 借用原字形，`char_sources.synthesized` = 字体合成；记录缺失时写明 `unknown`，需人工补齐归属）、以及 `qa.checks` 中逐项门禁的阈值、实测值与状态。`export_revision.py` 同时把源文件复制为 `original-reference.*` 一并留档。
- `review-report.md` 由 `export_revision.py` 依据 `qa_gate` 的真实结果生成，没有写死的结论行；门禁未通过时 `status` 为 `qa_failed`，脚本以非零码退出。
- 身份证件、印章、票据、法律效力文件的编辑，必须先取得用户对「产物为修订样本、不具备原件效力」的显式确认，并在产物目录和交付说明中标注为修订样本（`revised-sample.*`、`manifest.json` 的 `status`、`review-report.md`）。脚本本身不拦截内容类型，拦截责任在流程层，见 `references/input-policy.md`。
- 「无痕」的工程目标是不在编辑区留下可见的拼接痕迹（接缝台阶、字重/墨色/噪声失配），不是消除审计线索。两者分开实现：前者由 `glyph_engine` 与 `qa_gate` 负责，后者由 manifest、edit-log、review-report 负责；不要为「更干净」删改这些记录，也不要手工编辑 `review-report.md` 的门禁结论。

## Roadmap / 未实现

以下能力曾被文档承诺，但当前代码里没有实现；在实现并接入门禁之前，不要当作可用特性引用：

1. **页面分析层 / 版面分析**：文本块、表格线、水印、印章、签名识别，以及区域的 `confidence`、`editable` 属性。`inspect_scan.py` 只输出文件哈希、格式、PDF 元数据/字体/文本层统计、栅格尺寸与 DPI 元数据，不做版面分析。
2. **多候选与自动评分**：旧文档的「最多生成三个候选」「Automatic scores rank candidates」没有对应代码。`render_candidates.py` 每次调用只产出一个候选，JSON 里只有 `status: candidate`，没有 score 字段；需要多个版本时用不同 `--sigma` / `--weight` / `--font` / `--text` 多次运行并人工比较。
3. **云端漂移检测与回填**：`cloud_inpaint.py` 不比较未掩膜区域，也没有把云候选贴回页面的代码。唯一的自动像素比对是导出时的 `qa_gate`。
4. **真实 DPI 推断**：`inspect_scan.py` 固定 `pdftoppm -r 200`；栅格图只读取元数据里的 `dpi` 字段，不做测量，也没有「保留 200+ DPI」的自动逻辑。
5. **Alpha 软羽化贴回**：旧文档描述的「羽化粘贴」不存在。现在的合成是覆盖率模型 `out = B + A·(I_ink − B)`（`glyph_engine.compose`），软边来自字形自身的 alpha，而不是对矩形补丁做羽化。
6. **二值扫描件分支**（CCITT G4 / JBIG2）：未覆盖。
7. **云端适配器的色彩校正、接缝羽化、官方参数枚举对齐**：未实现。
8. **框选 GUI 的放大镜、多框、并排对比**：`selector.html` 仍是单框（该文件未改动）。
9. **PSF 的完整 erf 拟合**：当前 `_estimate_sigma` 用 alpha 0.5 交叉处的最大梯度估计 σ（σ = 0.3989 / max|dA/dx|），不是最小二乘曲线拟合。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
