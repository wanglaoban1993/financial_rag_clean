"""
多模态 PDF 解析器 — 自动适配 VLM / Standard Pipeline
=====================================================

自动探测逻辑:
  Docling:
    1. 检测 vLLM 是否可用 + GPU VRAM 是否够 → VLM Pipeline (最高精度)
    2. 检测 CUDA / MPS 是否可用           → Standard Pipeline + GPU 加速
    3. 都不满足                            → Standard Pipeline CPU

  MinerU:
    1. 检测 GPU VRAM >= 16GB              → hybrid 后端 (VLM + pipeline)
    2. 否则                                → pipeline 后端 (有 CUDA 自动 GPU 加速)

安装:
  # 必装
  pip install pymupdf pymupdf4llm

  # Docling (Standard Pipeline, CPU/GPU 都行)
  pip install docling
  # GPU 版 PyTorch:
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

  # Docling VLM (可选, 精度更高)
  pip install vllm
  # 然后启动: vllm serve ibm-granite/granite-docling-258M --port 8000

  # MinerU
  uv pip install -U "mineru[core]"       # pipeline 后端 (有 CUDA 自动加速)
  uv pip install -U "mineru[all]"        # 全部后端 (含 hybrid/vlm, 需 16GB+ VRAM)
"""

import json
import os
import subprocess
import time
import warnings
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal
from functools import lru_cache


# ══════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════

@dataclass
class ParsedImage:
    """提取的图片"""
    page_num: int
    path: str
    caption: str = ""
    description: str = ""


@dataclass
class ParsedTable:
    """提取的表格"""
    page_num: int
    content: str                       # Markdown 格式
    caption: str = ""


@dataclass
class ParsedDocument:
    """统一解析结果"""
    text: str                          # 全文 Markdown
    filename: str
    title: str = ""
    author: str = ""
    page_count: int = 0
    source_path: str = ""
    backend_used: str = ""             # 实际使用的后端
    pipeline_mode: str = ""            # 实际使用的 pipeline 模式
    images: list[ParsedImage] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    content_blocks: list[dict] = field(default_factory=list)
    pages: list[tuple[int, str]] = field(default_factory=list)  # [(页码, 该页 Markdown)], 仅部分后端提供


# ══════════════════════════════════════════════════════════
#  环境探测
# ══════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def detect_gpu_env() -> dict:
    """
    一次性探测当前 GPU 环境, 结果缓存。

    返回:
        {
            "has_cuda": bool,
            "has_mps": bool,
            "gpu_name": str,
            "vram_gb": float,        # VRAM 总量 (GB)
            "vram_free_gb": float,   # VRAM 可用 (GB)
            "cuda_version": str,
        }
    """
    info = {
        "has_cuda": False,
        "has_mps": False,
        "gpu_name": "",
        "vram_gb": 0.0,
        "vram_free_gb": 0.0,
        "cuda_version": "",
    }

    try:
        import torch
        info["has_cuda"] = torch.cuda.is_available()
        info["has_mps"] = hasattr(torch.backends, "mps") and torch.backends.mps.is_available()

        if info["has_cuda"]:
            info["gpu_name"] = torch.cuda.get_device_name(0)
            #info["vram_gb"] = torch.cuda.get_device_properties(0).total_mem / (1024**3)
            info["vram_gb"] = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            info["vram_free_gb"] = torch.cuda.mem_get_info(0)[0] / (1024**3)
            info["cuda_version"] = torch.version.cuda or ""
    except Exception as e:
        # 例如 WinError 1455 (页面文件不足) 时, torch 导入会失败。
        # 保持程序可用: 直接按 CPU 环境处理。
        warnings.warn(f"GPU 环境探测失败, 回退 CPU: {type(e).__name__}: {e}")
        pass

    return info


