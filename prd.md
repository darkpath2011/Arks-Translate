# PDF 文档结构保真解析与网页渲染改造

你正在开发一个 PDF 阅读 + AI 翻译网站。

当前系统已经能够解析 PDF，并且已经有 `pdf_extract.py`、`db.py`、`schema.sql`、`server.py`、AI 翻译等基础模块。

现在需要解决一个核心问题：

**PDF 原文件具有完整排版和视觉结构，但上传到网站后被转换成了近似纯文本/简单文本块，导致标题、段落、表格、图片、公式、多栏、列表、脚注等结构丢失，用户阅读时非常混乱。**

不要简单地“优化 CSS”。

需要从 PDF 解析层开始重新设计，使网站能够尽可能保留原 PDF 的文档结构和空间布局。

---

## 一、核心目标

目标不是把 PDF 转换成纯文本。

目标是：

```text
PDF
 ↓
结构化解析
 ↓
Document
 ├── Page
 │    ├── Block
 │    │    ├── Paragraph
 │    │    ├── Heading
 │    │    ├── List
 │    │    ├── Table
 │    │    ├── Figure
 │    │    ├── Equation
 │    │    ├── Caption
 │    │    ├── Footnote
 │    │    ├── Header
 │    │    └── Footer
 │    │
 │    └── bbox / position / style
 │
 ↓
网页渲染器
 ↓
尽可能保持原 PDF 的阅读结构
```

用户在网站阅读 PDF 时，视觉上应该仍然能够理解：

* 哪个是标题
* 哪个是正文
* 哪个是小标题
* 哪些文字属于同一个段落
* 哪些内容属于列表
* 哪些内容属于表格
* 哪些文字是图片说明
* 哪些内容是脚注
* 哪些内容属于页眉页脚
* 哪些内容属于公式
* 多栏 PDF 中文字的阅读顺序是什么
* 每个元素在页面中的实际位置在哪里

---

# 二、绝对不要做的事情

不要：

```python
page.get_text("text")
```

然后把整个页面拼成一个字符串，再交给 AI。

不要把 PDF：

```text
PDF → Plain Text → HTML
```

作为主要解析流程。

不要假设所有 PDF 都只有：

```text
标题
正文
```

不要假设一段文字一定对应一个 block。

不要假设 PDF 的文字读取顺序就是用户视觉上的阅读顺序。

不要因为 AI 翻译方便，就破坏原始文档结构。

---

# 三、建立统一的文档中间表示 IR

请设计一个统一的数据结构，例如：

```json
{
  "document": {
    "id": "...",
    "title": "...",
    "page_count": 10,
    "pages": []
  }
}
```

每个 page：

```json
{
  "page": 1,
  "width": 595.0,
  "height": 842.0,
  "blocks": []
}
```

每个 block 至少包含：

```json
{
  "id": "p1-b12",
  "type": "paragraph",
  "bbox": [72, 120, 520, 180],
  "reading_order": 12,
  "text": "...",
  "lines": [],
  "style": {},
  "children": []
}
```

---

# 四、必须保存 bbox

这是整个系统非常重要的一部分。

每一个可以被用户看到、选择、翻译的元素，都应该尽可能保存：

```text
x0
y0
x1
y1
```

例如：

```json
"bbox": [72, 120, 520, 180]
```

不要只保存：

```json
{
  "text": "Hello world"
}
```

而应该保存：

```json
{
  "text": "Hello world",
  "bbox": [72, 120, 200, 140]
}
```

这样未来才能实现：

```text
用户点击原文
 ↓
知道点击的是哪个 block
 ↓
找到对应翻译
 ↓
在对应位置显示翻译
```

---

# 五、文字必须细化到 sentence / line

不要只有 block。

推荐：

```text
Block
 ↓
Line
 ↓
Sentence
 ↓
Word
```

例如：

```json
{
  "type": "paragraph",
  "bbox": [72, 120, 520, 180],
  "lines": [
    {
      "bbox": [72, 120, 500, 140],
      "text": "Flow is a state of optimal experience."
    },
    {
      "bbox": [72, 142, 520, 162],
      "text": "It occurs when a person is fully involved."
    }
  ],
  "sentences": [
    {
      "text": "Flow is a state of optimal experience.",
      "bbox": [72, 120, 500, 140]
    },
    {
      "text": "It occurs when a person is fully involved.",
      "bbox": [72, 142, 520, 162]
    }
  ]
}
```

如果成本允许，可以进一步保存 word bbox。

---

