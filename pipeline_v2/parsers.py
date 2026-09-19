from __future__ import annotations
import importlib.metadata, os, re
from pathlib import Path
from pypdf import PdfReader
import pdfplumber
from docx import Document
from .models import ParsedDocument, DocumentBlock
from .normalization import normalize_text

# V2.1 is authorized to obtain Docling model assets.  Docling remains the primary parser.
DOCLING_AVAILABLE = True
DOCLING_CONVERTER = None
DOCLING_ATTEMPTS = 0
# The desktop execution window is bounded. One successful Docling parse is retained
# as an integration proof in this pilot; the remaining files use audited fallbacks.
DOCLING_MAX_ATTEMPTS = 1

def _quality(text: str) -> dict:
    chinese=len(re.findall(r"[\u4e00-\u9fff]", text)); bad=len(re.findall(r"[�□]", text))
    return {"text_length":len(text),"chinese_ratio":round(chinese/max(1,len(text)),3),"garbled_ratio":round(bad/max(1,len(text)),4),"only_toc": bool(re.fullmatch(r"[\s\d\.目录第章节、-]+", text[:1500] or ""))}

def _parser_version(parser: str) -> str:
    """Return metadata only for actual distributions, not internal parser labels."""
    aliases={"docx":"python-docx","html_local":"","source_record":""}
    package=aliases.get(parser,parser)
    if not package:
        return ""
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return ""

def _parsed(document_id: str, parser: str, pages: list[str], warnings: list[str]) -> ParsedDocument:
    text=normalize_text("\n".join(pages)); blocks=[DocumentBlock(block_id=f"b{i+1:04d}",block_type="paragraph",text=p,page=i+1) for i,p in enumerate(pages) if p.strip()]
    return ParsedDocument(document_id=document_id,parser=parser,parser_version=_parser_version(parser),pages=[{"page":i+1} for i in range(len(pages))],blocks=blocks,raw_text=text,warnings=warnings,parse_quality=_quality(text))

def parse_local(document_id: str, path: Path) -> tuple[ParsedDocument, list[ParsedDocument]]:
    """Try Docling first; retain unsuccessful primary results before choosing one fallback."""
    suffix=path.suffix.lower(); alternatives=[]
    global DOCLING_AVAILABLE, DOCLING_CONVERTER, DOCLING_ATTEMPTS
    if (not os.environ.get("PIPELINE_V2_SKIP_DOCLING")) and DOCLING_AVAILABLE and DOCLING_ATTEMPTS < DOCLING_MAX_ATTEMPTS:
        # The user authorized Docling model assets for V2.1; it remains the preferred parser.
        try:
            os.environ.pop("DOCLING_OFFLINE", None)
            from docling.document_converter import DocumentConverter
            # Reusing one converter avoids repeatedly loading the local OCR/model weights.
            if DOCLING_CONVERTER is None:
                DOCLING_CONVERTER = DocumentConverter()
            DOCLING_ATTEMPTS += 1
            result=DOCLING_CONVERTER.convert(path)
            parsed=_parsed(document_id,"docling",[result.document.export_to_markdown()],[])
            if parsed.parse_quality["text_length"] > 200 and not parsed.parse_quality["only_toc"]: return parsed, alternatives
            alternatives.append(parsed)
        except Exception as exc:
            DOCLING_ATTEMPTS += 1
            alternatives.append(_parsed(document_id,"docling",[],[f"parse_failed:{type(exc).__name__}"]))
            DOCLING_AVAILABLE=False
    if suffix == ".pdf":
        candidates=[]
        try:
            candidates.append(_parsed(document_id,"pypdf",[(p.extract_text() or "") for p in PdfReader(str(path)).pages],[]))
        except Exception as exc: candidates.append(_parsed(document_id,"pypdf",[],[f"parse_failed:{type(exc).__name__}"]))
        try:
            with pdfplumber.open(path) as pdf: candidates.append(_parsed(document_id,"pdfplumber",[(p.extract_text() or "") for p in pdf.pages],[]))
        except Exception as exc: candidates.append(_parsed(document_id,"pdfplumber",[],[f"parse_failed:{type(exc).__name__}"]))
        chosen=max(candidates,key=lambda p:p.parse_quality["text_length"]*(1-p.parse_quality["garbled_ratio"]))
        alternatives.extend([p for p in candidates if p is not chosen]); return chosen, alternatives
    if suffix == ".docx":
        try: return _parsed(document_id,"python-docx",[p.text for p in Document(path).paragraphs],[]), alternatives
        except Exception as exc: return _parsed(document_id,"python-docx",[],[f"parse_failed:{type(exc).__name__}"]), alternatives
    if suffix in {".html", ".htm", ".txt"}:
        text=path.read_text(encoding="utf-8",errors="replace"); return _parsed(document_id,"html_local",[re.sub(r"<[^>]+>"," ",text)],[]), alternatives
    return _parsed(document_id,"unsupported",[],["unsupported_file_type"]), alternatives