@lru_cache(maxsize=1)
def detect_vllm_server(url: str = "http://127.0.0.1:8000") -> dict:
    """
    检测本地 vLLM 服务是否运行中。

    返回:
        {
            "available": bool,
            "url": str,
            "models": list[str],
        }
    """
    result = {"available": False, "url": url, "models": []}

    try:
        import urllib.request
        req = urllib.request.Request(
            f"{url}/v1/models",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            models = [m["id"] for m in data.get("data", [])]
            if models:
                result["available"] = True
                result["models"] = models
    except Exception:
        pass

    return result


def print_env_report():
    """打印当前环境探测报告 (调试用)"""
    gpu = detect_gpu_env()
    vllm = detect_vllm_server()

    print("═" * 50)
    print("  环境探测报告")
    print("═" * 50)

    if gpu["has_cuda"]:
        print(f"  GPU:  {gpu['gpu_name']}")
        print(f"  VRAM: {gpu['vram_gb']:.1f} GB (可用 {gpu['vram_free_gb']:.1f} GB)")
        print(f"  CUDA: {gpu['cuda_version']}")
    elif gpu["has_mps"]:
        print(f"  GPU:  Apple Silicon (MPS)")
    else:
        print(f"  GPU:  无")

    if vllm["available"]:
        print(f"  vLLM: running ({vllm['url']})")
        print(f"        模型: {', '.join(vllm['models'])}")
    else:
        print(f"  vLLM: not detected")

    # 推荐方案
    print("─" * 50)
    docling_mode = _decide_docling_mode()
    mineru_mode = _decide_mineru_backend()
    gpu_accel = "GPU 加速" if (gpu["has_cuda"] or gpu["has_mps"]) else "纯 CPU"
    print(f"  Docling 推荐: {docling_mode}")
    print(f"  MinerU 推荐: {mineru_mode} ({gpu_accel})")
    print("═" * 50)


def _decide_docling_mode(vllm_url: str = "http://127.0.0.1:8000") -> str:
    """
    决定 Docling 使用哪种 pipeline:
      "vlm"       → vLLM 服务可用, 走 VLM Pipeline (最高精度)
      "gpu"       → CUDA/MPS 可用, 走 Standard Pipeline + GPU 加速
      "cpu"       → 纯 CPU
    """
    vllm = detect_vllm_server(vllm_url)
    if vllm["available"]:
        return "vlm"

    gpu = detect_gpu_env()
    if gpu["has_cuda"] or gpu["has_mps"]:
        return "gpu"

    return "cpu"


def _decide_mineru_backend() -> str:
    """
    决定 MinerU 使用哪种后端:
      "hybrid"    → GPU VRAM >= 16GB (VLM + pipeline 混合, 精度最高)
      "pipeline"  → 其他情况 (有 CUDA 自动 GPU 加速布局/OCR/公式, ~4-6GB VRAM 即可)
    """
    gpu = detect_gpu_env()
    if gpu["has_cuda"] and gpu["vram_gb"] >= 15.0:
        return "hybrid"
    return "pipeline"


# ══════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════

def _get_pdf_meta(pdf_path: str) -> dict:
    """PyMuPDF 提取元数据"""
    import fitz
    doc = fitz.open(pdf_path)
    meta = {
        "page_count": len(doc),
        "title": doc.metadata.get("title", "") or Path(pdf_path).stem,
        "author": doc.metadata.get("author", ""),
    }
    doc.close()
    return meta


def _extract_images_pymupdf(pdf_path: str, output_dir: Path) -> list[ParsedImage]:
    """用 PyMuPDF 提取嵌入图片 (所有后端通用)"""
    images = []
    try:
        import fitz
        img_dir = output_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)

        doc = fitz.open(pdf_path)
        for page_num, page in enumerate(doc):
            for img_idx, img in enumerate(page.get_images()):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha > 3:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    # 跳过太小的图 (< 5KB, 多为装饰元素)
                    if pix.width * pix.height < 2500:
                        continue
                    path = img_dir / f"page{page_num+1}_img{img_idx+1}.png"
                    pix.save(str(path))
                    images.append(ParsedImage(page_num=page_num+1, path=str(path)))
                except Exception:
                    pass
        doc.close()
    except Exception as e:
        print(f"[图片提取] 失败: {e}")

    return images


