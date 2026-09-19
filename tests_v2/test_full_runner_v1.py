from __future__ import annotations

from pathlib import Path

from pipeline_v2.full_runner_v1 import (
    baseline_signature,
    regulation_record,
    scan_raw,
    source_metadata,
    split_for_chunks,
)
from pipeline_v2.parsers import parse_local


def test_scan_raw_is_stable_and_read_only(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "样本.txt"
    source.write_text("原始资料", encoding="utf-8")
    before = source.read_bytes()
    first = scan_raw(raw)
    second = scan_raw(raw)
    assert first == second
    assert baseline_signature(first) == baseline_signature(second)
    assert source.read_bytes() == before


def test_source_metadata_never_invents_url():
    assert source_metadata([])["source_url"] == ""
    result = source_metadata([{"url": "https://example.invalid/source", "title": "资料", "type": "regulation"}])
    assert result["source_url"] == "https://example.invalid/source"
    assert result["source_url_status"] == "present"


def test_regulation_validity_remains_unknown_without_explicit_repeal(tmp_path: Path):
    source = tmp_path / "规定.txt"
    source.write_text("规定", encoding="utf-8")
    record = regulation_record({
        "document_id": "file-1234567890",
        "parent_document_id": "file-1234567890",
        "title": "某安全规定",
        "canonical_title": "某安全规定",
        "normalized_text": "第一条 为了安全。\n第二条 本规定自2020年1月1日起施行。",
        "source_file": str(source),
        "source_url": "",
        "source_urls": [],
        "sha256": "0" * 64,
        "quality_grade": "manual_review",
    })
    assert record["validity_status"] == "unknown"
    assert record["review_status"] == "pending"


def test_rag_secondary_split_preserves_text_content():
    text = "第一段处置说明。\n\n第二段处置说明。"
    chunks = split_for_chunks(text, max_chars=20, overlap=3)
    assert chunks
    assert "第一段处置说明" in chunks[0][2]
    assert all(start <= end for start, end, _ in chunks)


def test_internal_html_parser_label_does_not_require_package_metadata(tmp_path: Path, monkeypatch):
    source = tmp_path / "资料.html"
    source.write_text("<h1>有限空间作业指导意见</h1><p>" + "安全管理要求。" * 30 + "</p>", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_V2_SKIP_DOCLING", "1")
    parsed, _ = parse_local("file-html-parser-test", source)
    assert parsed.parser == "html_local"
    assert parsed.parser_version == ""
    assert len(parsed.raw_text) > 120