# 六、标题识别

需要尽可能识别：

```text
title
subtitle
heading1
heading2
heading3
heading4
```

不要单纯依赖文字内容。

应该综合：

* 字体大小
* 字体粗细
* 字体名称
* 文字所在位置
* 与上下内容的间距
* 是否居中
* 是否编号
* 前后文关系
* PDF 原始字体信息

例如：

```json
{
  "type": "heading",
  "level": 2,
  "text": "2. The Experience of Flow",
  "bbox": [72, 200, 350, 225],
  "style": {
    "font_size": 16,
    "bold": true
  }
}
```

---

# 七、段落识别

多个 PDF text block 不一定代表多个语义段落。

需要根据：

* 行间距
* block 间距离
* 首行缩进
* 左右边界
* 字体
* 字号
* 阅读顺序

尽可能合并属于同一个 paragraph 的内容。

同时不要过度合并。

---

# 八、列表

必须支持：

```text
• Item A
• Item B
• Item C
```

以及：

```text
1. Item A
2. Item B
3. Item C
```

以及：

```text
(a) ...
(b) ...
(c) ...
```

数据结构例如：

```json
{
  "type": "list",
  "ordered": true,
  "items": [
    {
      "text": "Item A",
      "bbox": [...]
    },
    {
      "text": "Item B",
      "bbox": [...]
    }
  ]
}
```

前端不要把列表渲染成普通段落。

---

# 九、表格

表格是重点。

不要把：

```text
Name Age Country
Tom 18 China
Bob 20 Japan
```

转换成普通文本。

必须保持：

```json
{
  "type": "table",
  "bbox": [70, 300, 520, 500],
  "rows": [
    {
      "cells": [
        {
          "text": "Name",
          "bbox": [...]
        },
        {
          "text": "Age",
          "bbox": [...]
        },
        {
          "text": "Country",
          "bbox": [...]
        }
      ]
    },
    {
      "cells": [
        {
          "text": "Tom",
          "bbox": [...]
        }
      ]
    }
  ]
}
```

尽可能识别：

* 行
* 列
* 单元格
* rowspan
* colspan
* 表头
* 表格标题
* 表格注释

如果 PDF 中有真正的表格线，可以利用线条信息辅助识别。

如果没有表格线，则综合：

* x 坐标
* y 坐标
* 对齐方式
* 字间距
* 行间距

进行推断。

---

# 十、图片 / Figure

PDF 中的图片不要丢弃。

需要识别：

```json
{
  "type": "figure",
  "bbox": [100, 400, 500, 650],
  "image": "...",
  "caption": "Figure 1. ..."
}
```

如果暂时无法提取图片，也至少保留：

```json
{
  "type": "figure",
  "bbox": [...],
  "caption": "..."
}
```

这样前端至少不会把图片周围的文字错误地拼成正文。

---

# 十一、公式

论文 PDF 中经常存在公式。

不要把公式强行当普通正文。

尽可能识别：

```json
{
  "type": "equation",
  "text": "...",
  "bbox": [...]
}
```

如果 PDF 内有可提取的公式文本，保存原始内容。

如果无法解析，则保留其 bbox，并允许后续 OCR / Math OCR 扩展。

不要为了“文本完整”而破坏公式的位置。

---

# 十二、多栏布局

必须考虑论文常见的：

```text
┌─────────────┬─────────────┐
│             │             │
│   Column 1  │   Column 2  │
│             │             │
│             │             │
└─────────────┴─────────────┘
```

不要简单按照 PDF 内部 object 顺序输出。

需要根据 bbox 判断：

```text
页面
 ↓
检测 column
 ↓
column 1
 ↓
column 2
```

阅读顺序应该尽可能符合人类阅读习惯：

```text
左栏上 → 左栏下 → 右栏上 → 右栏下
```

对于复杂布局，不要硬编码“永远两栏”。

应该设计成可扩展的 layout analysis。

---

# 十三、页眉页脚

需要尽可能识别：

```text
Header
Footer
Page Number
```

例如：

```json
{
  "type": "header",
  "text": "Journal of ...",
  "bbox": [...]
}
```

不要让页眉页脚混入正文翻译。

尤其是：

```text
第 1 页
第 2 页
第 3 页
```

这种页面编号不能被 AI 当成正文翻译。

---

# 十四、引用和脚注

支持：

```text
[1]
[2]
[3]
```

以及页面底部的：

```text
1. Author ...
2. ...
```

需要尽可能识别为：

```json
{
  "type": "footnote"
}
```