def _clean_latex_artifacts(text: str) -> str:
    """
    清理 MinerU pipeline 后端常见的 LaTeX 公式残留。

    例:
      "$2 5 \\%$"     → "25%"
      "$0{,}5{\\mathsf{Pp}}$" → "0,5 Pp."
      "$30 \\%$"      → "30%"
    """
    import re

    # 移除 $ ... $ 包裹, 清理内部 LaTeX 命令
    def _clean_inline_latex(match):
        inner = match.group(1)
        # 移除 \mathsf{...}, \mathrm{...}, \text{...} 等, 保留内容
        inner = re.sub(r'\\(?:mathsf|mathrm|text|textbf|textit)\s*\{([^}]*)\}', r'\1', inner)
        # 移除 {,} → ,  和 {} → 空
        inner = inner.replace('{,}', ',').replace('{ }', ' ')
        inner = re.sub(r'\{([^}]*)\}', r'\1', inner)
        # \% → %
        inner = inner.replace('\\%', '%')
        # 移除多余空格 (如 "2 5 %" → "25%")
        inner = re.sub(r'(\d)\s+(\d)', r'\1\2', inner)
        inner = re.sub(r'(\d)\s+(%)', r'\1\2', inner)
        # 清理其他常见 LaTeX
        inner = inner.replace('\\,', ' ').replace('\\;', ' ')
        inner = re.sub(r'\\[a-zA-Z]+', '', inner)  # 移除剩余 \command
        return inner.strip()

    text = re.sub(r'\$([^$]+)\$', lambda m: _clean_inline_latex(m), text)
    return text


def _extract_html_tables_from_md(md_text: str) -> list:
    """
    从 Markdown 中提取 <table>...</table> HTML 表格。
    MinerU pipeline 后端常把表格渲染为 HTML 而非 Markdown 表格。
    """
    import re
    tables = []
    for match in re.finditer(r'<table>.*?</table>', md_text, re.DOTALL):
        tables.append(ParsedTable(
            page_num=0,
            content=match.group(0),
        ))
    return tables


# ══════════════════════════════════════════════════════════
#  后端 1: Docling (自动选择 VLM / Standard+GPU / CPU)
# ══════════════════════════════════════════════════════════

def parse_with_docling(
    pdf_path: str,
    output_dir: str = "./docling_output",
    force_mode: str | None = None,
    vllm_url: str = "http://127.0.0.1:8000",
    vllm_model: str = "ibm-granite/granite-docling-258M",
    vllm_concurrency: int = 16,
    ocr_batch_size: int = 2,
    layout_batch_size: int = 2,
    extract_images: bool = True,
) -> ParsedDocument:
    """
    Docling 自适应解析。

    自动探测:
      1. vLLM 服务可用 → VLM Pipeline (最高精度)
      2. CUDA/MPS 可用 → Standard Pipeline + GPU 加速
      3. 都没有         → Standard Pipeline CPU

    参数:
        force_mode: 强制指定模式 "vlm" / "gpu" / "cpu" (跳过自动探测)
        vllm_url: vLLM 服务地址
        vllm_model: VLM 模型名
        vllm_concurrency: VLM 并发数
        ocr_batch_size: Standard Pipeline OCR batch (GPU 时有效)
        layout_batch_size: Standard Pipeline 布局 batch

    安装 VLM (可选):
        pip install vllm
        vllm serve ibm-granite/granite-docling-258M \\
          --host 127.0.0.1 --port 8000 \\
          --max-num-seqs 256 --gpu-memory-utilization 0.5
    """
    from docling.document_converter import DocumentConverter, InputFormat, PdfFormatOption

    pdf_path = str(Path(pdf_path).resolve())
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- 决定模式 ---
    mode = force_mode or _decide_docling_mode(vllm_url)

    # --- 构建 converter ---
    pipeline_cls = None
    pipeline_options = None

    if mode == "vlm":
        from docling.pipeline.vlm_pipeline import VlmPipeline
        pipeline_cls = VlmPipeline
        pipeline_options = _build_docling_vlm_config(
            vllm_url, vllm_model, vllm_concurrency
        )
        print(f"[Docling] VLM Pipeline (vLLM @ {vllm_url})")

    elif mode == "gpu":
        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
        pipeline_cls = StandardPdfPipeline
        pipeline_options = _build_docling_gpu_config(
            ocr_batch_size, layout_batch_size
        )
        gpu = detect_gpu_env()
        print(f"[Docling] Standard Pipeline + GPU ({gpu['gpu_name']})")

    else:
        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
        pipeline_cls = StandardPdfPipeline
        pipeline_options = _build_docling_gpu_config(ocr_batch=8, layout_batch=8)
        print(f"[Docling] Standard Pipeline (CPU)")

    def _make_converter(_pipeline_cls, _pipeline_options):
        # 新版 docling: DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(...)})
        # 旧版 docling: DocumentConverter(pipeline_options=...)
        try:
            pdf_option = PdfFormatOption(
                pipeline_cls=_pipeline_cls,
                pipeline_options=_pipeline_options,
            )
            return DocumentConverter(format_options={InputFormat.PDF: pdf_option})
        except TypeError:
            return DocumentConverter(pipeline_options=_pipeline_options)

    converter = _make_converter(pipeline_cls, pipeline_options)

    # --- 解析 ---
    print(f"[Docling] 解析: {Path(pdf_path).name}")
    try:
        result = converter.convert(pdf_path)
    except Exception as e:
        # GPU 模式偶发 bad_alloc / OOM 时, 自动降级到 Docling CPU 以提高可用性。
        msg = str(e).lower()
        if mode == "gpu" and (
            "bad_alloc" in msg
            or "out of memory" in msg
            or "cuda" in msg
            or "cudnn" in msg
        ):
            print(f"[Docling][WARN] GPU 解析失败, 自动降级 CPU 重试: {type(e).__name__}: {e}")
            from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
            cpu_options = _build_docling_gpu_config(ocr_batch=1, layout_batch=1)
            converter = _make_converter(StandardPdfPipeline, cpu_options)
            mode = "cpu"
            result = converter.convert(pdf_path)
        else:
            raise
    md_text = result.document.export_to_markdown()
    pages = _export_docling_pages(result.document)

    # --- 提取表格 ---
    tables = _extract_docling_tables(result.document)

    # --- 提取图片 ---
    images = _extract_images_pymupdf(pdf_path, out) if extract_images else []

    meta = _get_pdf_meta(pdf_path)
    return ParsedDocument(
        text=md_text,
        filename=Path(pdf_path).name,
        title=meta["title"],
        author=meta["author"],
        page_count=meta["page_count"],
        source_path=pdf_path,
        backend_used="docling",
        pipeline_mode=mode,
        images=images,
        tables=tables,
        pages=pages,
    )


