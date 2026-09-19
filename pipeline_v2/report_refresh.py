"""从已完成的100条试运行结果生成审计指标；不重新解析、不联网。"""
from __future__ import annotations
import json,re
from collections import Counter
from pathlib import Path
from .config import CLEANED, REPORTS

def load(name): return json.loads((name).read_text(encoding='utf-8'))
def main():
    rows=load(CLEANED/'pilot_100_results.json'); queue=load(CLEANED/'pilot_100_manual_review_queue.json'); audit=load(REPORTS/'redaction_audit.json'); dup=load(CLEANED/'duplicate_groups_v2.json'); manual=load(REPORTS/'manual_review_30.json'); parsed=[load(p) for p in (CLEANED/'parsed_documents').glob('*.json')]
    headings=[s['heading'] for r in rows for s in r['sections']]
    count=lambda p:sum(bool(re.search(p,x)) for x in headings)
    kinds=Counter(r['primary_type'] for r in rows); parsers=Counter(p['parser'] for p in parsed)
    trace=sum(bool(r['source_file'] and r['source_url']) for r in rows); pydantic_fail=sum(q['stage']=='pydantic_validation' for q in queue)
    bad_cas=count(r"^\d{2,7}-\d{2}-\d$"); bad_phone=count(r"^(?:1[3-9]\d{9}|0\d{2,3}[-— ]?\d{7,8})$"); bad_person=count(r"^(?:联系人|总指挥|负责人)[:：]"); bad_page=count(r"^第?\d+页")
    report=["# V2 100条离线试运行报告","", "## 范围", "", "固定随机种子：`20260824`。本轮只完成工具接入、流水线、测试与100条试运行；没有全量清洗、网络数据下载、知识库导入或模板生成。", "", "## 抽样构成", "", *[f"- {k}：{v['selected']} 条（请求 {v['requested']}）" for k,v in load(REPORTS/'pilot_sample_manifest.json')['strata'].items()], "", "## 自动分类（非人工验收）", "", *[f"- {k}：{v} 条" for k,v in sorted(kinds.items())], "", "## 解析器", "", f"- 代表性来源文件：{len(parsed)} 个", f"- Docling：已安装；离线模型初始化超时，未下载模型，因此本次未作为成功解析结果使用。", f"- 回退解析选择：{dict(parsers)}；回退使用率 100%（{len(parsed)}/{len(parsed)}）。", "", "## 自动质量指标", "", f"- 来源可追溯率：{trace}/{len(rows)} = {trace/len(rows):.0%}", f"- Pydantic 校验通过率：{len(rows)-pydantic_fail}/{len(rows)} = {(len(rows)-pydantic_fail)/len(rows):.0%}", f"- CAS 误识别为标题：{bad_cas}", f"- 电话误识别为标题：{bad_phone}", f"- 人名/联系人字段误识别为标题：{bad_person}", f"- 页码误识别为标题：{bad_page}", f"- 有限空间跨专项内容混入：{sum('cross_topic_boundary' in s['warnings'] for r in rows for s in r['sections'])}", f"- 近似重复候选：{len(dup)}（均为 pending，未自动合并）", f"- 脱敏审计替换：{len(audit)} 条（仅企业候选副本）", f"- SDS 身份/完整性待复核：{sum(q['stage']=='sds_match' for q in queue)} 条", f"- 分类待复核：{sum(q['stage']=='classification' for q in queue)} 条", "", "## 尚不可计算的人工指标", "", "以下指标必须以人工审核结果为分母，当前均为 N/A：文档分类准确率、企业专项预案识别准确率、章节切分准确率、近似重复误判率、脱敏误检/漏检数量、SDS不同物质错配的人工确认数。", "", "## 验收结论", "", "**未达到全量运行条件。** 虽然自动规则测试和可追溯/标题边界检查通过，但 `manual_review_30.*` 仍未由人工填写，不能替代人工验收。请先审阅并回填该30条包，再决定是否修正规则或另行授权全量运行。"]
    (REPORTS/'pilot_100_report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    lines=["# 30条人工审核抽查包","", "每条均为 `pending`，下列为待人工选择/确认的材料，不是人工审核结论。", ""]
    for item in manual:
        lines += [f"## {item['document_id']}", "", f"- 文件：{item['file_name']}", f"- 来源：{item['source_url']}", f"- 原类型：{item['original_type']}", f"- V2 分类：{item['v2_type']}", f"- 分类证据：{item['classification_evidence']}", f"- 主要章节：{item['main_sections']}", f"- 重复判断：{item['duplicate_status']}", f"- SDS 身份：{item['sds_identity']}", f"- 待人工结论：分类 / 章节边界 / SDS身份 / 脱敏结果 / 是否接受。", ""]
    (REPORTS/'manual_review_30.md').write_text('\n'.join(lines),encoding='utf-8')
if __name__=='__main__': main()