不要把脚注和正文合并。

---

# 十五、阅读顺序 reading_order

所有 block 都必须尽可能拥有：

```json
"reading_order": 1
```

例如：

```json
[
  {
    "id": "b1",
    "type": "heading",
    "reading_order": 1
  },
  {
    "id": "b2",
    "type": "paragraph",
    "reading_order": 2
  },
  {
    "id": "b3",
    "type": "figure",
    "reading_order": 3
  },
  {
    "id": "b4",
    "type": "paragraph",
    "reading_order": 4
  }
]
```

这样 AI 翻译和前端显示都可以按照：

```text
reading_order
```

进行处理。

---

# 十六、翻译系统不要破坏原始结构

非常重要：

AI 翻译时不要：

```text
原文 PDF
 ↓
提取所有文字
 ↓
AI 翻译成一大段中文
 ↓
重新显示
```

应该：

```text
Document
 ↓
Block
 ↓
Sentence
 ↓
翻译 sentence
 ↓
保存 translation
```

例如：

```json
{
  "id": "p1-b12-s2",
  "source": "Flow is a state...",
  "translation": "心流是一种状态..."
}
```

原文和译文必须保持一一对应。

---

# 十七、支持流式翻译

翻译结果应该允许：

```text
sentence 1
 ↓
翻译完成
 ↓
立即显示

sentence 2
 ↓
翻译完成
 ↓
立即显示

sentence 3
 ↓
翻译完成
 ↓
立即显示
```

不要等待整页翻译完成后才返回。

API 可以设计成：

```text
page
 ↓
blocks
 ↓
sentence
 ↓
translation delta
```

前端根据：

```text
page_id
block_id
sentence_id
```

精准更新对应内容。

---

# 十八、前端渲染

前端不要简单：

```html
<div>{{ text }}</div>
```

而应该根据：

```text
block.type
```

使用不同 renderer：

```text
HeadingRenderer
ParagraphRenderer
ListRenderer
TableRenderer
FigureRenderer
EquationRenderer
CaptionRenderer
FootnoteRenderer
```

例如：

```text
Heading
 ↓
<h1>/<h2>/<h3>

Paragraph
 ↓
<p>

List
 ↓
<ul>/<ol>

Table
 ↓
<table>

Figure
 ↓
<figure>

Equation
 ↓
公式组件
```

---

# 十九、布局模式

需要设计两种阅读模式。

## 模式 A：重排阅读

适合手机和普通网页：

```text
PDF
 ↓
结构化内容
 ↓
响应式 HTML
```

保留：

* 标题
* 段落
* 表格
* 图片
* 公式
* 列表
* 脚注

但不强求像素级还原 PDF。

---

## 模式 B：PDF 原位阅读

适合桌面端。

根据原 PDF：

```text
page width
page height
bbox
```

进行定位。

例如：

```css
.pdf-page {
    position: relative;
}

.pdf-block {
    position: absolute;
}
```

这样：

```text
PDF 原文位置
      ↓
bbox
      ↓
网页绝对定位
```

可以最大程度还原原始 PDF。

---

# 二十、翻译显示策略

不要简单把中文翻译直接塞到原文中间。

建议提供三种模式：

### 原文

只显示 PDF 原文。

### 双语

```text
Original sentence.

中文翻译。
```

### 沉浸翻译

原文保持原始布局：

```text
Original sentence.
```

译文在下方/侧边显示：

```text
中文翻译
```

并通过视觉样式区分。

---

# 二十一、数据库设计

检查当前 `schema.sql`。

如果当前数据库只有：

```text
page
block
text
```

请扩展为能够支持：

```text
document
page
block
line
sentence
word
table
table_cell
translation
```

至少确保：

```text
document_id
page_id
block_id
sentence_id
```

能够建立稳定关系。

不要把所有结构塞进一个巨大 text 字段。

---

# 二十二、不要过度依赖 AI

PDF 的：

```text
bbox
字体
字号
行
block
表格线
图片
页面尺寸
```

优先使用 PDF parser 获取。

AI 主要负责：

```text
语义判断
段落修复
标题判断辅助
阅读顺序异常判断
翻译
复杂表格语义理解
```

不要让 AI 重新“猜”所有 PDF 坐标。

---

# 二十三、推荐技术路线

如果当前项目使用 Python：

优先研究：

```text
PyMuPDF
```

用于：

* PDF 页面
* text
* words
* blocks
* bbox
* font
* image
* drawing
* page size