def _build_docling_vlm_config(url: str, model: str, concurrency: int) -> dict:
    """构建 Docling VLM Pipeline 配置"""
    from docling.datamodel.pipeline_options import VlmPipelineOptions
    from docling.datamodel.settings import settings

    settings.perf.page_batch_size = max(concurrency, 4)

    vlm_options = VlmPipelineOptions(
        enable_remote_services=True,
        vlm_options={
            "url": f"{url}/v1/chat/completions",
            "params": {"model": model, "max_tokens": 4096},
            "concurrency": concurrency,
            "prompt": "Convert this page to docling.",
            "timeout": 90,
        },
    )
    return vlm_options


# def _build_docling_gpu_config(ocr_batch: int, layout_batch: int) -> dict:
#     """构建 Docling Standard Pipeline + GPU 配置"""
#     config = {}
#     try:
#         from docling.datamodel.accelerator_options import (
#             AcceleratorDevice, AcceleratorOptions,
#         )
#         from docling.datamodel.pipeline_options import ThreadedPdfPipelineOptions

#         config["accelerator_options"] = AcceleratorOptions(
#             device=AcceleratorDevice.AUTO,
#         )
#         config["pipeline_options"] = ThreadedPdfPipelineOptions(
#             ocr_batch_size=ocr_batch,
#             layout_batch_size=layout_batch,
#             table_batch_size=4,
#         )
#     except ImportError:
#         warnings.warn("Docling GPU 模块不可用, 回退 CPU")

#     return config
def _build_docling_gpu_config(ocr_batch: int, layout_batch: int) -> dict:
    """构建 Docling Standard Pipeline + GPU 配置（兼容不同版本）"""
    try:
        from docling.datamodel.pipeline_options import ThreadedPdfPipelineOptions

        pipeline_options = ThreadedPdfPipelineOptions(
            ocr_batch_size=ocr_batch,
            layout_batch_size=layout_batch,
            table_batch_size=4,
        )
        return pipeline_options

    except ImportError:
        warnings.warn("Docling GPU 模块不可用, 回退 CPU")
        return None

