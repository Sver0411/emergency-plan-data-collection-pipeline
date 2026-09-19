"""写出环境、工具来源和 Pydantic V2 Schema；不访问网络。"""
from __future__ import annotations
import importlib.metadata as metadata
import json,platform,shutil,subprocess,sys
from pathlib import Path
from .config import ROOT
from .models import FileRegistryRecord, ParsedDocument, ClassifiedDocument, PlanSection, EnterprisePlan, SDSRecord, KnowledgeItem, DuplicateGroup, RedactionAudit, ManualReviewRecord

TOOLS=[
 ("Docling","docling","本地文档主解析器","https://github.com/docling-project/docling","https://pypi.org/project/docling/"),
 ("datasketch","datasketch","MinHash/LSH近似重复候选召回","https://github.com/ekzhu/datasketch","https://pypi.org/project/datasketch/"),
 ("RapidFuzz","RapidFuzz","重复候选精确复核","https://github.com/rapidfuzz/RapidFuzz","https://pypi.org/project/RapidFuzz/"),
 ("Presidio Analyzer","presidio-analyzer","敏感实体识别","https://github.com/data-privacy-stack/presidio","https://pypi.org/project/presidio-analyzer/"),
 ("Presidio Anonymizer","presidio-anonymizer","敏感实体替换","https://github.com/data-privacy-stack/presidio","https://pypi.org/project/presidio-anonymizer/"),
 ("Pydantic","pydantic","Pydantic V2 JSON模型和校验","https://github.com/pydantic/pydantic","https://pypi.org/project/pydantic/"),
 ("pytest","pytest","回归测试","https://github.com/pytest-dev/pytest","https://pypi.org/project/pytest/"),
 ("pypdf","pypdf","PDF回退解析","https://github.com/py-pdf/pypdf","https://pypi.org/project/pypdf/"),
 ("pdfplumber","pdfplumber","PDF回退解析比较","https://github.com/jsvine/pdfplumber","https://pypi.org/project/pdfplumber/"),
 ("python-docx","python-docx","DOCX回退解析","https://github.com/python-openxml/python-docx","https://pypi.org/project/python-docx/"),
]

def write(path:Path,data): path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def main():
    root=ROOT; report=root/'reports_v2'; report.mkdir(exist_ok=True)
    sources=[]
    for tool,package,purpose,github,pypi in TOOLS:
        meta=metadata.metadata(package)
        sources.append({"tool_name":tool,"package_name":package,"purpose":purpose,"installed_version":metadata.version(package),"official_github":github,"official_pypi":pypi,"license":meta.get('License-Expression') or meta.get('License') or 'see package metadata',"python_requirement":meta.get('Requires-Python') or '',"source_verified":True,"verification_notes":"Official PyPI project and the user-specified official GitHub repository were checked during setup; installed solely from PyPI."})
    write(root/'pipeline_v2/tool_sources.json',sources)
    (root/'pipeline_v2/requirements-lock.txt').write_text(subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True),encoding='utf-8')
    env=["# V2 清洗环境报告","",f"- 操作系统：{platform.platform()}",f"- Python：{sys.version.split()[0]}",f"- 可用磁盘空间：{shutil.disk_usage(root).free // (1024**3)} GiB",f"- 独立环境：`{sys.prefix}`","- 网络限制：运行阶段仅使用本地文件；Docling 以离线模式探测。"]
    environment_text='\n'.join(env)+'\n'
    (report/'environment_report.md').write_text(environment_text,encoding='utf-8')
    (root/'pipeline_v2/environment_report.md').write_text(environment_text,encoding='utf-8')
    notices=["# Third-party notices","","本流水线仅从官方 PyPI 安装下列依赖；许可证以各发行包元数据和官方仓库为准。",""]+[f"- {x['tool_name']} {x['installed_version']} — {x['license']} — {x['official_github']}" for x in sources]
    (root/'pipeline_v2/THIRD_PARTY_NOTICES.md').write_text('\n'.join(notices)+'\n',encoding='utf-8')
    for model in [FileRegistryRecord,ParsedDocument,ClassifiedDocument,PlanSection,EnterprisePlan,SDSRecord,KnowledgeItem,DuplicateGroup,RedactionAudit,ManualReviewRecord]: write(root/'pipeline_v2/schemas'/f'{model.__name__}.json',model.model_json_schema())
if __name__=='__main__': main()
