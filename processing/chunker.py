"""
Markdown 感知智能分块器
======================

针对 Docling / MinerU 输出的 Markdown 优化:
  1. 按 heading 层级切分, 每个 chunk 带上 heading 面包屑
  2. 表格不会被截断 (整张表作为一个 chunk)
  3. 图片引用 ![...](...) 不会被丢弃
  4. 法律声明 / disclaimer 可按 heading 过滤
  5. 兼容原版 chunk_text() 接口
"""

import re
from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────
#  数据结构
# ──────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """带元数据的文本块"""
    text: str
    index: int = 0
    page_hint: str = ""                # 页码 (如果能从文本识别)
    heading_path: str = ""             # 面包屑 "Title > Section > Sub"
    has_table: bool = False
    has_image: bool = False
    char_count: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.char_count = len(self.text)

    @property
    def page(self) -> int | None:
        """页码 (int); 未知时为 None。"""
        return int(self.page_hint) if self.page_hint.isdigit() else None


# ──────────────────────────────────────────────────────────
#  主入口: Markdown 感知分块
# ──────────────────────────────────────────────────────────

def chunk_markdown(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    min_chunk_len: int = 50,
    respect_headings: bool = True,
    protect_tables: bool = True,
    heading_prefix: bool = True,
    skip_headings: list[str] | None = None,
    heading_stack: list[tuple[int, str]] | None = None,
) -> list[Chunk]:
    """
    针对 Markdown 文本的智能分块。

    参数:
        chunk_size:       目标块大小 (字符数)
        chunk_overlap:    重叠大小
        min_chunk_len:    最小块长度
        respect_headings: 在 heading 处强制切分
        protect_tables:   保护表格完整性
        heading_prefix:   在每个 chunk 前注入 heading 面包屑
        skip_headings:    跳过的 heading 关键词 (如 ["disclaimer", "appendix"])
        heading_stack:    初始 heading 栈, 会被原地更新; 逐页分块时用来把上一页的标题面包屑带到下一页

    示例:
        from pdf_parser import parse_pdf
        from chunker import chunk_markdown

        doc = parse_pdf("report.pdf")
        chunks = chunk_markdown(doc.text, chunk_size=512,
                                skip_headings=["disclaimer", "risk information"])
    """
    if not text or not text.strip():
        return []

    # Step 1: 按 heading 分段
    sections = _split_by_headings(text, heading_stack) if respect_headings else [("", text)]

    # Step 2: 过滤不需要的 section
    if skip_headings:
        skip_lower = [s.lower() for s in skip_headings]
        sections = [
            (path, content) for path, content in sections
            if not any(kw in path.lower() for kw in skip_lower)
        ]

    # Step 3: 对每个 section 按大小切分
    chunks = []
    for heading_path, section_text in sections:
        sub_chunks = _chunk_section(
            section_text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            min_chunk_len=min_chunk_len,
            protect_tables=protect_tables,
        )
        for chunk_text in sub_chunks:
            prefix = f"[{heading_path}]\n" if (heading_prefix and heading_path) else ""
            full_text = prefix + chunk_text

            chunks.append(Chunk(
                text=full_text,
                index=len(chunks),
                heading_path=heading_path,
                has_table=bool(re.search(r'\|.*\|.*\||<table>', chunk_text)),
                has_image=bool(re.search(r'!\[.*?\]\(.*?\)', chunk_text)),
            ))

    # Step 4: 识别页码
    for chunk in chunks:
        m = re.search(r'\[第(\d+)页\]|<!-- page (\d+) -->|Page (\d+)|page_?(\d+)', chunk.text)
        if m:
            chunk.page_hint = next((g for g in m.groups() if g), "")

    return chunks


def chunk_pages(
    pages: list[tuple[int, str]],
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    min_chunk_len: int = 50,
    **kwargs,
) -> list[Chunk]:
    """
    逐页分块, 每个 chunk 的 page_hint 为其所在页码。

    chunk 不会跨页; 标题面包屑会从上一页延续到下一页。
    """
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    for page_no, page_text in pages:
        for c in chunk_markdown(
            page_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            min_chunk_len=min_chunk_len, heading_stack=stack, **kwargs,
        ):
            c.page_hint = str(page_no)
            c.index = len(chunks)
            chunks.append(c)
    return chunks


def chunk_parsed_pdf(parsed, chunk_size: int = 512, chunk_overlap: int = 64) -> list[Chunk]:
    """
    对 parse_pdf() 的结果分块: 后端提供了分页文本就带页码, 否则回退全文分块 (page 为 None)。
    """
    if getattr(parsed, "pages", None):
        return chunk_pages(parsed.pages, chunk_size, chunk_overlap)
    chunks = chunk_markdown(parsed.text, chunk_size, chunk_overlap)
    for c in chunks:
        c.page_hint = ""  # 正文里偶然出现的 "Page 3" 不是可靠页码
    return chunks