def _export_docling_pages(doc) -> list[tuple[int, str]]:
    """按页导出 Markdown → [(页码, 文本), ...], 供分块时记录页码。失败返回 [] (调用方回退到全文)。"""
    pages = []
    try:
        for page_no in sorted(doc.pages):
            md = doc.export_to_markdown(page_no=page_no)
            if md.strip():
                pages.append((page_no, md))
    except Exception as e:
        print(f"[Docling][WARN] 按页导出失败, 回退全文分块 (无页码): {type(e).__name__}: {e}")
        return []
    return pages


def _extract_docling_tables(doc) -> list[ParsedTable]:
    """从 Docling Document 中提取表格"""
    tables = []
    try:
        for item in doc.iterate_items():
            node = item[0] if isinstance(item, tuple) else item
            if "table" in type(node).__name__.lower():
                md = ""
                if hasattr(node, "export_to_markdown"):
                    md = node.export_to_markdown()
                elif hasattr(node, "text"):
                    md = node.text
                if md:
                    tables.append(ParsedTable(
                        page_num=getattr(node, "page_no", 0),
                        content=md,
                        caption=getattr(node, "caption", ""),
                    ))
    except Exception:
        pass
    return tables


# ══════════════════════════════════════════════════════════
#  后端 2: MinerU (自动选择 hybrid / pipeline)
# ══════════════════════════════════════════════════════════

def parse_with_mineru_local(
    pdf_path: str,
    output_dir: str = "./mineru_output",
    force_backend: str | None = None,
    lang: str = "en",
) -> ParsedDocument:
    """
    MinerU 自适应解析。

    自动探测:
      GPU VRAM >= 16GB → hybrid 后端 (VLM + pipeline 混合, 精度最高)
      GPU VRAM < 16GB  → pipeline 后端 (YOLO + PaddleOCR + UniMERNet, 有 CUDA 自动加速)
      无 GPU            → pipeline 后端 (纯 CPU)

    参数:
        force_backend: 强制 "hybrid" / "vlm" / "pipeline"
        lang: OCR 语言 "en" / "ch" / "ch_server"

    安装:
        uv pip install -U "mineru[all]"     # 全功能 (推荐)
        uv pip install -U "mineru[core]"    # 仅 pipeline
        mineru-models-download
    """
    pdf_path = str(Path(pdf_path).resolve())
    output_dir = str(Path(output_dir).resolve())
    os.makedirs(output_dir, exist_ok=True)

    # 决定后端
    backend = force_backend or _decide_mineru_backend()

    cmd = ["mineru", "-p", pdf_path, "-o", output_dir, "-b", backend, "--lang", lang]

    gpu = detect_gpu_env()
    if backend == "hybrid":
        print(f"[MinerU] hybrid 后端 ({gpu['gpu_name']}, {gpu['vram_gb']:.0f}GB VRAM)")
    elif backend == "vlm":
        print(f"[MinerU] vlm 后端 ({gpu['gpu_name']})")
    elif gpu["has_cuda"]:
        print(f"[MinerU] pipeline 后端 + GPU 加速 ({gpu['gpu_name']})")
    elif gpu["has_mps"]:
        print(f"[MinerU] pipeline 后端 + MPS 加速")
    else:
        print(f"[MinerU] pipeline 后端 (纯 CPU)")

    print(f"[MinerU] 解析: {Path(pdf_path).name}")
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600,
        encoding="utf-8", errors="replace",  # 修复 Windows cp1252 编码错误
    )

    if result.returncode != 0:
        # hybrid 失败时自动降级到 pipeline
        if backend != "pipeline":
            print(f"[MinerU][WARN] {backend} 失败, 降级到 pipeline")
            cmd_fallback = [
                "mineru", "-p", pdf_path, "-o", output_dir,
                "-b", "pipeline", "--lang", lang,
            ]
            result = subprocess.run(
                cmd_fallback, capture_output=True, text=True, timeout=600,
                encoding="utf-8", errors="replace",
            )
            backend = "pipeline"
            if result.returncode != 0:
                raise RuntimeError(f"MinerU 解析失败:\n{result.stderr[:500]}")
        else:
            raise RuntimeError(f"MinerU 解析失败:\n{result.stderr[:500]}")

    # 读取输出
    stem = Path(pdf_path).stem
    md_files = list(Path(output_dir).rglob(f"{stem}*.md"))
    if not md_files:
        md_files = list(Path(output_dir).rglob("*.md"))
    if not md_files:
        raise FileNotFoundError("未找到 Markdown 输出")

    md_text = md_files[0].read_text("utf-8")

    # 清理 LaTeX 公式残留 (MinerU pipeline 常见问题)
    md_text = _clean_latex_artifacts(md_text)

    # 图片
    images = []
    for img_dir in Path(output_dir).rglob("images"):
        for f in sorted(img_dir.glob("*")):
            if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                images.append(ParsedImage(page_num=0, path=str(f)))

    # content_list.json
    content_blocks = []
    tables = []
    for cl in Path(output_dir).rglob("content_list.json"):
        content_blocks = json.loads(cl.read_text("utf-8"))
        tables = [
            ParsedTable(
                page_num=item.get("page_idx", 0),
                content=item.get("content", ""),
                caption=item.get("title", ""),
            )
            for item in content_blocks if item.get("type") == "table"
        ]
        break

    # 兜底: 从 Markdown 中提取 HTML <table> (MinerU pipeline 常把表格渲染为 HTML)
    if not tables:
        tables = _extract_html_tables_from_md(md_text)

    meta = _get_pdf_meta(pdf_path)

    # 构建详细的 pipeline_mode 描述
    if backend == "pipeline":
        if gpu["has_cuda"]:
            mode_desc = "pipeline+gpu"
        elif gpu["has_mps"]:
            mode_desc = "pipeline+mps"
        else:
            mode_desc = "pipeline+cpu"
    else:
        mode_desc = backend

    return ParsedDocument(
        text=md_text,
        filename=Path(pdf_path).name,
        title=meta["title"],
        author=meta["author"],
        page_count=meta["page_count"],
        source_path=pdf_path,
        backend_used="mineru-local",
        pipeline_mode=mode_desc,
        images=images,
        tables=tables,
        content_blocks=content_blocks,
    )


