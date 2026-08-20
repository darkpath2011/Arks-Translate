# Arks Translate

> 一个保留 PDF 原始版面的英语阅读 + AI 翻译工具，专为英语学习者设计。

Arks Translate 把英语论文 / 文章 PDF 解析为结构化文档（保留标题、段落、表格、图片、句子、单词的精确坐标），再通过大语言模型流式翻译为中文。读者可以在网页上**原位阅读**原文 + 译文，划词查义、句子解析、章节提示，并将带翻译的 PDF 重新导出。

---

## ✨ 核心特性

- **结构保真解析**：基于 PyMuPDF 的 `dict` 与 `words` 通道，提取 block / line / sentence / word 四个粒度的 `bbox`，区分 heading / paragraph / list / table / caption / figure / header / footer。
- **流式 AI 翻译**：通过 SSE 把 block 级别的中文译文**逐段推送**到前端，前端按 `page_id / block_id / sentence_id` 精准刷新；同一页加锁防并发，失败可恢复重试。
- **单词学习状态机**（new → learning → known）：自动累计出现次数，支持「我认识 / 我忘了」标注，结果按 `(paper_id, word)` 持久化。
- **划词查义 + 句子解析**：每个单词、句子都对应独立的 LLM 任务，结果进入 `llm_cache` 避免重复扣费。
- **章节提示**：自动识别 section 标题，生成一句话中文提示，帮助读者快速判断要不要精读。
- **带翻译的 PDF 导出**：保留原 PDF 排版，把中文译文以原字号贴合到原文 `bbox` 内（中文回退到 5pt 保底），导出为单页或全部已翻译页的 PDF。
- **不做 PDF→纯文本→HTML**：刻意保留每一个 `bbox`，未来可实现点击原文 → 跳到对应译文。

---

## 🏗️ 架构

```
                  PDF
                   │
                   ▼
           ┌───────────────┐
           │ pdf_extract   │  PyMuPDF: dict + words → IR (block/line/sentence/word + bbox)
           └───────┬───────┘
                   ▼
        ┌─────────────────────┐
        │ SQLite (db.py)      │  papers / pages / page_translations / words / sentence_helps
        └──────────┬──────────┘
                   ▼
          ┌────────────────┐
          │ FastAPI server │  REST + SSE  (server.py)
          └────────┬───────┘
                   ▼
   ┌───────────────┴───────────────┐
   ▼                               ▼
Page translate (SSE)         Word / Sentence / Section (SSE)
   │                               │
   ▼                               ▼
ai_client.AIClient  ──►  OpenAI-compatible Chat Completions (stream)
                           (默认 MiniMax-M3，可切换任何兼容端点)
```

### 数据模型（schema.sql）

| 表 | 作用 |
|---|---|
| `papers` | 论文元数据（id 由 SHA1 截取 16 位） |
| `sections` | 章节标题 + 起止页 + AI 中文提示 |
| `pages` | 每页的 `blocks_json` 与 `page_json`（含 word/sentence bbox） |
| `page_translations` | 一页翻译的状态机：none → streaming → done / error |
| `words` | 词典（lemma → 中文释义、IPA、词形、助记） |
| `word_state` | 每个 paper 内单词的学习状态（new/learning/known） |
| `sentence_helps` | 句子解析缓存（结构 / 简单英语 / 中文） |
| `llm_cache` | 所有 LLM 调用的 SHA1 缓存 |

---

## 📂 目录结构

```
translate/
├── arks.py            # 启动入口（uvicorn.run）
├── server.py          # FastAPI 路由 + SSE 事件流
├── pdf_extract.py     # PyMuPDF 结构化解析
├── ai_client.py       # OpenAI 兼容流式客户端（兼容 MiniMax-M3）
├── prompts.py         # 翻译 / 单词 / 句子 / 章节提示词
├── db.py              # SQLite 帮助函数 + 缓存
├── schema.sql         # 表结构（CREATE IF NOT EXISTS）
├── pdf_export.py      # 带翻译的 PDF 导出（simfang.ttf）
├── debug_screenshot.py# Playwright 调试脚本
├── prd.md             # 设计与验收标准
├── requirements.txt
├── .arks.env          # API 配置（不入库）
├── static/            # 前端 (index.html / app.js / styles.css)
├── papers/            # 上传的 PDF
├── .cache/            # 渲染缓存（arks.db + pages/PNG）
├── output/pdf/        # 导出的 PDF
└── tmp/               # 临时文件
```

---

## 🚀 快速开始

### 1. 安装依赖

需要 Python 3.10+。

```bash
pip install -r requirements.txt
```

主要依赖：

- `fastapi` + `uvicorn`
- `pymupdf`（PDF 解析与导出）
- `httpx`（异步 SSE 流）
- `python-multipart`（文件上传）

### 2. 配置 LLM

复制一份配置文件并填入你的 API 信息：

```bash
# .arks.env
ARKS_BASE_URL=https://api.minimaxi.com/v1   # 任何 OpenAI 兼容端点
ARKS_API_KEY=sk-...
ARKS_MODEL=MiniMax-M3                       # 或 gpt-4o-mini / claude-...
```

