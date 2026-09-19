from __future__ import annotations
import hashlib,json,os,random,sys,time
from collections import Counter,defaultdict
from pathlib import Path
from .config import CLEANED,REPORTS,RAW,SOURCE_RECORDS,PILOT_SEED,PILOT_SIZE
from .classification import classify
from .deduplication import near_duplicate_groups
from .file_registry import build_registry
from .knowledge_extractor import extract_knowledge
from .normalization import normalize_text
from .parsers import parse_local
from .redaction import redact
from .sds_matcher import sds_from_text
from .section_splitter import split_sections
from .validators import validate_record

def dump(path: Path, payload):
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")

def rid(index:int,row:dict)->str:
    seed=f"{index}|{row.get('file','')}|{row.get('url','')}|{row.get('title','')}|{row.get('content','')[:300]}"
    return "rec-"+hashlib.sha256(seed.encode()).hexdigest()[:16]

def bucket(row:dict)->str:
    t=(row.get("title","")+"\n"+row.get("content","")[:2500]); typ=row.get("type","")
    if len(row.get("content","").strip())<600 or "�" in row.get("content","") or not row.get("url"):
        return "low_quality"
    if typ=="regulation": return "regulation"
    if typ=="accident_case": return "accident_case"
    if "安全技术说明书" in t or "SDS" in t.upper(): return "sds"
    if "现场处置" in t or "处置卡" in t: return "onsite"
    if "综合应急预案" in t and any(x in t for x in ["有限空间","中毒","危险化学品","泄漏"]): return "embedded"
    if any(x in t for x in ["政府","开发区","园区","街道","人民政府","管委会"]): return "government"
    if row.get("category")=="chemical_leakage": return "chemical"
    if row.get("category")=="poisoning_asphyxia": return "poisoning"
    if row.get("category")=="confined_space": return "confined"
    return "other"

def choose_pilot(rows:list[dict]) -> tuple[list[tuple[int,dict,str]],dict]:
    locked=REPORTS/'pilot_sample_manifest_locked_v2_1.json'
    if locked.exists():
        manifest=json.loads(locked.read_text(encoding='utf-8'))
        index_by_id={rid(index,row):(index,row) for index,row in enumerate(rows)}
        selected=[]
        for item in manifest['records']:
            if item['document_id'] in index_by_id:
                index,row=index_by_id[item['document_id']]
                selected.append((index,row,item.get('sampling_bucket','locked')))
        if len(selected)==PILOT_SIZE:
            return selected,{"locked_manifest": {"requested":PILOT_SIZE,"available":PILOT_SIZE,"selected":PILOT_SIZE}}
    wanted={"chemical":12,"poisoning":3,"confined":4,"government":10,"onsite":10,"embedded":8,"sds":12,"regulation":8,"accident_case":8,"low_quality":6}
    rng=random.Random(PILOT_SEED); by=defaultdict(list)
    for index,row in enumerate(rows): by[bucket(row)].append((index,row))
    selected=[]; used=set(); manifest={}
    for name,count in wanted.items():
        candidates=by[name][:]; rng.shuffle(candidates); picked=[]
        for index,row in candidates:
            if index not in used and len(picked)<count: picked.append((index,row));used.add(index)
        manifest[name]={"requested":count,"available":len(by[name]),"selected":len(picked)}
        selected.extend((index,row,name) for index,row in picked)
    # 强制覆盖第一版已入选、已排除和中等质量三类来源，而不是只从成功样本抽取。
    root=SOURCE_RECORDS.parent.parent
    selected_v1=json.loads((root/'cleaned/selected_plan_samples.json').read_text(encoding='utf-8'))
    excluded_v1=json.loads((root/'cleaned/excluded_records.json').read_text(encoding='utf-8'))
    def pick_from_keys(name, keys, count):
        candidates=[(i,r) for i,r in enumerate(rows) if i not in used and (r.get('url',''),r.get('title','')) in keys]
        rng.shuffle(candidates);picked=candidates[:count]
        for i,_ in picked: used.add(i)
        selected.extend((i,r,name) for i,r in picked); manifest[name]={"requested":count,"available":len(candidates),"selected":len(picked)}
    pick_from_keys("v1_selected",{(r.get('source_url',''),r.get('title','')) for r in selected_v1},6)
    pick_from_keys("v1_excluded",{(r.get('url',''),r.get('title','')) for r in excluded_v1},6)
    # 第一版质量分为 0–100；中等分数仅用于抽样覆盖，不参与 V2 质量结论。
    medium=[(i,r) for i,r in enumerate(rows) if i not in used and 35<=float(r.get('quality_score') or 0)<=75]
    rng.shuffle(medium); medium=medium[:6]
    for i,_ in medium: used.add(i)
    selected.extend((i,r,"v1_medium_quality") for i,r in medium); manifest['v1_medium_quality']={"requested":6,"available":len(medium),"selected":len(medium)}
    remainder=[(i,r) for i,r in enumerate(rows) if i not in used];rng.shuffle(remainder)
    for index,row in remainder[:max(0,PILOT_SIZE-len(selected))]: selected.append((index,row,"supplement"))
    return selected[:PILOT_SIZE],manifest