# ══════════════════════════════════════════════════════════
#  后端 3: MinerU 云端 API
# ══════════════════════════════════════════════════════════

def parse_with_mineru_cloud(
    pdf_path: str,
    api_token: str | None = None,
    model_version: str = "vlm",
    poll_interval: int = 5,
    max_wait: int = 300,
) -> ParsedDocument:
    """
    MinerU 云端 — 服务器有 GPU+VLM, 本地零要求。

    获取 Token: https://mineru.net/apiManage/token
    免费: 2000 页/天
    环境变量: export MINERU_API_TOKEN='your_token'
    """
    import requests

    token = api_token or os.environ.get("MINERU_API_TOKEN")
    if not token:
        raise ValueError(
            "需要 MinerU API Token:\n"
            "  1. https://mineru.net/apiManage/token\n"
            "  2. export MINERU_API_TOKEN='your_token'"
        )

    base = "https://mineru.net/api/v4"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    fname = Path(pdf_path).name

    # 获取上传 URL
    res = requests.post(f"{base}/file-urls/batch", headers=headers, json={
        "files": [{"name": fname, "data_id": fname}],
        "model_version": model_version,
    })
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"MinerU API: {data}")

    batch_id = data["data"]["batch_id"]
    upload_url = data["data"]["file_urls"][0]

    print(f"[MinerU-Cloud] 上传: {fname}")
    with open(pdf_path, "rb") as f:
        requests.put(upload_url, data=f).raise_for_status()

    print("[MinerU-Cloud] 等待解析...")
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        res = requests.get(f"{base}/extract-results/batch/{batch_id}", headers=headers)
        items = res.json().get("data", {}).get("extract_result", [])
        if not items:
            continue

        state = items[0].get("state")
        if state == "done":
            print("[MinerU-Cloud] done")
            return _unpack_mineru_zip(items[0]["full_zip_url"], pdf_path)
        elif state == "failed":
            raise RuntimeError(f"失败: {items[0].get('err_msg')}")

        p = items[0].get("extract_progress", {})
        print(f"  {p.get('extracted_pages','?')}/{p.get('total_pages','?')} 页")

    raise TimeoutError(f"超时 ({max_wait}s)")