# ──────────────────────────────────────────────────────────
#  兼容入口: 原始 chunk_text 接口
# ──────────────────────────────────────────────────────────

def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    min_chunk_len: int = 50,
) -> list[str]:
    """兼容原始接口 — 返回纯字符串列表。"""
    return [c.text for c in chunk_markdown(
        text, chunk_size=chunk_size,
        chunk_overlap=chunk_overlap, min_chunk_len=min_chunk_len,
    )]


# ──────────────────────────────────────────────────────────
#  内部实现
# ──────────────────────────────────────────────────────────

def _split_by_headings(
    text: str, stack: list[tuple[int, str]] | None = None,
) -> list[tuple[str, str]]:
    """按 Markdown heading 切分 → [(heading_path, text), ...]

    stack 为调用方传入的 heading 栈时会被原地更新 (逐页分块用)。"""
    heading_re = re.compile(r'^(#{1,4})\s+(.+)$', re.MULTILINE)
    sections = []
    if stack is None:
        stack = []
    last_pos = 0

    for m in heading_re.finditer(text):
        if last_pos < m.start():
            seg = text[last_pos:m.start()].strip()
            if seg:
                path = " > ".join(t for _, t in stack)
                sections.append((path, seg))

        level = len(m.group(1))
        title = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        last_pos = m.start()

    tail = text[last_pos:].strip()
    if tail:
        path = " > ".join(t for _, t in stack)
        sections.append((path, tail))

    return sections if sections else [("", text)]


def _chunk_section(
    text: str, chunk_size: int, chunk_overlap: int,
    min_chunk_len: int, protect_tables: bool,
) -> list[str]:
    """将 section 切分为合适大小的 chunks"""
    if len(text) <= chunk_size:
        return [text] if len(text) >= min_chunk_len else []

    blocks = _split_into_blocks(text, protect_tables)
    chunks = []
    current = ""

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        if len(block) > chunk_size:
            if current.strip() and len(current.strip()) >= min_chunk_len:
                chunks.append(current.strip())
            chunks.extend(_split_large_block(block, chunk_size, chunk_overlap, min_chunk_len))
            current = ""
            continue

        if len(current) + len(block) + 1 <= chunk_size:
            current += block + "\n"
        else:
            if len(current.strip()) >= min_chunk_len:
                chunks.append(current.strip())
            overlap = _sentence_overlap(current, chunk_overlap)
            current = overlap + block + "\n"

    if current.strip() and len(current.strip()) >= min_chunk_len:
        chunks.append(current.strip())

    return chunks


def _split_into_blocks(text: str, protect_tables: bool) -> list[str]:
    """
    分逻辑块: 表格整体保留, 其余按空行分段。
    同时保护 Markdown pipe 表格 (|...|) 和 HTML <table> 表格。
    """
    if not protect_tables:
        return re.split(r'\n\s*\n', text)

    # 先把 HTML <table>...</table> 整块提取出来, 用占位符替换
    html_tables = {}
    counter = [0]

    def _replace_html_table(m):
        key = f"__HTML_TABLE_{counter[0]}__"
        html_tables[key] = m.group(0)
        counter[0] += 1
        return f"\n{key}\n"

    text = re.sub(r'<table>.*?</table>', _replace_html_table, text, flags=re.DOTALL)

    # 然后按 Markdown pipe 表格 + 空行分块
    blocks = []
    buf: list[str] = []
    in_table = False

    for line in text.split('\n'):
        # 检查是否是 HTML 表格占位符
        stripped = line.strip()
        if stripped in html_tables:
            if buf:
                joined = '\n'.join(buf).strip()
                if joined:
                    blocks.append(joined)
                buf = []
            blocks.append(html_tables[stripped])
            continue

        is_tbl = bool(re.match(r'\s*\|', line))

        if is_tbl and not in_table:
            if buf:
                joined = '\n'.join(buf).strip()
                if joined:
                    blocks.append(joined)
                buf = []
            in_table = True
            buf.append(line)
        elif is_tbl and in_table:
            buf.append(line)
        elif not is_tbl and in_table:
            blocks.append('\n'.join(buf))
            buf = [line]
            in_table = False
        else:
            if line.strip() == '' and buf:
                joined = '\n'.join(buf).strip()
                if joined:
                    blocks.append(joined)
                buf = []
            else:
                buf.append(line)

    if buf:
        joined = '\n'.join(buf).strip()
        if joined:
            blocks.append(joined)

    return blocks


def _split_large_block(text: str, chunk_size: int, overlap: int, min_len: int) -> list[str]:
    """按句子边界切分超大块"""
    sents = _split_sentences(text)
    chunks = []
    current = ""
    for s in sents:
        if len(current) + len(s) <= chunk_size:
            current += s + " "
        else:
            if len(current.strip()) >= min_len:
                chunks.append(current.strip())
            current = _sentence_overlap(current, overlap) + s + " "
    if current.strip() and len(current.strip()) >= min_len:
        chunks.append(current.strip())
    return chunks


