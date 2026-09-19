from __future__ import annotations
import re

CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-— ]?\d{7,8})(?!\d)")

def normalize_text(text: str) -> str:
    text = text.replace("\r", "\n").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?m)^\s*(?:第\s*\d+\s*页|\d+)\s*(?:/\s*共\s*\d+\s*页)?\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def devariable_text(text: str) -> str:
    text = PHONE_RE.sub("{{phone}}", text)
    text = CAS_RE.sub("{{cas}}", text)  # 仅用于近似比较，不改 normalized_text。
    text = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "{{date}}", text)
    text = re.sub(r"(?im)(联系人|总指挥|负责人)\s*[:：]\s*[\u4e00-\u9fff]{2,4}", r"\1：{{person}}", text)
    text = re.sub(r"(?im)(?:地址|厂址|办公地址)\s*[:：]\s*[^\n。；;]{5,80}", "地址：{{address}}", text)
    return text

def looks_garbled(text: str) -> bool:
    if len(text.strip()) < 120:
        return True
    invalid = len(re.findall(r"[�□�]", text))
    return invalid / max(1, len(text)) > 0.01