def _unpack_mineru_zip(zip_url: str, pdf_path: str) -> ParsedDocument:
    import requests, zipfile, io
    out = Path(f"./mineru_cloud/{Path(pdf_path).stem}")
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(requests.get(zip_url).content)) as zf:
        zf.extractall(out)

    md_files = list(out.rglob("*.md"))
    md_text = md_files[0].read_text("utf-8") if md_files else ""
    images = [
        ParsedImage(page_num=0, path=str(f))
        for f in out.rglob("*") if f.suffix.lower() in {".png", ".jpg", ".jpeg"}
    ]
    meta = _get_pdf_meta(pdf_path)
    return ParsedDocument(
        text=md_text, filename=Path(pdf_path).name,
        title=meta["title"], author=meta["author"],
        page_count=meta["page_count"], source_path=pdf_path,
        backend_used="mineru-cloud", pipeline_mode="vlm-cloud",
        images=images,
    )


# ══════════════════════════════════════════════════════════
#  后端 4: PyMuPDF4LLM (最快)
# ══════════════════════════════════════════════════════════

def parse_with_pymupdf4llm(
    pdf_path: str,
    output_dir: str = "./pymupdf_output",
    extract_images: bool = True,
) -> ParsedDocument:
    """PyMuPDF4LLM — 最快 (~0.1s/页), 纯 CPU。"""
    import pymupdf4llm

    pdf_path = str(Path(pdf_path).resolve())
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[PyMuPDF4LLM] parsing: {Path(pdf_path).name}")

    img_dir = str(out / "images") if extract_images else None
    if img_dir:
        Path(img_dir).mkdir(parents=True, exist_ok=True)

    md_text = pymupdf4llm.to_markdown(
        pdf_path, write_images=extract_images, image_path=img_dir,
    )

    images = []
    if img_dir and Path(img_dir).exists():
        images = [
            ParsedImage(page_num=0, path=str(f))
            for f in sorted(Path(img_dir).glob("*"))
            if f.suffix.lower() in {".png", ".jpg", ".jpeg"}
        ]

    meta = _get_pdf_meta(pdf_path)
    return ParsedDocument(
        text=md_text, filename=Path(pdf_path).name,
        title=meta["title"], author=meta["author"],
        page_count=meta["page_count"], source_path=pdf_path,
        backend_used="pymupdf4llm", pipeline_mode="cpu",
        images=images,
    )


# ══════════════════════════════════════════════════════════
#  后端 5: PyMuPDF Legacy (兜底)
# ══════════════════════════════════════════════════════════

def parse_with_pymupdf_legacy(pdf_path: str) -> ParsedDocument:
    """原始 PyMuPDF — 永远能跑的兜底方案。"""
    import fitz
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        blocks = page.get_text("blocks", sort=True)
        lines = []
        h = page.rect.height
        for b in blocks:
            x0, y0, x1, y1, text, *_ = b
            if y0 < h * 0.05 or y1 > h * 0.95:
                continue
            text = text.strip()
            if not text or text.isdigit():
                continue
            lines.append(text)
        if lines:
            pages.append(f"[第{i+1}页]\n" + "\n".join(lines))

    meta = doc.metadata
    n = len(doc)
    doc.close()
    return ParsedDocument(
        text="\n\n".join(pages),
        filename=Path(pdf_path).name,
        title=meta.get("title") or Path(pdf_path).name,
        author=meta.get("author", ""),
        page_count=n, source_path=pdf_path,
        backend_used="pymupdf-legacy", pipeline_mode="cpu",
    )


# ══════════════════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════════════════

BackendType = Literal[
    "docling", "mineru-local", "mineru-cloud", "pymupdf4llm", "pymupdf-legacy"
]

_FALLBACK_CHAIN = ["docling", "mineru-local", "pymupdf4llm", "pymupdf-legacy"]


