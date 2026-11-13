原始 PDF (39页, 双栏, 表格, 图表, 德文)
       │
       ▼
  parse_pdf(backend="mineru-local")
       │
       │  MinerU 内部:
       │  YOLO 布局检测 → 识别标题/正文/表格/图片区域
       │  PaddleOCR     → 识别文字
       │  UniMERNet      → 识别公式
       │  → 输出 .md 文件 + images/ 文件夹 + content_list.json
       │
       ▼
  ParsedDocument(
      text = "# Equity Preferences\n\n## CIO View...\n\n<table>...</table>\n\n![](images/xxx.png)\n\n...",
      images = [ParsedImage(...), ...],     # 351 张
      tables = [ParsedTable(...), ...],     # HTML 表格
  )
       │
       │  doc.text 就是一个很长的 Markdown 字符串
       │
       ▼
  chunk_markdown(doc.text, chunk_size=512, skip_headings=["disclaimer"])
       │
       │  Layer 1: 按 # ## ### 切成 sections
       │  Layer 2: 每个 section 内按段落/表格边界累加
       │  Layer 3: 超大段落按句子切
       │
       ▼
  [Chunk(text="[Equity Preferences > Most Preferred]\n<table>...", has_table=True),
   Chunk(text="[Equity Preferences > Ausblick]\nDie KI-Wert...", has_table=False),
   ...]
       │
       ▼
  喂入 embedding 模型 → 向量库 → RAG 检索