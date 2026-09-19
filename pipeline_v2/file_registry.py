from __future__ import annotations
import hashlib
from pathlib import Path
from .models import FileRegistryRecord

def sha256_file(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""): digest.update(chunk)
    return digest.hexdigest()

def build_registry(raw_dir: Path, source_urls: dict[str, str]) -> list[FileRegistryRecord]:
    records=[]; seen={}
    for path in sorted(p for p in raw_dir.rglob("*") if p.is_file()):
        digest=sha256_file(path); document_id=f"file-{digest[:16]}"; master=seen.get(digest)
        records.append(FileRegistryRecord(document_id=document_id,source_file=str(path),source_url=source_urls.get(str(path),""),file_name=path.name,file_type=path.suffix.lower().lstrip("."),file_size=path.stat().st_size,sha256=digest,duplicate_of=master,parse_status="pending"))
        seen.setdefault(digest,document_id)
    return records