def _sentence_overlap(text: str, max_chars: int) -> str:
    """取末尾完整句子作为重叠"""
    if not text.strip() or max_chars <= 0:
        return ""
    sents = _split_sentences(text)
    if not sents:
        return text[-max_chars:] + "\n"
    result = ""
    for s in reversed(sents):
        if len(result) + len(s) > max_chars:
            break
        result = s + " " + result
    return result.strip() + "\n" if result.strip() else ""


def _split_sentences(text: str) -> list[str]:
    """中英文句子边界切分"""
    return [s.strip() for s in re.split(r'(?<=[。！？.!?])\s*', text) if s.strip()]


# ──────────────────────────────────────────────────────────
#  工具函数
# ──────────────────────────────────────────────────────────

def chunks_to_langchain_docs(chunks: list[Chunk], source: str = "") -> list:
    """Chunk → LangChain Document"""
    from langchain_core.documents import Document
    return [
        Document(
            page_content=c.text,
            metadata={
                "source": source,
                "chunk_index": c.index,
                "heading_path": c.heading_path,
                "page_hint": c.page_hint,
                "has_table": c.has_table,
                "has_image": c.has_image,
                **c.metadata,
            },
        ) for c in chunks
    ]


def filter_chunks(
    chunks: list[Chunk],
    skip_headings: list[str] | None = None,
    min_length: int = 0,
) -> list[Chunk]:
    """
    后处理过滤器。

    示例:
        filtered = filter_chunks(chunks,
            skip_headings=["disclaimer", "risk information"],
            min_length=80)
    """
    result = chunks
    if skip_headings:
        skip_lower = [s.lower() for s in skip_headings]
        result = [c for c in result if not any(s in c.heading_path.lower() for s in skip_lower)]
    if min_length > 0:
        result = [c for c in result if len(c.text) >= min_length]
    for i, c in enumerate(result):
        c.index = i
    return result


def print_chunk_stats(chunks: list[Chunk]):
    """打印分块统计"""
    if not chunks:
        print("没有 chunk")
        return
    lengths = [c.char_count for c in chunks]
    print(f"分块统计:")
    print(f"  总数: {len(chunks)}")
    print(f"  平均: {sum(lengths)/len(lengths):.0f} 字符")
    print(f"  范围: {min(lengths)} ~ {max(lengths)} 字符")
    print(f"  含表格: {sum(1 for c in chunks if c.has_table)}")
    print(f"  含图片: {sum(1 for c in chunks if c.has_image)}")


# ──────────────────────────────────────────────────────────
#  测试
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_md = """# Higher oil forecasts amid Iran conflict

## CIO View: Crude oil

Giovanni Staunovo, Strategist, UBS Switzerland AG

The conflict in the Middle East has entered its third week, with oil flows
through the Strait of Hormuz still restricted. Oil tanker loading nearly
came to a standstill at the end of last week.

### Brent crude oil forecasts

| Date | Forecast | Spot |
|------|----------|------|
| 16 Mar 26 | - | 104.70 |
| Jun 26 | 90.0 | - |
| Sep 26 | 85.0 | - |

### Supply disruption

Saudi Arabia and the UAE have continued to divert their oil exports
by pipeline to ports outside the Gulf. Exports up 3mbpd over the
last seven days versus the average in January and February.

![Chart: Strait of Hormuz flows](images/chart_1.png)

## What does this mean for oil prices?

The path of least resistance for crude prices remains upward as long
as flows through the Strait are restricted. We are raising our Brent
crude oil forecasts to USD 90/bbl by end-June.

## Appendix

### Risk information

This publication is for your information only and is not intended as an
offer. UBS accepts no liability whatsoever.

### Disclaimer

Tax treatment depends on the individual circumstances.
"""

    print("=" * 60)
    print("测试 1: 正常分块")
    print("=" * 60)
    chunks = chunk_markdown(test_md, chunk_size=300, chunk_overlap=50)
    print_chunk_stats(chunks)
    for c in chunks:
        tag = ""
        if c.has_table: tag += " [table]"
        if c.has_image: tag += " [image]"
        print(f"\n--- Chunk {c.index} [{c.heading_path}]{tag} ---")
        print(c.text[:150])

    print(f"\n\n{'=' * 60}")
    print("测试 2: 过滤法律声明")
    print("=" * 60)
    chunks2 = chunk_markdown(
        test_md, chunk_size=300,
        skip_headings=["risk information", "disclaimer", "appendix"],
    )
    print_chunk_stats(chunks2)
    print(f"过滤前: {len(chunks)} → 过滤后: {len(chunks2)}")