表格解析根据当前项目实际情况选择：

```text
PyMuPDF
pdfplumber
Camelot
TableFormer / Layout 类模型
```

不要为了引入更多依赖而引入依赖。

先充分利用当前已有解析器。

---

# 二十四、必须考虑异常 PDF

实现时不要假设 PDF 一定规范。

至少考虑：

* 单栏
* 双栏
* 多栏
* 标题跨行
* 段落跨 block
* 表格
* 无边框表格
* 图片
* 图注
* 公式
* 页眉
* 页脚
* 脚注
* 引用
* 列表
* 扫描 PDF
* 混合文字 + 图片 PDF
* 旋转页面
* 不同页面尺寸
* 横向页面
* 空白页
* 字体缺失
* 字符编码异常

对于无法可靠识别的内容：

**宁愿保留原始 block + bbox，也不要错误地把它合并进正文。**

---

# 二十五、渐进式实现，不要一次重写全部项目

先完成 MVP：

## Phase 1

实现：

```text
PDF
 ↓
Page
 ↓
Block
 ↓
bbox
 ↓
text
 ↓
reading_order
```

确保普通论文 PDF 能够正确显示。

---

## Phase 2

增加：

```text
heading
paragraph
list
table
figure
caption
footnote
```

---

## Phase 3

增加：

```text
sentence bbox
translation mapping
streaming translation
```

---

## Phase 4

增加：

```text
复杂多栏
扫描 PDF
OCR
复杂表格
公式
```

---

# 二十六、最重要的验收标准

完成后，用：

```text
csikszentmihalyi_optimalexperience_1989.pdf
```

进行实际测试。

至少检查：

1. 标题是否还是标题
2. 正文是否还是段落
3. 多栏是否按照正确阅读顺序
4. 表格是否仍然是表格
5. 图片是否仍然存在
6. 图注是否与图片关联
7. 脚注是否没有混入正文
8. 页眉页脚是否没有污染正文
9. 公式是否没有被破坏
10. 每个 block 是否有 bbox
11. 每个 sentence 是否能够定位到原文
12. AI 翻译完成后是否能够精准映射回原文
13. 翻译是否可以流式显示
14. 用户翻页时是否可以提前解析下一页
15. 不同页面布局是否不会互相污染

---

# 二十七、最终目标

最终系统应该形成：

```text
                  PDF
                   │
                   ▼
           ┌───────────────┐
           │ PDF Extractor │
           └───────┬───────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ Document IR         │
        │                     │
        │ Page                │
        │ ├─ Heading          │
        │ ├─ Paragraph        │
        │ ├─ List             │
        │ ├─ Table            │
        │ ├─ Figure           │
        │ ├─ Equation         │
        │ └─ Footnote         │
        └──────────┬──────────┘
                   │
          ┌────────┴────────┐
          ▼                 ▼
   Original Renderer    Translation Engine
          │                 │
          │                 ▼
          │          sentence translation
          │                 │
          │                 ▼
          │          translation mapping
          │                 │
          └────────┬────────┘
                   ▼
             Web Renderer
                   │
                   ▼
        ┌────────────────────┐
        │ 原文 + AI 翻译      │
        │                    │
        │ 保持原始结构        │
        │ 支持流式翻译        │
        │ 支持精准定位        │
        └────────────────────┘
```

**请先阅读并理解当前项目的 `pdf_extract.py`、`db.py`、`schema.sql`、`server.py` 以及前端代码。**

不要直接大规模重写。

先分析当前实现：

1. 当前 PDF 是如何提取文字的
2. 当前 block 是如何生成的
3. 当前 table 是如何生成的
4. 当前数据库如何保存 block
5. 当前前端如何渲染 block
6. 当前 AI 翻译如何关联文本

然后提出最小改造方案。

优先保证：

**原始结构不丢失 > 文本完整 > 翻译功能 > 性能优化。**

如果当前实现已经有部分结构化能力，不要重复实现，而是在现有架构上扩展。

完成修改后，使用：

```text
csikszentmihalyi_optimalexperience_1989.pdf
```

实际运行测试，并输出：

* 解析出的页面数量
* block 数量
* 各 block 类型数量
* table 数量
* figure 数量
* heading 数量
* 每页是否存在 bbox
* sentence 是否可以定位
* 数据库是否正确保存
* 前端是否可以按照结构正确渲染

如果发现某种 PDF 元素无法可靠识别，不要伪造结构，保留原始 block 和 bbox，并在代码中留下清晰的扩展点。