def parse_pdf(
    pdf_path: str,
    backend: BackendType = "docling",
    output_dir: str = "./parse_output",
    auto_fallback: bool = True,
    **kwargs,
) -> ParsedDocument:
    """
    统一入口 — 自动探测环境, 缺依赖时自动回退。

    示例:
        # Docling (自动选 VLM / GPU / CPU)
        doc = parse_pdf("report.pdf")

        # 强制 Docling VLM
        doc = parse_pdf("report.pdf", force_mode="vlm")

        # 强制 Docling GPU (不用 VLM)
        doc = parse_pdf("report.pdf", force_mode="gpu")

        # MinerU (自动选 hybrid / pipeline)
        doc = parse_pdf("report.pdf", backend="mineru-local", lang="en")

        # 最快
        doc = parse_pdf("report.pdf", backend="pymupdf4llm")

        # 查看实际用了什么
        print(doc.backend_used, doc.pipeline_mode)
    """
    dispatch = {
        "docling":        lambda: parse_with_docling(pdf_path, output_dir, **kwargs),
        "mineru-local":   lambda: parse_with_mineru_local(pdf_path, output_dir, **kwargs),
        "mineru-cloud":   lambda: parse_with_mineru_cloud(pdf_path, **kwargs),
        "pymupdf4llm":    lambda: parse_with_pymupdf4llm(pdf_path, output_dir, **kwargs),
        "pymupdf-legacy": lambda: parse_with_pymupdf_legacy(pdf_path),
    }

    if backend not in dispatch:
        raise ValueError(f"未知后端: {backend}. 可选: {list(dispatch.keys())}")

    try:
        return dispatch[backend]()
    except Exception as e:
        if not auto_fallback:
            raise
        # Windows 下 docling 首次下载 HuggingFace 模型时, 可能因 symlink 权限不足报错。
        # 这里统一做后端回退, 保证解析流程可继续。
        print(f"[WARNING] {backend} failed: {type(e).__name__}: {e}")
        for fb in _FALLBACK_CHAIN:
            if fb == backend:
                continue
            try:
                print(f"[WARNING] fallback -> {fb}")
                return dispatch[fb]()
            except Exception as fb_e:
                print(f"[WARNING] {fb} failed: {type(fb_e).__name__}: {fb_e}")
                continue
        return parse_with_pymupdf_legacy(pdf_path)


# ══════════════════════════════════════════════════════════
#  LangChain 快捷集成
# ══════════════════════════════════════════════════════════

def parse_to_langchain_docs(
    pdf_path: str,
    backend: BackendType = "docling",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    skip_headings: list[str] | None = None,
    **kwargs,
) -> list:
    """
    一步到位: 解析 + 分块 → LangChain Document 列表。

    示例:
        docs = parse_to_langchain_docs("report.pdf",
            skip_headings=["disclaimer", "risk information"])
        vector_store.add_documents(docs)
    """
    from chunker import chunk_markdown, chunks_to_langchain_docs
    parsed = parse_pdf(pdf_path, backend=backend, **kwargs)
    chunks = chunk_markdown(
        parsed.text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        skip_headings=skip_headings,
    )
    return chunks_to_langchain_docs(chunks, source=parsed.filename)


# ══════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2 or sys.argv[1] == "--env":
        if len(sys.argv) >= 2 and sys.argv[1] == "--env":
            print_env_report()
            sys.exit(0)

        print("用法:")
        print("  python pdf_parser.py <pdf_path> [backend]")
        print("  python pdf_parser.py --env              # 查看环境探测")
        print()
        print("后端:")
        print("  docling        自动选 VLM/GPU/CPU (推荐)")
        print("  mineru-local   自动选 hybrid/pipeline")
        print("  mineru-cloud   云端 API (需 token)")
        print("  pymupdf4llm    最快")
        print("  pymupdf-legacy 兜底")
        sys.exit(1)

    pdf_file = sys.argv[1]
    chosen = sys.argv[2] if len(sys.argv) > 2 else "docling"

    # 先打印环境
    print_env_report()
    print()

    doc = parse_pdf(pdf_file, backend=chosen)

    print(f"\n{'═'*60}")
    print(f"  后端: {doc.backend_used} ({doc.pipeline_mode})")
    print(f"  文件: {doc.filename}")
    print(f"  页数: {doc.page_count}")
    print(f"  图片: {len(doc.images)} 张")
    print(f"  表格: {len(doc.tables)} 个")
    print(f"  文本: {len(doc.text)} 字符")
    print(f"{'═'*60}")
    print(f"\n{doc.text[:10000]}")
    if len(doc.text) > 10000:
        print("...(截断)")