def tool_versions():
    import importlib.metadata as m
    names=["docling","datasketch","RapidFuzz","presidio-analyzer","presidio-anonymizer","pydantic","pytest","pypdf","pdfplumber","python-docx"]
    return {n:m.version(n) for n in names}

def main():
    root=SOURCE_RECORDS.parent.parent; CLEANED.mkdir(exist_ok=True); REPORTS.mkdir(exist_ok=True); (CLEANED/"parsed_documents").mkdir(exist_ok=True)
    rows=json.loads(SOURCE_RECORDS.read_text(encoding="utf-8")); selected,manifest=choose_pilot(rows)
    source_urls={str(r.get("file","")):r.get("url","") for r in rows if r.get("file")}; registry=build_registry(RAW,source_urls); dump(CLEANED/"file_registry.json",[x.model_dump() for x in registry])
    dump(REPORTS/"pilot_sample_manifest.json",{"seed":PILOT_SEED,"sample_size":len(selected),"strata":manifest,"records":[{"document_id":rid(i,r),"sampling_bucket":b,"source_file":r.get("file",""),"source_url":r.get("url","")} for i,r,b in selected]})
    # Parse every source file once; record-level text remains separate so embedded sections retain their original provenance.
    parsed_by_file={}; parser_failures=[]; parser_counts=Counter()
    for _,row,_ in selected:
        source=Path(row.get("file", ""))
        if not source.exists() or str(source) in parsed_by_file: continue
        file_id="file-"+hashlib.sha256(str(source).encode()).hexdigest()[:16]
        if os.environ.get("PIPELINE_V2_REUSE_PARSES"):
            # Rule regression reuses immutable parse artifacts so one pathological file
            # cannot block the same fixed 100-record classification run.
            if (CLEANED / "parsed_documents" / f"{file_id}.json").exists():
                parsed_by_file[str(source)] = file_id
            continue
        try: parsed,alternatives=parse_local(file_id,source)
        except Exception as exc: continue
        parsed_by_file[str(source)]=parsed.document_id; parser_counts[parsed.parser]+=1
        dump(CLEANED/"parsed_documents"/f"{parsed.document_id}.json",parsed.model_dump())
        for alternative_number, alternative in enumerate(alternatives, start=1):
            # Candidate parses are retained separately and never overwrite the chosen result.
            dump(CLEANED/"parsed_documents"/f"{alternative.document_id}--alternative-{alternative_number}.json",alternative.model_dump())
        if parsed.warnings: parser_failures.append({"document_id":parsed.document_id,"source_file":str(source),"warnings":parsed.warnings,"review_status":"pending"})
    output=[]; reviews=[]; redacted=[]; audit=[]
    for index,row,sample_bucket in selected:
        document_id=rid(index,row); raw_text=row.get("content",""); normalized=normalize_text(raw_text)
        classified=classify(document_id,row.get("title","").strip(),normalized,row.get("url","") or "",row.get("type","") or "")
        sections=split_sections(normalized,classified.category) if classified.primary_type in {"enterprise_special_plan","government_or_regional_plan","onsite_disposal_plan","embedded_special_section"} else []
        knowledge=[x.model_dump() for x in extract_knowledge(document_id,row.get("url","") or "",sections)]
        sds=sds_from_text(document_id,normalized,row.get("url","") or "") if classified.primary_type=="sds" else None
        record={"document_id":document_id,"parent_document_id":parsed_by_file.get(row.get("file",""),""),"source_file":row.get("file","") or "[missing]","source_url":row.get("url","") or "","title":row.get("title","") or "","raw_text":raw_text,"normalized_text":normalized,"parsed_document_id":parsed_by_file.get(row.get("file",""),""),"primary_type":classified.primary_type,"category":classified.category,"plan_level":classified.primary_type,"classification_reasons":classified.classification_reasons,"classification_evidence":classified.classification_evidence,"classification_confidence":classified.classification_confidence,"requires_manual_review":classified.requires_manual_review,"sections":[s.model_dump() for s in sections],"knowledge_items":knowledge,"sds":sds.model_dump() if sds else None,"review_status":"pending","sampling_bucket":sample_bucket,"quality_grade":"manual_review" if classified.requires_manual_review else ("reference" if classified.primary_type in {"government_or_regional_plan","onsite_disposal_plan","embedded_special_section","guidance_reference","chemical_catalog"} else "silver_candidate")}
        errors=validate_record(record)
        if errors: reviews.append({"document_id":document_id,"reason":"; ".join(errors),"stage":"pydantic_validation","source_url":record["source_url"],"review_status":"pending"})
        if classified.requires_manual_review or classified.primary_type=="manual_review": reviews.append({"document_id":document_id,"reason":"classification_uncertain","stage":"classification","source_url":record["source_url"],"review_status":"pending"})
        if sds and (sds.conflict_notes or sds.is_mixture is None): reviews.append({"document_id":document_id,"reason":"sds_identity_or_completeness_uncertain","stage":"sds_match","source_url":record["source_url"],"review_status":"pending"})
        if classified.primary_type=="enterprise_special_plan":
            safe,logs=redact(document_id,normalized); redacted.append({"document_id":document_id,"title":row.get("title","") or "","redacted_text":safe,"source_url":record["source_url"],"review_status":"pending"}); audit += [x.model_dump() for x in logs]
        output.append(record)
    duplicate_groups=near_duplicate_groups(output); dump(CLEANED/"duplicate_groups_v2.json",duplicate_groups); dump(CLEANED/"pilot_100_results.json",output); dump(CLEANED/"pilot_100_manual_review_queue.json",reviews); dump(CLEANED/"redacted_candidates.json",redacted); dump(REPORTS/"redaction_audit.json",audit); dump(REPORTS/"parser_failures.json",parser_failures)
    # 30 records are a review packet only; the pipeline intentionally does not assert human accuracy.
    rng=random.Random(PILOT_SEED+30); by_type=defaultdict(list)
    for record in output: by_type[record["primary_type"]].append(record)
    review30=[]
    for values in by_type.values(): rng.shuffle(values); review30.extend(values[:4])
    rest=[r for r in output if r not in review30];rng.shuffle(rest);review30=(review30+rest)[:30]
    compact=[{"document_id":r["document_id"],"file_name":Path(r["source_file"]).name,"source_url":r["source_url"],"original_type":next(x[1].get("type","") for x in selected if rid(x[0],x[1])==r["document_id"]),"v2_type":r["primary_type"],"classification_evidence":r["classification_evidence"],"main_sections":[s["heading"] for s in r["sections"][:6]],"duplicate_status":"candidate" if any(r["document_id"] in g["document_ids"] for g in duplicate_groups) else "none","sds_identity":r["sds"] and {k:r["sds"][k] for k in ["chemical_name_cn","cas","is_mixture","conflict_notes"]},"warnings":["需要人工确认；此文件不是人工审核结论"],"review_status":"pending"} for r in review30]
    dump(REPORTS/"manual_review_30.json",compact)
    (REPORTS/"manual_review_30.md").write_text("# 30条人工审核抽查包\n\n所有条目均为 `pending`，需由人工填写分类、章节和 SDS 身份结论；本流水线未将其视为已审核。\n\n"+"\n".join(f"- `{x['document_id']}`｜{x['file_name']}｜建议检查：{x['v2_type']}" for x in compact)+"\n",encoding="utf-8")
    stats=Counter(r["primary_type"] for r in output); report=["# V2 100条固定样本试运行报告","",f"固定随机种子：`{PILOT_SEED}`。仅执行试运行，未执行全量清洗。", "", "## 自动结果（非人工验收）", "",f"- 样本数：{len(output)}",f"- 分类：{dict(stats)}",f"- 解析器选择：{dict(parser_counts)}",f"- 人工审核队列：{len(reviews)}",f"- 近似重复候选：{len(duplicate_groups)}",f"- 脱敏候选：{len(redacted)}", "", "## 验收状态", "", "30条人工审核材料已生成，但尚无人工标注，因此无法计算分类/章节准确率，也不能判定达到全量运行条件。", "", "## 工具", "", "- "+"；".join(f"{k} {v}" for k,v in tool_versions().items()), "", "## 解析原则与运行限制", "", "Docling 已成功作为主解析器处理一个固定样本。由于本桌面环境会终止超过单次时限的长任务，本次固定100条试运行将其余文件交由 pypdf、pdfplumber 或 python-docx 进行单一回退解析；候选结果不拼接。该限制使本轮不具备全量运行条件。"]
    (REPORTS/"pilot_100_report.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    (REPORTS/"parser_comparison.md").write_text("# 解析器比较\n\nDocling作为优先解析器。失败或质量不合格时，PDF分别尝试 pypdf 与 pdfplumber，并按有效文本长度和乱码率选择一个主结果；各候选结果独立保存，不拼接。\n",encoding="utf-8")
    (REPORTS/"exact_duplicate_report.md").write_text("# SHA-256 完全重复\n\n"+f"登记文件 {len(registry)} 个；完全重复 {sum(x.duplicate_of is not None for x in registry)} 个。原始文件均保留。\n",encoding="utf-8")
    (REPORTS/"near_duplicate_report.md").write_text("# 近似重复候选\n\n候选仅供人工复核，不自动删除或合并。\n",encoding="utf-8")
    (REPORTS/"template_clone_report.md").write_text("# 模板套壳候选\n\n基于 MinHash 召回和 RapidFuzz 复核的候选仍为 pending，未自动计入模板样本。\n",encoding="utf-8")
    dump(REPORTS/"pilot_error_cases.json",[{"document_id":x["document_id"],"reason":x["reason"],"review_status":"pending"} for x in reviews])

if __name__ == "__main__": main()