也可以直接用环境变量覆盖：`ARKS_API_KEY=... python arks.py`。

> MiniMax M3 会在响应里附带 `<think>...</think>` 思考链，`ai_client` 会自动剔除，**只在 `base_url` 命中 `minimaxi.com` 时启用 `reasoning_split`**。

### 3. 启动

```bash
python arks.py
```

默认监听 `http://127.0.0.1:8765`。可通过 `ARKS_PORT` / `ARKS_HOST` 覆盖。

启动时如果根目录存在 `csikszentmihalyi_optimalexperience_1989.pdf`，会自动导入为示例论文。

### 4. 打开浏览器

访问 [http://127.0.0.1:8765/](http://127.0.0.1:8765/) 即可看到论文列表 + 阅读器。

---

## 🔌 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/health` | 检查 API key 是否就绪 |
| GET  | `/api/papers` | 列出所有论文（含章节、页数） |
| POST | `/api/papers/import` | 上传 PDF 文件 |
| POST | `/api/papers/import-path` | 通过本地路径导入 PDF |
| GET  | `/api/papers/{id}/toc` | 章节目录 |
| GET  | `/api/papers/{id}/page/{n}` | 单页完整 IR（blocks + bbox + 翻译） |
| GET  | `/api/papers/{id}/page/{n}/image` | 单页 PNG |
| GET  | `/api/papers/{id}/page/{n}/translate` | **SSE**：流式翻译该页所有 block |
| POST | `/api/papers/{id}/page/{n}/export` | 导出已翻译页（`partial` / `full`） |
| GET  | `/api/papers/{id}/section/{sid}/hint` | **SSE**：流式生成章节一句话提示 |
| GET  | `/api/words/lookup?w=&s=` | **SSE**：划词查义 |
| POST | `/api/sentences/help` | **SSE**：句子四件套（关键词 / 结构 / 简英 / 中文） |
| POST | `/api/words/state` | 单词动作：`click` / `know` / `forget` |
| GET  | `/api/words/state?paper_id=&word=` | 读取单词状态 |
| POST | `/api/words/states` | 批量读取一页所有单词状态 |

所有 SSE 端点事件类型：

- `delta` —— LLM token 流
- `blocks` —— 当前页已累积的中文 block 数组
- `replay` —— 缓存命中，直接重放
- `done` —— 结束
- `error` —— 错误（含可读 message）

---

## 🧠 设计取舍

详见 `prd.md`。几个关键决策：

1. **绝不做 PDF → 纯文本 → HTML**。每条内容都带 `bbox`，所以点击原文能精准找到译文。
2. **不要让 AI 重新猜坐标**。`bbox / font / size / page_rect` 全部来自 PyMuPDF，AI 只负责语义（翻译、标题辅助、阅读顺序异常修复）。
3. **翻译粒度是 block，按 batch=4 顺序调 LLM**。MiniMax 的 OpenAI 兼容端点单次最多 2048 token，分批可以保证长页不会整页丢失。
4. **unknown → 宁可保留原 block**，绝不伪造结构。`_extract_page_ir` 里的启发式（heading / list / caption）全部显式可读，未来可逐个替换为更强模型。
5. **本地优先**：SQLite + 本地 PNG 缓存，没有外部存储依赖。

---

## 🧪 自检

启动后访问 `/api/health` 确认 API key；导入 `csikszentmihalyi_optimalexperience_1989.pdf` 后可以验证：

- 标题仍是 heading，正文仍是 paragraph
- 多栏按视觉阅读顺序
- 表格保留为 table block
- 页眉 / 页脚不被混入正文
- 每个 block / sentence 都能定位（bbox）
- 翻译流式逐 block 出现
- 划词 → 词典；点句子 → 解析

`debug_screenshot.py` 用 Playwright 抓 console + 网络日志 + DOM 状态，可用于调试前端。

---

## 🛠️ 常见问题

**Q：导出 PDF 时报 `SimFang font is unavailable`？**
A：`pdf_export.py` 依赖 `C:\Windows\Fonts\simfang.ttf`，目前仅 Windows。Mac / Linux 用户可换成任意 CJK TTF 并修改 `CHINESE_FONT`。

**Q：模型只回 `<think>...</think>`，页面没有任何译文？**
A：缓存里可能存了只有思考链的旧响应。删除 `.cache/arks.db` 中的 `llm_cache` 行，或直接删库重建。`ai_client.stream` 已经会拒绝把空内容当命中。

**Q：长页翻译到一半报 `token limit`？**
A：服务端以 4 个 block 一批调用；如仍撞限，请在 `server.py:_translate_page_events` 把 `batch_size` 调小。

**Q：如何接 Claude / GPT / 其他模型？**
A：把 `ARKS_BASE_URL` 改成对应端点、`ARKS_MODEL` 改成模型名即可。`reasoning_split` 仅在 `minimaxi.com` 时启用。

---

## 📜 许可与声明

本项目用于个人学习与英语阅读研究，请遵守你所用 LLM 服务的使用条款与上传 PDF 的版权要求。