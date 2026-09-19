from __future__ import annotations
import re
from .models import KnowledgeItem, PlanSection

RULES={"accident_risks":r"事故风险|危险有害|事故类型|事故后果|影响范围","handling_steps":r"应急处置|现场处置|切断|隔离|警戒|疏散|收容|中和|洗消","ppe":r"个人防护|防护装备|呼吸器|防毒面具|防护服|防护手套","first_aid":r"急救|吸入|皮肤接触|眼睛接触|食入|心肺复苏|医疗转运","forbidden_actions":r"禁止|严禁|不得|不可|避免"}
UNSAFE=r"催吐|洗胃|小动物试验"
def extract_knowledge(document_id: str, source_url: str, sections: list[PlanSection]) -> list[KnowledgeItem]:
    output=[]
    for section in sections:
        for kind,pattern in RULES.items():
            if re.search(pattern,section.heading):
                for sentence in re.split(r"(?<=[。；;])",section.content):
                    if re.search(pattern,sentence) and len(sentence.strip())>8:
                        start=section.start_position+section.content.find(sentence); flags=["unsafe_or_outdated_candidate"] if re.search(UNSAFE,sentence) else []
                        output.append(KnowledgeItem(kind=kind,text=sentence.strip(),document_id=document_id,source_url=source_url,source_section=section.heading,start_position=start,end_position=start+len(sentence),flags=flags))
    return output
