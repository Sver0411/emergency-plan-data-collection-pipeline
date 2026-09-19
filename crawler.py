"""公开应急预案采集、解析、分类和 JSON 构建流水线。

本脚本在当前流水线目录工作，不依赖业务应用代码。
运行后会生成原始文档、解析文本、通用记录、分类 JSON、质量报告和模板草稿。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup
from docx import Document
from lxml import html
from openpyxl import load_workbook
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw"
ATTACHMENT_DIR = RAW_DIR / "attachments"
TEXT_DIR = ROOT / "text"
JSON_DIR = ROOT / "json"
TEMPLATE_DIR = ROOT / "templates"
REPORT_DIR = ROOT / "reports"
USER_AGENT = "emergency-plan-rag-research/1.0 (+public-material-collection)"
REQUEST_TIMEOUT = 10
SEARCH_TIMEOUT = int(os.getenv("EMERGENCY_SEARCH_TIMEOUT", "6"))
SEARCH_WORKERS = int(os.getenv("EMERGENCY_SEARCH_WORKERS", "8"))
# 目标数量是最低量，不设置按类别截断。可用环境变量仅在需要控制单次运行规模时临时指定。
MAX_RESULTS_PER_QUERY = int(os.getenv("EMERGENCY_MAX_RESULTS_PER_QUERY", "8"))
MAX_CANDIDATES = int(os.getenv("EMERGENCY_MAX_CANDIDATES", "0"))
MIN_TARGETS = {
    "chemical_leakage": 40,
    "poisoning_asphyxia": 30,
    "confined_space": 20,
    "other_accident": 10,
    "SDS": 100,
    "regulation": 30,
    "accident_case": 50,
}
ALLOWED_DOC_EXTENSIONS = {".pdf", ".doc", ".docx"}

# 用户提供的目录表作为本地权威输入保存到流水线原始资料目录；不为本地附件编造互联网 URL。
CHEMICAL_CATALOG_SOURCE = Path(
    os.getenv("EMERGENCY_CHEMICAL_CATALOG", str(ATTACHMENT_DIR / "危险化学品目录指南(2)(1).xlsx"))
)
CHEMICAL_CATALOG_COPY = ATTACHMENT_DIR / CHEMICAL_CATALOG_SOURCE.name
CHEMICAL_CATALOG_URL = "local://raw/attachments/危险化学品目录指南(2)(1).xlsx"

# 搜索词按目标资料类型组织，便于后续扩展或替换搜索引擎。
SEARCH_QUERIES = {
    "chemical_leakage": [
        "危险化学品泄漏事故专项应急预案 filetype:pdf",
        "危化品泄漏现场处置方案 filetype:pdf",
        "化工企业泄漏事故应急预案 filetype:pdf",
        "储罐泄漏事故专项预案 filetype:pdf",
        "液氨泄漏事故应急预案 filetype:pdf",
        "氯气泄漏事故应急预案 filetype:pdf",
        "甲醇泄漏事故应急预案 filetype:pdf",
        "危险化学品生产安全事故专项应急预案 filetype:doc",
        "危险化学品事故专项应急预案 site:gov.cn filetype:pdf",
        "危险化学品泄漏应急预案 site:edu.cn filetype:pdf",
    ],
    "poisoning_asphyxia": [
        "中毒和窒息事故专项应急预案 filetype:pdf",
        "有限空间事故专项应急预案 filetype:pdf",
        "受限空间作业事故应急预案 filetype:pdf",
        "硫化氢中毒事故应急预案 filetype:pdf",
        "缺氧窒息事故现场处置方案 filetype:pdf",
        "有限空间作业专项应急预案 site:gov.cn filetype:pdf",
        "中毒窒息专项应急预案 site:edu.cn filetype:pdf",
        "有限空间应急预案 filetype:docx",
    ],
    "confined_space": [
        "有限空间作业专项应急预案 filetype:pdf",
        "有限空间事故现场处置方案 filetype:pdf",
        "受限空间中毒窒息应急预案 filetype:pdf",
        "污水池有限空间应急预案 filetype:pdf",
        "储罐有限空间作业应急预案 filetype:pdf",
        "有限空间应急救援预案 site:gov.cn filetype:pdf",
        "有限空间专项预案 site:edu.cn filetype:pdf",
    ],
    "other_accident": [
        "生产安全事故专项应急预案 火灾爆炸 filetype:pdf",
        "危险化学品火灾爆炸专项应急预案 filetype:pdf",
        "机械伤害专项应急预案 filetype:pdf",
        "触电事故专项应急预案 filetype:pdf",
        "高处坠落专项应急预案 filetype:pdf",
        "坍塌事故专项应急预案 filetype:pdf",
        "生产安全事故现场处置方案 site:gov.cn filetype:pdf",
    ],
    "regulation": [
        "危险化学品安全管理条例 site:gov.cn",
        "生产安全事故应急预案管理办法 site:gov.cn",
        "GB/T 29639 生产经营单位生产安全事故应急预案编制导则",
        "有限空间作业安全规定 site:gov.cn",
        "危险化学品企业特殊作业安全规范 site:gov.cn",
    ],
    "sds": [],
    "accident_case": [
        "有限空间 中毒窒息 典型事故案例 site:mem.gov.cn filetype:pdf",
        "危险化学品 泄漏 典型事故案例 site:mem.gov.cn filetype:pdf",
        "化工企业 泄漏 事故案例 应急处置 filetype:pdf",
    ],
}

# 常见危险化学品词表用于扩展 SDS 检索和风险关键词，不局限于任务示例中的十种物质。
COMMON_CHEMICALS = [
    "盐酸", "硫酸", "硝酸", "氢氟酸", "磷酸", "醋酸", "氢氧化钠", "氢氧化钾", "氨", "氨水",
    "氯气", "氯化氢", "硫化氢", "一氧化碳", "二氧化硫", "二氧化氮", "光气", "甲醛", "苯", "甲苯",
    "二甲苯", "甲醇", "乙醇", "丙酮", "乙酸乙酯", "苯乙烯", "氯乙烯", "苯酚", "次氯酸钠", "过氧化氢",
    "液化石油气", "丙烷", "丁烷", "汽油", "柴油", "煤油", "电石", "硝酸铵", "过氧乙酸", "环氧乙烷", "丙烯腈",
    "丙烯", "乙烯", "乙炔", "氢气", "氧气", "二氧化碳", "一氧化氮", "二氧化氯", "氯甲烷", "二氯甲烷",
    "三氯甲烷", "四氯化碳", "三氯乙烯", "四氯乙烯", "氯苯", "溴甲烷", "氟化氢", "溴素", "碘", "硫磺",
    "三氧化硫", "硫化钠", "硫酸二甲酯", "氰化钠", "氰化钾", "氰化氢", "砷化氢", "磷化氢", "磷酸三丁酯", "黄磷",
    "红磷", "五氧化二磷", "氯酸钾", "高锰酸钾", "重铬酸钾", "铬酸", "漂白粉", "次氯酸钙", "氯酸钠", "亚硝酸钠",
    "硝酸钾", "硝酸钠", "硝酸银", "过硫酸铵", "过硫酸钠", "过氧化钠", "过氧化钾", "金属钠", "金属钾", "镁粉",
    "铝粉", "锌粉", "碳化钙", "硅烷", "硼烷", "氯乙酸", "丙烯酸", "甲基丙烯酸", "丙烯酸甲酯", "丙烯酸乙酯",
    "甲苯二异氰酸酯", "二苯基甲烷二异氰酸酯", "异氰酸甲酯", "环氧氯丙烷", "乙二醇", "二甘醇", "正己烷", "正庚烷", "正辛烷", "环己烷",
    "环己酮", "乙酸乙烯酯", "乙酸酐", "醋酸乙烯", "甲酸", "草酸", "柠檬酸", "氯仿", "二硫化碳", "硝基苯",
    "苯胺", "乙醚", "四氢呋喃", "二甲基甲酰胺", "二甲基亚砜", "吡啶", "喹啉", "萘", "蒽", "沥青",
]

# PubChem PUG View 以英文名称检索。该映射只用于查询名称，不替代返回内容中的化学品事实。
# 混合物（汽油、柴油、煤油、液化石油气、漂白粉、沥青）不强行映射，避免把混合物冒充单一化合物。
PUBCHEM_NAMES = {
    "盐酸": "hydrochloric acid", "硫酸": "sulfuric acid", "硝酸": "nitric acid", "氢氟酸": "hydrofluoric acid",
    "磷酸": "phosphoric acid", "醋酸": "acetic acid", "氢氧化钠": "sodium hydroxide", "氢氧化钾": "potassium hydroxide",
    "氨": "ammonia", "氨水": "ammonium hydroxide", "氯气": "chlorine", "氯化氢": "hydrogen chloride",
    "硫化氢": "hydrogen sulfide", "一氧化碳": "carbon monoxide", "二氧化硫": "sulfur dioxide", "二氧化氮": "nitrogen dioxide",
    "光气": "phosgene", "甲醛": "formaldehyde", "苯": "benzene", "甲苯": "toluene", "二甲苯": "xylene",
    "甲醇": "methanol", "乙醇": "ethanol", "丙酮": "acetone", "乙酸乙酯": "ethyl acetate", "苯乙烯": "styrene",
    "氯乙烯": "vinyl chloride", "苯酚": "phenol", "次氯酸钠": "sodium hypochlorite", "过氧化氢": "hydrogen peroxide",
    "丙烷": "propane", "丁烷": "butane", "电石": "calcium carbide", "硝酸铵": "ammonium nitrate", "过氧乙酸": "peracetic acid",
    "环氧乙烷": "ethylene oxide", "丙烯腈": "acrylonitrile", "丙烯": "propene", "乙烯": "ethene", "乙炔": "acetylene",
    "氢气": "hydrogen", "氧气": "oxygen", "二氧化碳": "carbon dioxide", "一氧化氮": "nitric oxide", "二氧化氯": "chlorine dioxide",
    "氯甲烷": "chloromethane", "二氯甲烷": "dichloromethane", "三氯甲烷": "chloroform", "四氯化碳": "carbon tetrachloride",
    "三氯乙烯": "trichloroethylene", "四氯乙烯": "tetrachloroethylene", "氯苯": "chlorobenzene", "溴甲烷": "bromomethane",
    "氟化氢": "hydrogen fluoride", "溴素": "bromine", "碘": "iodine", "硫磺": "sulfur", "三氧化硫": "sulfur trioxide",
    "硫化钠": "sodium sulfide", "硫酸二甲酯": "dimethyl sulfate", "氰化钠": "sodium cyanide", "氰化钾": "potassium cyanide",
    "氰化氢": "hydrogen cyanide", "砷化氢": "arsine", "磷化氢": "phosphine", "磷酸三丁酯": "tributyl phosphate",
    "黄磷": "white phosphorus", "红磷": "red phosphorus", "五氧化二磷": "phosphorus pentoxide", "氯酸钾": "potassium chlorate",
    "高锰酸钾": "potassium permanganate", "重铬酸钾": "potassium dichromate", "铬酸": "chromic acid", "次氯酸钙": "calcium hypochlorite",
    "氯酸钠": "sodium chlorate", "亚硝酸钠": "sodium nitrite", "硝酸钾": "potassium nitrate", "硝酸钠": "sodium nitrate",
    "硝酸银": "silver nitrate", "过硫酸铵": "ammonium persulfate", "过硫酸钠": "sodium persulfate", "过氧化钠": "sodium peroxide",
    "过氧化钾": "potassium peroxide", "金属钠": "sodium", "金属钾": "potassium", "镁粉": "magnesium", "铝粉": "aluminum",
    "锌粉": "zinc", "碳化钙": "calcium carbide", "硅烷": "silane", "硼烷": "borane", "氯乙酸": "chloroacetic acid",
    "丙烯酸": "acrylic acid", "甲基丙烯酸": "methacrylic acid", "丙烯酸甲酯": "methyl acrylate", "丙烯酸乙酯": "ethyl acrylate",
    "甲苯二异氰酸酯": "toluene diisocyanate", "二苯基甲烷二异氰酸酯": "methylene diphenyl diisocyanate",
    "异氰酸甲酯": "methyl isocyanate", "环氧氯丙烷": "epichlorohydrin", "乙二醇": "ethylene glycol", "二甘醇": "diethylene glycol",
    "正己烷": "hexane", "正庚烷": "heptane", "正辛烷": "octane", "环己烷": "cyclohexane", "环己酮": "cyclohexanone",
    "乙酸乙烯酯": "vinyl acetate", "醋酸乙烯": "vinyl acetate", "乙酸酐": "acetic anhydride", "甲酸": "formic acid",
    "草酸": "oxalic acid", "柠檬酸": "citric acid", "氯仿": "chloroform", "二硫化碳": "carbon disulfide", "硝基苯": "nitrobenzene",
    "苯胺": "aniline", "乙醚": "diethyl ether", "四氢呋喃": "tetrahydrofuran", "二甲基甲酰胺": "dimethylformamide",
    "二甲基亚砜": "dimethyl sulfoxide", "吡啶": "pyridine", "喹啉": "quinoline", "萘": "naphthalene", "蒽": "anthracene",
}

# 补充事故场景同义词，提升公开资料检索召回率。
SEARCH_QUERIES["chemical_leakage"].extend([
    "有毒有害气体泄漏应急处置方案 filetype:pdf",
    "危险源泄漏现场处置方案 filetype:pdf",
    "化学品跑冒滴漏应急预案 filetype:pdf",
    "罐区泄漏事故应急预案 filetype:pdf",
])
SEARCH_QUERIES["poisoning_asphyxia"].extend([
    "急性中毒事故专项应急预案 filetype:pdf",
    "气体中毒事故现场处置方案 filetype:pdf",
    "罐内救援窒息事故应急预案 filetype:pdf",
    "地下管道硫化氢中毒应急预案 filetype:pdf",
])
SEARCH_QUERIES["confined_space"].extend([
    "污水井中毒窒息现场处置方案 filetype:pdf",
    "化粪池中毒窒息应急预案 filetype:pdf",
    "发酵池有限空间事故预案 filetype:pdf",
])
SEARCH_QUERIES["other_accident"].extend([
    "灼烫事故专项应急预案 filetype:pdf",
    "物体打击专项应急预案 filetype:pdf",
    "车辆伤害专项应急预案 filetype:pdf",
])
SEARCH_QUERIES["sds"].extend([f"{chemical} 化学品安全技术说明书 SDS filetype:pdf" for chemical in COMMON_CHEMICALS])

# 公开页面经常没有被搜索结果直接指向附件，这些已确认的政府公开页面用于一层附件发现。
SEED_PAGES = [
    "https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf",
    "https://xxgk.yczf.gov.cn/qtdw_42136/ycxjjjskfq/fdzdgknr/fgwj/202509/P020251223390182355986.pdf",
    "https://www.sxakhidz.gov.cn/Content-2791124.html",
    "https://www.pudong.gov.cn/zwgk/14511.gkml_ywl_aqscgl/2024/305/333854.html",
    "https://www.pudong.gov.cn/zwgk/14528.gkml_ywl_yjgl/2022/316/296897.html",
    "https://www.beijing.gov.cn/zhengce/zhengcefagui/201905/t20190522_57346.html",
    "https://www.yantai.gov.cn/art/2023/7/14/art_43336_3137962.html",
    "https://www.cqcs.gov.cn/zwgk_164/fdzdgknr/yjgl/yjya_yjj/202605/t20260513_15673966.html",
    "https://www.szns.gov.cn/nsqajj/attachment/1/1715/1715358/12799387.pdf",
    "https://www.suzhou.gov.cn/szsrmzf/zfbgswj/202411/23ea91fba0fa4b20a18051e92edd7b6f/files/16280d5b11bb475cb051847a54a047ce.pdf",
    "https://www.pjq.gov.cn/jmpjqyjj/attachment/0/263/263666/2838367.pdf",
    "https://www.huaishang.gov.cn/zfxxgk/public/24521/48127231.html",
    "https://www.genhe.gov.cn/file_hlbe/7/202605/20260508161718037fi5zwd.pdf",
    "https://www.yjglj.sh.gov.cn/xxgk/xxgkml/zcfg/aqbz/20150721/0037-21564.html",
    "https://yjglj.sh.gov.cn/xxgk/zfxxgk/zcwj/yjczyzh/20240620/3a32989712f94ff381f80d25bb798f0c.html",
    "https://www.tjftz.gov.cn/contents/6014/367426.html",
    "https://www.shyp.gov.cn/zhengwu/zwgk-zdjc2024/2025/219/b90f94247ec1466f3a593284c23c194a.html",
    "https://swj.beijing.gov.cn/swdt/tzgg/202111/P020211104461791934756.pdf",
    "https://www.zfcxjw.cq.gov.cn/zwxx_166/gsgg/202402/t20240207_12917703.html",
    "https://www.sxakhidz.gov.cn/UploadFiles/akjyhfj65/file/20250120/20250120143724_8748.pdf",
    "https://jnsafety.jinan.gov.cn/module/download/downfile.jsp?classid=0&filename=e74ab00ad6da408492dae9965df9f5fa.pdf&showname=%E9%99%84%E4%BB%B63%EF%BC%9A%E6%B5%8E%E5%8D%97%E5%B8%82%E5%8D%B1%E9%99%A9%E5%8C%96%E5%AD%A6%E5%93%81%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E5%BA%94%E6%80%A5%E9%A2%84%E6%A1%88.pdf",
    "https://www.chinamine-safety.gov.cn/zfxxgk/fdzdgknr/zqyj_01/202307/W020230721541318426522.pdf",
    "https://www.zj.gov.cn/art/2024/1/1/art_1229017138_59029849.html",
    "https://xhq.nc.gov.cn/xhqrmzf/yjya01/202104/cb67fd3b1329456d9829d2105eb80654.shtml",
    "https://www.jinjiang.gov.cn/xxgk/zdxxgk/zhsgjy/yjya/202206/t20220610_2737049.htm",
    "https://www.sm.gov.cn/smsrmzfbgs/smsrmzf/zfxxgkml/fggzhgfxwj/201901/t20190104_1255746.htm",
    "https://yjgl.sz.gov.cn/zwgk/xxgkml/zcfgjjd/zcfg/content/post_11233862.html",
    # 公开预案补充种子（来源为政府/应急管理部门公开页面）。
    "https://innovation.jinan.gov.cn/cms_files/jcms1/web46/site/attach/0/6030b29d45744adeb14ddecec3c0941b.pdf?fileName=6030b29d45744adeb14ddecec3c0941b.pdf",
    "https://www.dpxq.gov.cn/xxgk/xxgk/yjgl/yjya/content/post_12833757.html",
    "https://www.sz.msa.gov.cn/u/cms/portal_wai/202406/1717483462065.pdf",
    "https://www.szss.gov.cn/sszhss/xxgk/tzgg/content/post_12650224.html",
    "https://www.yuanling.gov.cn/yuanling/c108890/202508/c121e5e76be34805a92c7f069142f826/files/a0bc72cc25484863a97cb0aa160a9998.pdf",
    "https://www.sxakhidz.gov.cn/UploadFiles/akjyhfj65/file/20250120/20250120143521_9471.pdf",
    "https://www.ahhuoshan.gov.cn/group3/M00/8F/56/wKgSG2nzAU-AT2rwABnkjTt36D8637.pdf",
    "https://yjglj.dazhou.gov.cn/news-show-1413.html",
    "https://www.guangde.gov.cn/OpennessContent/show/3293850.html",
    # 应急管理部事故案例汇编/通报页面，可拆分为多个独立案例记录。
    "https://www.mem.gov.cn/xw/yjglbgzdt/202312/t20231212_471660.shtml",
    "https://www.mem.gov.cn/xw/yjglbgzdt/202103/t20210326_382087.shtml",
    "https://www.mem.gov.cn/xw/yjglbgzdt/202108/t20210811_395669.shtml",
    "https://www.mem.gov.cn/xw/bndt/201806/t20180608_229498.shtml",
    "https://www.mem.gov.cn/gk/tzgg/tz/202010/W020201102332723879957.pdf",
    "https://www.guanganqu.gov.cn/zfxxgk/szfwj/1843590689556185088/4xQffnuz.pdf",
    "https://yjgl.gd.gov.cn/attachment/0/531/531412/4252512.pdf",
    "https://www.mem.gov.cn/gk/gwgg/agwzlfl/zjmjjgg/201507/P020190411410755017555.pdf",
    # 公开企业/项目预案样本，用于补足行业和事故类型覆盖（来源 URL 全部保留）。
    "https://lyjs.linyi.gov.cn/virtual_attach_file.vsb?afc=BMz-GbozWVLlVVLf4LaUllio7VfL4TkRLlLbnRlbM4vaoRU0gihFp2hmCIa0oSyiMkybLSybnmv4n7M2U8WRLll8UmMkLNLDozQRnmAVL4rFLNMkU8nkUm6FLz-YLRVfgjfNQmOeo4xEqtw0qIbtpYyPLRU4g4vsLm9sgtA8pURcc&e=.pdf&nid=77287&oid=1438332361&tid=1043",
    "https://www.dicastal.com/Public/txcx/20250328/8.pdf",
    "https://ankebio.com/upload/aqyjyba.pdf",
    "https://www.palum.com/pdf/%E6%B2%BB%E7%90%86/%E8%8D%A3%E9%98%B3%E5%AE%9E%E4%B8%9A%EF%BC%88%E5%8D%97%E9%98%B3%EF%BC%89%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E5%BA%94%E6%80%A5%E9%A2%84%E6%A1%88.pdf",
    "https://zhongruiyaoye.com/home/f/5/mgbjge/resource/2024/06/04/665e6699bce8f.pdf",
    "https://www.rongchang.gov.cn/zwgk_264/zcwj/qzfwj/zcyy/202111/t20211117_9988687.html",
    "https://www.jhtcl.com/nr.jsp",
    "https://zwgk.shcn.gov.cn/xxgk/anquan-ygjzdgz/2024/152/73357/3831c4820e7a45608464784752f70e49.pdf",
    "https://www.caidian.gov.cn/zwgk/xxgkml/zdly/aqsc_/202211/t20221104_2084477.shtml",
    "https://www.beijing.gov.cn/zhengce/zhengcefagui/201905/t20190522_59930.html",
    "https://yjj.cq.gov.cn/zwgk_230/fdzdgknr/yjyayjxx/yjya/202011/t20201130_8510532.html",
    "https://www.xuanhan.gov.cn/xxgk-show-84688.html",
    "https://www.sz.msa.gov.cn/yjya/277470.jhtml",
    "https://www.hunan.gov.cn/hnszf/xxgk/yjgl/yuan/bmya/201301/t20130108_4694140.html",
    "https://www.sxakhidz.gov.cn/UploadFiles/akjyhfj65/file/20250120/20250120143644_0203.pdf",
    "https://hnls.gov.cn/upload/files/2021/12/1618157181.pdf",
    "https://www.zge.gov.cn/xxgk_zge/zgeqrmzf/202312/t20231220_3544536.html",
    "https://www.yishui.gov.cn/info/43630/450350.htm",
    "https://www.bjft.gov.cn/ftq/yjgl/202308/a16e2423f2ca4225a1fb73b787e6cba3.shtml",
    "https://www.hengyang.gov.cn/DFS/file/2026/03/20/202603201316117575azwke.pdf",
    "https://novelis.com/wp-content/uploads/2025/04/10-%E8%AF%BA%E8%B4%9D%E4%B8%BD%E6%96%AF%E9%93%9D%E4%B8%9A%EF%BC%88%E9%95%87%E6%B1%9F%EF%BC%89%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E5%BA%94%E6%80%A5%E9%A2%84%E6%A1%88.pdf",
    "https://dub-auto.com/Public/Uploads/20240527/6653d9ff6d574.pdf",
    "https://wwwfile.sxylny.com/2024/0722/20240722041533505.pdf",
    "https://zlrm.chinalco.com.cn/shzr/hjbh/202506/t20250613_149222.html",
    "https://www.safehoo.com/Emergency/Case/hg/202010/5615820.shtml",
    # 中毒/窒息与有限空间补充种子：优先采用政府、企业官网或公开行业资料页。
    "https://www.smzjm.com/storage/posts/20240715/1721007750330849.pdf",
    "https://www.shqp.gov.cn/shqp/shqp/upload/202211/1124_140618_929.pdf",
    "https://www.jiningcoal.com/uploads/file/2308/1692084073272778.pdf",
    "https://www.safehoo.com/Emergency/Case/hg/202301/5695084.shtml",
    "https://www.safehoo.com/Emergency/Case/hg/202101/5627814.shtml",
    "https://www.sdclny.com/uploads/allimg/201108/%E6%BB%95%E5%B7%9E%E5%B8%82%E4%B8%9C%E5%A4%A7%E7%9F%BF%E4%B8%9A%E6%9C%89%E9%99%90%E8%B4%A3%E4%BB%BB%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E7%8E%B0%E5%9C%BA%E5%A4%84%E7%BD%AE%E6%96%B9%E6%A1%88.pdf",
    "https://max.book118.com/html/2025/0619/6132142041011145.shtm",
    "https://max.book118.com/html/2024/0521/7142144144006111.shtm",
    "https://max.book118.com/html/2024/1012/6241010124010232.shtm",
    "https://ww.ahsrst.cn/a/202301/715514.html",
    "https://www.weidong.gov.cn/upload/files/2021/11/2316519631.pdf",
    "https://www.huian.gov.cn/zwgk/zdxxgk/aqsc/aqyhpgjst/202206/W020220614626657413837.pdf",
    "https://www.dxh.gov.cn/ZWGK/bmxxgk/bwbjxxgk/qyjglj/qtxxgk/202010/P020250513393221078327.pdf",
    "https://www.hnziyang.gov.cn/18534/18525/18457/18403/content_1777993.html",
    "https://jnwater.jinan.gov.cn/col126159/art/2025/art_126159_4787551.html",
    "https://xxgk.yczf.gov.cn/qtdw_42136/ycxjjjskfq/fdzdgknr/fgwj/202509/P020250910394633852083.pdf",
    "https://www.bzko.com/std/new237404.html",
    "https://www.bzko.com/std/255700.html",
    "https://www.whhumon.com/data/upload/202306/1687481868130098.pdf",
    "https://www.huarong.gov.cn/uploadfiles/202310/2023100808463339405.pdf",
    "https://www.yjijy.com/post/43698.html",
    "https://max.book118.com/html/2021/1014/8114126057004020.shtm",
    "https://max.book118.com/html/2021/0616/6045121043003201.shtm",
    "https://www.huasugroup.com/sites/default/files/%E8%8B%8F%E5%B7%9E%E5%8D%8E%E8%8B%8F%E5%A1%91%E6%96%99%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E5%BA%94%E6%80%A5%E6%95%91%E6%8F%B4%E9%A2%84%E6%A1%88%EF%BC%882021.6%E6%9C%80%E6%96%B0%EF%BC%89.pdf",
    "https://www.renrendoc.com/paper/323483383.html",
    "https://www.taodocs.com/p-1153038973.html",
    "https://www.sinolube.com/Upload/file/20211104/20211104102915_3349.pdf",
    "https://www.sdclny.com/static/upload/file/20240521/1716282342307372.pdf",
    "https://haomei-alu.com/ckeditor/upload/file/1730170746801603.pdf",
    "https://dlzx.pku.edu.cn/gzzd/yjya/index.htm",
    "https://www.renrendoc.com/p-60794385.html",
    "https://oss.lcweb01.cn/jzt/3318/file/20240819/247959232e983b5ec3255bf007f1a000.pdf",
    "https://www.dicastal.com/Public/txcx/2025071801/34.pdf",
    "https://www.zhenping.gov.cn/ifile/20260123/17691396329583m3TI3cm.pdf",
    "https://www.lingwen.com/fangan/251785.html",
    "https://www.baofengenergy.com/user_tm/bjq2/attached/file/20240524/20240524163141884188.pdf",
    "https://www.wenkub.com/doc-313462482.html",
    # 法规/标准补充种子（官方规章、办法、公告和标准附件）。
    "https://www.mee.gov.cn/gzk/gz/202112/t20211211_963792.shtml",
    "https://www.mee.gov.cn/gzk/gz/202112/t20211210_963741.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/202508/t20250804_553279.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/202501/t20250126_513844.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/202304/t20230427_449131.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/202304/t20230417_448156.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/202111/t20211101_401284.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/202303/t20230329_446050.shtml",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/202405/W020240508698465710475.pdf",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/201602/P020220630402117472444.pdf",
    "https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/201602/P020220630402119861756.pdf",
]

# 上一阶段已确认的官方法规资料可作为本地输入重新解析，但仍保留其原始官方 URL。
LOCAL_MANIFESTS = [ROOT / "local_manifests" / "资料清单.json"]

INDUSTRY_RULES = {
    "chemical": ["化工", "危险化学品", "危化品", "储罐", "化学品", "石化"],
    "manufacturing": ["工贸", "制造", "机械", "冶金", "有色", "建材", "纺织"],
    "construction": ["建筑", "施工", "项目部", "工程", "隧道", "市政"],
    "mining": ["矿山", "煤矿", "井下", "矿井"],
    "environmental": ["污水", "环保", "生态环境", "环境事件", "废水", "危废"],
    "education": ["学校", "高校", "大学", "实验室"],
}

RISK_RULES = {
    "leakage": ["泄漏", "泄露", "跑冒滴漏", "储罐泄漏", "阀门泄漏"],
    "poisoning": ["中毒", "有毒气体", "硫化氢", "一氧化碳", "氨气"],
    "asphyxia": ["窒息", "缺氧", "有限空间", "受限空间", "氧含量"],
    "fire_explosion": ["火灾", "爆炸", "燃烧", "闪燃", "易燃"],
    "environmental_pollution": ["突发环境事件", "环境污染", "污染物", "水体", "土壤"],
}

KEYWORD_RULES = [
    "危险化学品", "泄漏", "中毒", "窒息", "有限空间", "受限空间", "硫化氢", "一氧化碳",
    "液氨", "氯气", "甲醇", "盐酸", "硫酸", "硝酸", "氢氧化钠", "苯", "通风", "检测",
    "呼吸防护", "应急救援", "现场处置", "洗消", "疏散", "监护", "事故案例", "安全技术说明书",
] + COMMON_CHEMICALS


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip(" .")
    return value[:100] or "未命名资料"


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_public_host(host: str) -> bool:
    return host.endswith((".gov.cn", ".edu.cn", ".org.cn", ".gov", ".edu")) or host in {
        "www.mem.gov.cn", "www.mee.gov.cn", "www.npc.gov.cn", "www.samr.gov.cn",
        "openstd.samr.gov.cn",
    }


def decode_search_url(url: str) -> str:
    if "uddg=" in url:
        values = parse_qs(urlparse(url).query).get("uddg")
        if values:
            return unquote(values[0])
    return url if url.startswith("http") else "https:" + url


def search_web(query: str, allow_fallback: bool = True) -> list[dict[str, str]]:
    """优先使用 DuckDuckGo，遇到限流时回退到搜狗 HTML 结果。"""
    endpoint = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    results: list[dict[str, str]] = []
    try:
        response = requests.get(endpoint, headers={"User-Agent": USER_AGENT}, timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a.result__a")[:MAX_RESULTS_PER_QUERY]:
            url = decode_search_url(anchor.get("href", ""))
            if url.startswith("http"):
                results.append({"title": anchor.get_text(" ", strip=True), "url": url, "query": query})
    except Exception:
        results = []
    if len(results) >= 3 or not allow_fallback:
        return results
    # 搜狗结果页在 DuckDuckGo 被限流时作为第二搜索通道；后续下载阶段仍会校验公开 URL。
    fallback = "https://www.sogou.com/web?query=" + quote_plus(query)
    response = requests.get(fallback, headers={"User-Agent": USER_AGENT}, timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for anchor in soup.select("h3 a, .vr-title a")[:MAX_RESULTS_PER_QUERY]:
        url = decode_search_url(urljoin("https://www.sogou.com", anchor.get("href", "")))
        if url.startswith("http"):
            results.append({"title": anchor.get_text(" ", strip=True), "url": url, "query": query})
    return results


class FetchResult:
    """统一不同抓取器的最小响应接口，解析层不绑定某个网络库。"""

    def __init__(self, url: str, content: bytes, headers: dict[str, str] | None = None):
        self.url = url
        self.content = content
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def raise_for_status(self) -> None:
        return None


def fetch_by_requests(url: str) -> FetchResult:
    """通用网页抓取器，处理搜索结果中的普通 HTML 页面。"""
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,application/msword,*/*"},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return FetchResult(response.url, response.content, dict(response.headers))


def fetch_by_curl(url: str) -> FetchResult:
    """二进制附件抓取器：对部分政府站点的 TLS/重定向更稳定。"""
    result = subprocess.run(
        ["curl", "--location", "--fail", "--silent", "--show-error", "--connect-timeout", "12", "--max-time", "25", "-A", USER_AGENT, url],
        check=True,
        capture_output=True,
        timeout=35,
    )
    suffix = Path(urlparse(url).path).suffix.lower()
    content_type = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(suffix, "application/octet-stream")
    return FetchResult(url, result.stdout, {"content-type": content_type})


def fetch_by_trafilatura(url: str) -> FetchResult:
    """官方网页抓取器：使用 Trafilatura 获取正文页原始 HTML。"""
    raw_html = trafilatura.fetch_url(url)
    if not raw_html:
        raise RuntimeError("Trafilatura 未返回网页正文")
    return FetchResult(url, raw_html.encode("utf-8"), {"content-type": "text/html; charset=utf-8"})


def fetch(url: str) -> FetchResult:
    """按资源类型和站点路由抓取器：搜索页、官方 HTML、二进制附件分别处理。"""
    host = host_of(url)
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in ALLOWED_DOC_EXTENSIONS:
        return fetch_by_curl(url)
    if is_public_host(host):
        try:
            return fetch_by_trafilatura(url)
        except Exception:
            # 个别站点会拦截 Trafilatura 的请求，回退到普通请求抓取器。
            return fetch_by_requests(url)
    return fetch_by_requests(url)


def _pubchem_text(value: object) -> list[str]:
    """提取 PUG View 的可读字段，保留原始 API 返回的安全信息，不自行补写结论。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_pubchem_text(item))
        return result
    if not isinstance(value, dict):
        return []
    result: list[str] = []
    for key in ("String", "StringWithMarkup", "Number", "Unit", "Boolean"):
        if key not in value:
            continue
        item = value[key]
        if key == "StringWithMarkup" and isinstance(item, list):
            result.extend(str(entry.get("String", "")) for entry in item if isinstance(entry, dict) and entry.get("String"))
        else:
            result.extend(_pubchem_text(item))
    return [item.strip() for item in result if item and item.strip()]


def _flatten_pubchem_sections(sections: list[dict], lines: list[str] | None = None) -> str:
    lines = lines if lines is not None else []
    for section in sections:
        heading = str(section.get("TOCHeading", "")).strip()
        if heading:
            lines.append(f"【{heading}】")
        for information in section.get("Information", []):
            values = _pubchem_text(information.get("Value", {}))
            label = str(information.get("Name", "")).strip()
            for value in values:
                if label:
                    lines.append(f"{label}: {value}")
                else:
                    lines.append(value)
        _flatten_pubchem_sections(section.get("Section", []), lines)
    return clean_text("\n".join(lines))


def fetch_pubchem_sds(chemical: str) -> dict | None:
    """从 PubChem PUG REST/PUG View 获取单一化学品安全数据，作为 SDS 补充抓取器。"""
    english_name = PUBCHEM_NAMES.get(chemical)
    if not english_name:
        return None
    try:
        cid_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(english_name, safe='')}/cids/JSON"
        cid_response = requests.get(cid_url, headers={"User-Agent": USER_AGENT}, timeout=SEARCH_TIMEOUT)
        cid_response.raise_for_status()
        cids = cid_response.json().get("IdentifierList", {}).get("CID", [])
        if not cids:
            return None
        cid = int(cids[0])
        data_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON?heading=Safety%20and%20Hazards"
        data_response = requests.get(data_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        data_response.raise_for_status()
        payload = data_response.json()
        record = payload.get("Record", {})
        content = _flatten_pubchem_sections(record.get("Section", []))
        if len(content) < 500:
            return None
        digest = hashlib.sha256(data_response.content).hexdigest()
        raw_path = RAW_DIR / f"PubChem-{safe_name(chemical)}-{digest[:12]}.json"
        if not raw_path.exists():
            raw_path.write_bytes(data_response.content)
        source_url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
        full_content = f"化学品：{chemical}\n英文名称：{english_name}\nPubChem CID：{cid}\n\n{content}"
        return {
            "title": f"{chemical} PubChem安全数据",
            "source": host_of(source_url),
            "url": source_url,
            "type": "knowledge",
            "category": "SDS",
            "risk_category": infer_risks(full_content),
            "risk_type": infer_risks(full_content),
            "industry": "chemical",
            "plan_type": "",
            "enterprise": "",
            "published_at": "",
            "content": full_content,
            "keywords": sorted(set(infer_keywords(full_content) + [chemical])),
            "quality_score": quality_score(full_content, source_url, "knowledge"),
            "file": str(raw_path),
            "content_bytes": len(full_content.encode("utf-8")),
            "collected_at": now_iso(),
        }
    except Exception:
        return None


def collect_pubchem_sds() -> list[dict]:
    tasks = list(PUBCHEM_NAMES)
    with ThreadPoolExecutor(max_workers=int(os.getenv("EMERGENCY_PUBCHEM_WORKERS", "8"))) as executor:
        return [record for record in executor.map(fetch_pubchem_sds, tasks) if record]


def extension_for(url: str, response: FetchResult) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in ALLOWED_DOC_EXTENSIONS:
        return suffix
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or response.content.startswith(b"%PDF"):
        return ".pdf"
    if "word" in content_type or "msword" in content_type:
        return ".doc"
    if "officedocument" in content_type or response.content.startswith(b"PK"):
        return ".docx"
    return ".html"


def discover_attachment_urls(page_url: str, raw_html: str) -> list[str]:
    """只发现同页或同类公共站点的一层附件，避免无限爬取。"""
    urls: list[str] = []
    tree = html.fromstring(raw_html)
    for href in tree.xpath("//a/@href"):
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        suffix = Path(parsed.path).suffix.lower()
        # 政府站点优先；对普通公开企业站只允许抓取同一站点附件，避免跨站扩散。
        if suffix not in ALLOWED_DOC_EXTENSIONS or (not is_public_host(host_of(absolute)) and host_of(absolute) != host_of(page_url)):
            continue
        if absolute not in urls:
            urls.append(absolute)
    return urls[:8]


def clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not re.fullmatch(r"[-—_ ]*\d+[-—_ ]*", line)]
    counts = Counter(lines)
    # 删除高频页眉页脚，但保留正文中可能重复出现的章节标题。
    lines = [line for line in lines if not (counts[line] >= 4 and len(line) <= 40)]
    return "\n".join(lines).strip()


def parse_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_docx(path: Path) -> str:
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def parse_doc(path: Path) -> str:
    """DOC 使用本机 LibreOffice 转文本，失败时仍保留原始文件和错误状态。"""
    with tempfile.TemporaryDirectory(prefix="emergency_plan_doc_") as temp_dir:
        command = [
            "soffice", "--headless", "--convert-to", "txt", "--outdir", temp_dir, str(path)
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=90)
        converted = Path(temp_dir) / f"{path.stem}.txt"
        return converted.read_text(encoding="utf-8", errors="ignore")


def parse_file(path: Path, raw_html: str | None = None) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return clean_text(parse_pdf(path))
    if suffix == ".docx":
        return clean_text(parse_docx(path))
    if suffix == ".doc":
        return clean_text(parse_doc(path))
    if raw_html is not None:
        extracted = trafilatura.extract(raw_html, include_tables=True, include_links=False) or ""
        if not extracted:
            extracted = BeautifulSoup(raw_html, "html.parser").get_text("\n")
        return clean_text(extracted)
    return clean_text(path.read_text(encoding="utf-8", errors="ignore"))


def _catalog_cell(value: object) -> str:
    """将目录表单元格转成可审计文本，特别处理 Excel 误识别为日期的 CAS 号。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        # Excel 可能把形如 7789-09-5 的 CAS 号读成日期；保留原数字组成而不补写化学事实。
        return f"{value.year:04d}-{value.month:02d}-{value.day}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\u00a0", " ").strip()


def _catalog_risks(hazard_characteristics: list[str], chemical: str) -> list[str]:
    """只依据表内危险性类别生成检索标签，不把标签当作处置建议。"""
    hazard_text = f"{chemical} {' '.join(hazard_characteristics)}"
    risks = infer_risks(hazard_text)
    if any(word in hazard_text for word in ("急性毒性", "致癌性", "生殖毒性", "致突变性")):
        risks.append("poisoning")
    if "腐蚀" in hazard_text:
        risks.append("corrosive")
    if "氧化性" in hazard_text or "有机过氧化物" in hazard_text:
        risks.append("oxidizer")
    return list(dict.fromkeys(risks))


def collect_local_chemical_catalog() -> tuple[list[dict], dict]:
    """读取用户提供的危险化学品目录，保留目录序号、表名和原始行定位。"""
    summary: dict = {
        "source_file": str(CHEMICAL_CATALOG_SOURCE),
        "copied_file": str(CHEMICAL_CATALOG_COPY),
        "source_url": CHEMICAL_CATALOG_URL,
        "selected_sheet": "",
        "sheet_summaries": [],
        "entry_count": 0,
        "status": "missing",
    }
    if not CHEMICAL_CATALOG_SOURCE.exists():
        return [], summary

    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    # 附件是输入资料，不修改用户原文件；目标目录保留一份可随流水线复现的副本。
    if not CHEMICAL_CATALOG_COPY.exists() or CHEMICAL_CATALOG_COPY.stat().st_size != CHEMICAL_CATALOG_SOURCE.stat().st_size:
        shutil.copy2(CHEMICAL_CATALOG_SOURCE, CHEMICAL_CATALOG_COPY)

    workbook = load_workbook(CHEMICAL_CATALOG_COPY, read_only=True, data_only=True)
    selected_sheet = "Sheet2" if "Sheet2" in workbook.sheetnames else workbook.sheetnames[0]
    summary["selected_sheet"] = selected_sheet
    entries: list[dict] = []
    for worksheet in workbook.worksheets:
        numeric_serials = 0
        row_count = 0
        for row in worksheet.iter_rows(min_row=2, values_only=True):
            row_count += 1
            serial_text = _catalog_cell(row[0] if len(row) > 0 else None)
            if re.fullmatch(r"\d+", serial_text):
                numeric_serials += 1
        summary["sheet_summaries"].append({
            "sheet": worksheet.title,
            "rows_after_header": row_count,
            "numeric_serial_rows": numeric_serials,
        })
        if worksheet.title != selected_sheet:
            continue

        current_catalog_no = ""
        for row_number, row in enumerate(worksheet.iter_rows(min_row=2, values_only=True), 2):
            values = [_catalog_cell(row[index] if index < len(row) else None) for index in range(7)]
            serial_text, chemical, alias, english_name, cas, hazard, remark = values
            numeric_serial = serial_text if re.fullmatch(r"\d+", serial_text) else ""
            is_note = serial_text.startswith("注") or chemical.startswith("注")
            # 2828 号条目包含多个无序号子项；子项继承上一条目录序号，但单独保留为检索条目。
            if numeric_serial:
                current_catalog_no = numeric_serial
            if chemical and not is_note and chemical != "常见品种如下：":
                entry = {
                    "catalog_no": current_catalog_no,
                    "chemical": chemical,
                    "alias": alias,
                    "english_name": english_name,
                    "cas": cas,
                    "hazard_characteristics": [hazard] if hazard else [],
                    "remark": remark,
                    "source": "用户提供附件：危险化学品目录指南(2)(1).xlsx",
                    "source_url": CHEMICAL_CATALOG_URL,
                    "source_file": str(CHEMICAL_CATALOG_COPY),
                    "source_sheet": worksheet.title,
                    "source_locator": f"{worksheet.title}!A{row_number}:G{row_number}",
                    "category": "hazardous_chemical_catalog",
                    "leak_response": [],
                    "ppe": [],
                    "first_aid": [],
                    "forbidden_actions": [],
                }
                entries.append(entry)
                continue
            # 空白品名行是同一化学品的附加危险性类别，合并到最近的目录条目。
            if entries and hazard and not chemical and not is_note:
                entries[-1]["hazard_characteristics"].append(hazard)

    # 同名/同 CAS 的重复行合并危险性类别，避免把 Excel 的重复展示行当成多个化学品。
    merged: dict[tuple[str, str, str, str, str], dict] = {}
    for entry in entries:
        key = (
            entry["catalog_no"], entry["chemical"], entry["alias"], entry["english_name"], entry["cas"]
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = entry
            continue
        existing["hazard_characteristics"] = list(dict.fromkeys(
            existing["hazard_characteristics"] + entry["hazard_characteristics"]
        ))
    entries = list(merged.values())
    for entry in entries:
        entry["hazard_characteristics"] = list(dict.fromkeys(entry["hazard_characteristics"]))
        entry["risk_category"] = _catalog_risks(entry["hazard_characteristics"], entry["chemical"])
        aliases = [item.strip() for item in re.split(r"[；;]", entry["alias"]) if item.strip()]
        entry["keywords"] = list(dict.fromkeys(
            ["危险化学品", entry["chemical"], *aliases, *entry["hazard_characteristics"]]
        ))
        entry["collected_at"] = now_iso()
    summary["entry_count"] = len(entries)
    summary["status"] = "ok"
    return entries, summary


def build_chemical_catalog_record(entry: dict) -> dict:
    """把目录条目转为统一 records 记录，处置字段留空以避免从目录推断措施。"""
    hazards = "；".join(entry["hazard_characteristics"]) or "未在该行填写"
    content = "\n".join([
        "资料类型：危险化学品目录条目",
        f"目录序号：{entry['catalog_no']}",
        f"品名：{entry['chemical']}",
        f"别名：{entry['alias'] or '无'}",
        f"英文名：{entry['english_name'] or '无'}",
        f"CAS号：{entry['cas'] or '未填写'}",
        f"危险性类别：{hazards}",
        f"备注：{entry['remark'] or '无'}",
        "说明：本条目仅反映目录字段；泄漏处置、个体防护、急救和禁忌动作需引用对应 SDS 或法规，不在此处补写。",
    ])
    return {
        "title": f"危险化学品目录｜{entry['chemical']}",
        "source": entry["source"],
        "url": entry["source_url"],
        "source_locator": entry["source_locator"],
        "type": "knowledge",
        "category": "chemical_catalog",
        "risk_category": entry["risk_category"],
        "risk_type": entry["risk_category"],
        "industry": "chemical",
        "plan_type": "",
        "enterprise": "",
        "published_at": "",
        "content": content,
        "keywords": entry["keywords"],
        # 本地附件的权威性未由网络来源验证，质量分不冒充官方认证。
        "quality_score": 50,
        "file": entry["source_file"],
        "content_bytes": len(content.encode("utf-8")),
        "collected_at": entry["collected_at"],
    }


def find_title(text: str, fallback: str) -> str:
    for line in text.splitlines()[:80]:
        if 4 <= len(line) <= 100 and re.search(r"(应急预案|现场处置方案|安全技术说明书|管理办法|规定|导则|指南)", line):
            return line.strip(" ：:")
    return fallback.strip() or "未命名公开资料"


def find_date(text: str) -> str:
    match = re.search(r"((?:19|20)\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", text[:8000])
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return ""


def infer_industry(text: str) -> str:
    scores = {industry: sum(text.count(word) for word in words) for industry, words in INDUSTRY_RULES.items()}
    best, score = max(scores.items(), key=lambda item: item[1])
    return best if score else "general"


def infer_risks(text: str) -> list[str]:
    return [risk for risk, words in RISK_RULES.items() if any(word in text for word in words)] or ["general"]


def infer_keywords(text: str) -> list[str]:
    return [keyword for keyword in KEYWORD_RULES if keyword in text]


def infer_category(title: str, text: str, query_type: str = "") -> tuple[str, str]:
    combined = f"{title}\n{text[:12000]}"
    headline = f"{title}\n{text[:1800]}"
    if query_type == "regulation":
        return "regulation", "regulation"
    if query_type == "sds" or "安全技术说明书" in combined or re.search(r"\bSDS\b", combined, re.I):
        return "SDS", "knowledge"
    if query_type == "accident_case" or re.search(r"(典型事故案例|事故案例汇编|事故调查报告)", combined):
        return "accident_case", "accident_case"
    # 法规/标准标题优先于正文中的“应急预案”引用，避免把规定、导则和手册误当成预案样本。
    if re.search(r"(管理办法|安全规定|国家标准|行业标准|编制导则|条例|指导手册|安全标准)", title):
        return "regulation", "regulation"
    # 预案检索词优先级高于文档中引用的法规名称，避免把预案样本误判成法规。
    plan_signal = re.search(r"(应急预案|现场处置方案|专项预案)", combined)
    topic_window = f"{title}\n{text[:3500]}"
    if plan_signal and re.search(r"(有限空间|受限空间)[\s\S]{0,35}(专项应急预案|现场处置方案)", topic_window):
        return "confined_space", "emergency_plan"
    if plan_signal and re.search(r"(中毒和窒息|中毒窒息)[\s\S]{0,35}(专项应急预案|现场处置方案)", topic_window):
        return "poisoning_asphyxia", "emergency_plan"
    if plan_signal and re.search(r"(危险化学品|危化品)[\s\S]{0,35}(泄漏|事故)[\s\S]{0,35}(专项应急预案|现场处置方案)", topic_window):
        return "chemical_leakage", "emergency_plan"
    if query_type in {"chemical_leakage", "poisoning_asphyxia", "confined_space", "other_accident", "seed"} and plan_signal:
        # 标题是资料主题的最强信号，先处理明确的中毒/窒息/有限空间和泄漏名称，
        # 再使用正文词频，避免引用法规或其它事故章节造成误分类。
        if re.search(r"(有限空间|受限空间)", title):
            return "confined_space", "emergency_plan"
        if re.search(r"(中毒|窒息)", title):
            return "poisoning_asphyxia", "emergency_plan"
        if re.search(r"(危险化学品|危化品|液氨|氯气|甲醇).*(泄漏|事故)", title):
            return "chemical_leakage", "emergency_plan"
        # 其他事故类型也以标题为准；不能因为正文的通用风险说明提到“中毒”
        # 就把淹溺、起重等预案误归入中毒窒息类别。
        if re.search(
            r"(火灾|爆炸|触电|机械伤害|高处坠落|坍塌|车辆伤害|起重伤害|物体打击|淹溺|灼烫|特种设备|锅炉爆炸|容器爆炸|建筑施工|自然灾害)",
            title,
        ):
            return "other_accident", "emergency_plan"
        category_scores = {
            "chemical_leakage": sum(headline.count(word) for word in ("危险化学品", "泄漏", "液氨", "氯气", "储罐")),
            "confined_space": sum(headline.count(word) for word in ("有限空间", "受限空间", "污水池", "化粪池", "罐内作业")),
            "poisoning_asphyxia": sum(headline.count(word) for word in ("中毒和窒息", "中毒", "窒息", "硫化氢", "一氧化碳")),
            "other_accident": sum(headline.count(word) for word in ("火灾", "爆炸", "触电", "机械伤害", "高处坠落", "坍塌")),
        }
        best_category, best_score = max(category_scores.items(), key=lambda item: item[1])
        query_hint = {"chemical_leakage": "chemical_leakage", "confined_space": "confined_space", "poisoning_asphyxia": "poisoning_asphyxia", "other_accident": "other_accident"}.get(query_type)
        if best_score == 0:
            best_category = query_hint or "other_accident"
        elif query_hint and best_score == category_scores.get(query_hint, 0):
            best_category = query_hint
        return best_category, "emergency_plan"
    if plan_signal and not re.search(r"(管理办法|安全规定|国家标准|行业标准|条例)$", title):
        risks = infer_risks(headline)
        if "leakage" in risks:
            return "chemical_leakage", "emergency_plan"
        if "有限空间" in title or "受限空间" in title or "有限空间" in headline:
            return "confined_space", "emergency_plan"
        if "poisoning" in risks or "asphyxia" in risks:
            return "poisoning_asphyxia", "emergency_plan"
        return "other_accident", "emergency_plan"
    if query_type == "regulation" or re.search(r"(管理办法|安全规定|国家标准|行业标准|编制导则|条例)", combined):
        return "regulation", "regulation"
    if re.search(r"(模板|编制指南|编制导则)", combined):
        return "template", "template"
    return "knowledge", "knowledge"


def infer_plan_type(text: str) -> str:
    for label in ("现场处置方案", "专项应急预案", "综合应急预案", "总体应急预案"):
        if label in text[:6000]:
            return label
    return "公开应急资料"


def infer_company(text: str, title: str) -> str:
    candidate_text = f"{title}\n{text[:5000]}"
    matches = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9]{2,35}(?:有限公司|有限责任公司|集团公司|化工厂|化工园区|项目部|矿业公司))", candidate_text)
    for match in matches:
        if match not in {"危险化学品有限公司", "应急管理有限公司"}:
            return match
    return ""


def quality_score(text: str, source_url: str, category: str) -> int:
    official = is_public_host(host_of(source_url))
    score = 20 if official else 8
    if len(text) >= 8000:
        score += 20
    if re.search(r"(应急处置|处置措施|现场处置|救援程序)", text):
        score += 20
    if re.search(r"(依据|法律法规|标准|编制说明)", text):
        score += 15
    if re.search(r"(事故类型|危险因素|危害程度|事故情景)", text):
        score += 15
    if category == "emergency_plan" and re.search(r"(组织机构|应急保障|预警|响应)", text):
        score += 10
    return min(score, 100)


def save_binary(url: str, response: requests.Response, title: str) -> tuple[Path, str]:
    extension = extension_for(url, response)
    digest = hashlib.sha256(response.content).hexdigest()
    path = RAW_DIR / f"{safe_name(title)}-{digest[:12]}{extension}"
    if not path.exists():
        path.write_bytes(response.content)
    return path, digest


def find_cached_raw(candidate: dict[str, str]) -> Path | None:
    """网络请求失败时复用本地同名原始文件，避免重复下载并恢复中断批次。"""
    title_hint = candidate.get("title", "") or Path(urlparse(candidate.get("url", "")).path).stem
    prefix = safe_name(title_hint)
    matches = sorted(
        path for path in RAW_DIR.glob(f"{prefix}-*")
        if path.is_file() and path.suffix.lower() in ALLOWED_DOC_EXTENSIONS | {".html"}
    )
    return matches[0] if matches else None


def build_record(path: Path, source_url: str, title_hint: str, query_type: str, raw_html: str | None = None) -> dict:
    text = parse_file(path, raw_html=raw_html)
    title = find_title(text, title_hint or path.stem)
    category, record_type = infer_category(title, text, query_type)
    risks = infer_risks(text)
    record = {
        "title": title,
        "source": host_of(source_url),
        "url": source_url,
        "type": record_type,
        "category": category,
        "risk_category": risks,
        "risk_type": risks,
        "industry": infer_industry(text),
        "plan_type": infer_plan_type(text) if record_type == "emergency_plan" else "",
        "enterprise": infer_company(text, title),
        "published_at": find_date(text),
        "content": text,
        "keywords": infer_keywords(text),
        "quality_score": quality_score(text, source_url, record_type),
        "file": str(path),
        "content_bytes": len(text.encode("utf-8")),
        "collected_at": now_iso(),
    }
    return record


def split_accident_records(records: list[dict]) -> list[dict]:
    """将官方“案例汇编/一批典型案例”页面拆成独立案例，保留同一来源 URL。"""
    expanded: list[dict] = []
    marker_pattern = re.compile(r"(?m)^(?P<marker>(?:\d{1,2}[.、]\s*|[一二三四五六七八九十]+[、.])[^\n]{4,140})$")
    for record in records:
        content = record.get("content", "")
        is_case_bundle = bool(re.search(r"(典型事故案例|事故案例选编|公布一批.*案例|盲目施救导致伤亡扩大)", content))
        markers = list(marker_pattern.finditer(content)) if is_case_bundle else []
        segments: list[dict] = []
        for index, match in enumerate(markers):
            start = match.start()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
            segment = content[start:end].strip()
            if len(segment) < 300:
                continue
            title_line = re.sub(r"[.．…·\s]+(?:\d+|[一二三四五六七八九十]+)?$", "", match.group("marker").strip(" ："))
            item = dict(record)
            item["title"] = f"{record.get('title', '事故案例')}｜{title_line[:100]}"
            item["type"] = "accident_case"
            item["category"] = "accident_case"
            item["risk_category"] = infer_risks(segment)
            item["risk_type"] = item["risk_category"]
            item["content"] = segment
            item["keywords"] = infer_keywords(segment)
            item["quality_score"] = quality_score(segment, item.get("url", ""), "accident_case")
            item["content_bytes"] = len(segment.encode("utf-8"))
            segments.append(item)
        if len(segments) >= 2:
            expanded.extend(segments)
        else:
            # 历史记录也按标题重新校正一次，避免重复运行后保留旧的错误类别。
            refreshed = dict(record)
            category, record_type = infer_category(refreshed.get("title", ""), refreshed.get("content", ""), "seed")
            if record_type == "emergency_plan":
                refreshed["category"] = category
                refreshed["type"] = record_type
                refreshed["risk_category"] = infer_risks(refreshed.get("content", ""))
                refreshed["risk_type"] = refreshed["risk_category"]
            expanded.append(refreshed)
    return expanded


def split_plan_records(records: list[dict]) -> list[dict]:
    """将一个公开汇编中的多个专项预案拆成可单独检索的预案样本。"""
    expanded: list[dict] = []
    marker_pattern = re.compile(r"(?m)^(?P<marker>[^\n]{4,240}(?:专项应急预案|现场处置方案|现场处置要点)[^\n]{0,120})$")
    numbered = re.compile(r"^(?:\d{1,2}[.、\s]|[一二三四五六七八九十]+[、.\s]|[（(]\d+[）)])")
    topic_prefix = re.compile(r"^(?:危险化学品|危化品|中毒和窒息|中毒窒息|职业病危害|有毒有害气体|有限空间|受限空间|火灾|爆炸|触电|机械伤害|高处坠落|坍塌|车辆伤害|自然灾害)")
    for record in records:
        if record.get("type") != "emergency_plan":
            expanded.append(record)
            continue
        markers: list[re.Match] = []
        for match in marker_pattern.finditer(record.get("content", "")):
            title_line = re.sub(r"[.．…·\s]+(?:\d+|[一二三四五六七八九十]+)?$", "", match.group("marker").strip(" ："))
            normalized = re.sub(r"\s+", "", title_line)
            if normalized in {"生产安全事故专项应急预案", "应急预案专项应急预案"}:
                continue
            if not (numbered.match(title_line) or topic_prefix.match(re.sub(r"^[一二三四五六七八九十]+[、.\s]*", "", title_line))):
                continue
            markers.append(match)
        segments: list[dict] = []
        for index, match in enumerate(markers):
            start = match.start()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(record.get("content", ""))
            segment = record.get("content", "")[start:end].strip()
            if len(segment) < 500:
                continue
            title_line = match.group("marker").strip(" ：")
            item = dict(record)
            category, record_type = infer_category(title_line, segment, "seed")
            item["title"] = title_line
            item["type"] = "emergency_plan" if record_type == "emergency_plan" else record_type
            item["category"] = category if category in {"chemical_leakage", "poisoning_asphyxia", "confined_space", "other_accident"} else "other_accident"
            item["risk_category"] = infer_risks(segment)
            item["risk_type"] = item["risk_category"]
            item["plan_type"] = infer_plan_type(segment)
            item["content"] = segment
            item["keywords"] = infer_keywords(segment)
            item["quality_score"] = quality_score(segment, item.get("url", ""), "emergency_plan")
            item["content_bytes"] = len(segment.encode("utf-8"))
            segments.append(item)
        if len(segments) >= 2:
            expanded.extend(segments)
            continue
        # 增量运行时，历史 records 可能已经是拆分后的单条样本；即使没有再次发现
        # 两个标题，也要按当前标题规则刷新分类，避免旧版本误分类持续累积。
        refreshed = dict(record)
        category, record_type = infer_category(refreshed.get("title", ""), refreshed.get("content", ""), "seed")
        if record_type == "emergency_plan":
            refreshed["type"] = record_type
            refreshed["category"] = category
            refreshed["plan_type"] = infer_plan_type(refreshed.get("content", ""))
            refreshed["risk_category"] = infer_risks(refreshed.get("content", ""))
            refreshed["risk_type"] = refreshed["risk_category"]
        expanded.append(refreshed)
    return expanded


def extract_common_headings(records: list[dict]) -> list[dict]:
    canonical = [
        ("总则", "总则"), ("事故风险分析", "事故风险分析"), ("危险因素", "事故风险分析"),
        ("应急组织机构", "应急组织机构"), ("组织机构", "应急组织机构"), ("预警", "预警与信息报告"),
        ("信息报告", "预警与信息报告"), ("应急响应", "应急响应"), ("处置措施", "应急处置"),
        ("应急处置", "应急处置"), ("现场处置", "应急处置"), ("人员防护", "人员防护与救援"),
        ("医疗救护", "人员防护与救援"), ("应急保障", "应急保障"), ("培训和演练", "培训与演练"),
        ("应急演练", "培训与演练"), ("附则", "附则"),
    ]
    counter: Counter[str] = Counter()
    for record in records:
        observed: set[str] = set()
        for line in record["content"].splitlines():
            line = line[:100]
            for keyword, normalized in canonical:
                if keyword in line:
                    observed.add(normalized)
        counter.update(observed)
    return [{"section": name, "sample_count": counter[name], "coverage": round(counter[name] / max(len(records), 1), 3)} for name in sorted(counter, key=counter.get, reverse=True)]


def build_template(records: list[dict], category: str) -> dict:
    selected = [r for r in records if r["category"] == category]
    if len(selected) < 5:
        selected = [r for r in records if r["type"] == "emergency_plan"]
    headings = extract_common_headings(selected)
    if category == "chemical_leakage":
        sections = [
            {"section": "总则", "content": "明确编制目的、适用范围、工作原则和编制依据。", "variables": ["company_name", "plan_version"], "required": ["company_name", "applicable_scope"], "optional": ["plan_version"]},
            {"section": "事故风险分析", "content": "企业{{company_name}}涉及{{chemical_name}}，储存/使用量为{{storage_amount}}，存在{{risk}}风险。", "variables": ["company_name", "chemical_name", "storage_amount", "risk", "process_area"], "required": ["chemical_name", "risk", "process_area"], "optional": ["storage_amount"]},
            {"section": "应急组织机构", "content": "配置总指挥、抢险堵漏、警戒疏散、医疗救护、环境监测和后勤保障职责。", "variables": ["commander", "response_teams"], "required": ["commander", "response_teams"], "optional": []},
            {"section": "预警与信息报告", "content": "根据{{leakage_trigger}}启动预警，记录发现时间、物料、位置、风向和影响范围。", "variables": ["leakage_trigger", "report_contacts", "wind_direction"], "required": ["leakage_trigger", "report_contacts"], "optional": ["wind_direction"]},
            {"section": "应急处置", "content": "在确认物料和监测结果后，执行隔离警戒、切断来源、控制扩散、回收/洗消、环境监测和安全撤离；具体措施必须引用对应 SDS。", "variables": ["isolation_distance", "shutdown_steps", "containment_method", "sds_source"], "required": ["shutdown_steps", "containment_method", "sds_source"], "optional": ["isolation_distance"]},
            {"section": "人员防护与救援", "content": "根据化学品危害特性配置{{ppe}}，未经检测和呼吸防护确认不得进入污染区。", "variables": ["ppe", "rescue_equipment", "medical_facility"], "required": ["ppe", "rescue_equipment"], "optional": ["medical_facility"]},
            {"section": "应急保障", "content": "列明监测仪器、堵漏器材、吸附/收集材料、洗消物资、通信和外部救援资源。", "variables": ["emergency_materials", "external_rescue"], "required": ["emergency_materials"], "optional": ["external_rescue"]},
            {"section": "培训与演练", "content": "围绕泄漏报警、上风向撤离、堵漏隔离、个人防护和环境保护开展演练。", "variables": ["training_frequency", "drill_frequency"], "required": ["drill_frequency"], "optional": ["training_frequency"]},
            {"section": "附则", "content": "记录修订、解释、备案和生效信息。", "variables": ["effective_date", "revision_record"], "required": ["effective_date"], "optional": ["revision_record"]},
        ]
    else:
        sections = [
            {"section": "总则", "content": "明确中毒和窒息专项预案的目的、范围、原则和依据。", "variables": ["company_name", "applicable_scope"], "required": ["company_name", "applicable_scope"], "optional": []},
            {"section": "事故风险分析", "content": "识别{{confined_space_name}}内可能存在的{{toxic_gases}}、缺氧、火灾爆炸和其他风险。", "variables": ["confined_space_name", "toxic_gases", "oxygen_threshold", "risk_sources"], "required": ["confined_space_name", "toxic_gases", "risk_sources"], "optional": ["oxygen_threshold"]},
            {"section": "应急组织机构", "content": "明确现场指挥、监护、检测、救援、医疗和警戒疏散职责，监护人员不得擅自进入有限空间。", "variables": ["commander", "watcher", "rescue_team"], "required": ["watcher", "rescue_team"], "optional": ["commander"]},
            {"section": "预警与信息报告", "content": "发现人员异常、报警器报警或氧/有毒气体指标异常时立即停止作业、撤离、报警并报告。", "variables": ["alarm_thresholds", "report_contacts"], "required": ["alarm_thresholds", "report_contacts"], "optional": []},
            {"section": "应急处置", "content": "坚持先通风、再检测、后作业；事故救援前必须检测、通风、做好呼吸和绳索防护，严禁无防护盲目施救。", "variables": ["ventilation_method", "gas_detection_items", "rescue_sequence"], "required": ["ventilation_method", "gas_detection_items", "rescue_sequence"], "optional": []},
            {"section": "人员防护与救援", "content": "配置气体检测报警仪、机械通风、隔绝式呼吸防护、全身式安全带、安全绳和通讯设备。", "variables": ["respiratory_protection", "rescue_equipment", "medical_facility"], "required": ["respiratory_protection", "rescue_equipment"], "optional": ["medical_facility"]},
            {"section": "应急保障", "content": "列明检测、通风、救援、医疗、通信和外部专业救援资源。", "variables": ["emergency_materials", "external_rescue"], "required": ["emergency_materials"], "optional": ["external_rescue"]},
            {"section": "培训与演练", "content": "对审批人、监护人、作业人员和救援人员开展有限空间中毒窒息专项培训和演练。", "variables": ["training_frequency", "drill_frequency"], "required": ["drill_frequency"], "optional": ["training_frequency"]},
            {"section": "附则", "content": "记录修订、解释、备案和生效信息。", "variables": ["effective_date", "revision_record"], "required": ["effective_date"], "optional": ["revision_record"]},
        ]
    return {
        "template_name": "leakage_template" if category == "chemical_leakage" else "poisoning_template",
        "source": "由公开预案样本共同章节统计生成，不复制单一来源正文",
        "sample_count": len(selected),
        "observed_common_headings": headings,
        "sections": sections,
        "required_fields": sorted({field for section in sections for field in section["required"]}),
        "optional_fields": sorted({field for section in sections for field in section["optional"]}),
        "generated_at": now_iso(),
    }


def build_specialized_records(records: list[dict]) -> dict[str, list[dict]]:
    plans = [r for r in records if r["type"] == "emergency_plan"]
    regulations = [
        {"name": r["title"], "type": r["category"], "issuer": r["source"], "scope": r["industry"], "requirements": r["keywords"], "source": r["url"], "content": r["content"]}
        for r in records if r["category"] == "regulation"
    ]
    knowledge: list[dict] = []
    sds: list[dict] = []
    for record in records:
        if record["category"] == "SDS":
            # 优先匹配长名称，避免“氨水”被“氨”提前截获；词表覆盖扩展后的常见化学品集合。
            chemical_haystack = f"{record['title']}\n{record.get('file', '')}\n{record['content'][:2000]}"
            chemical = next(
                (
                    word
                    for word in sorted(COMMON_CHEMICALS, key=len, reverse=True)
                    if word in chemical_haystack
                ),
                "",
            )
            sds.append({"chemical": chemical, "category": "SDS", "hazard_characteristics": record["risk_category"], "leak_response": [sentence for sentence in re.split(r"[。；]", record["content"]) if "泄漏" in sentence][:8], "ppe": [sentence for sentence in re.split(r"[。；]", record["content"]) if "防护" in sentence or "呼吸" in sentence][:8], "first_aid": [sentence for sentence in re.split(r"[。；]", record["content"]) if "急救" in sentence or "冲洗" in sentence][:8], "forbidden_actions": [sentence for sentence in re.split(r"[。；]", record["content"]) if "禁止" in sentence or "严禁" in sentence][:8], "source": record["url"]})
        elif record["category"] in {"knowledge", "chemical_catalog", "chemical_leakage", "poisoning_asphyxia"}:
            knowledge.append({"title": record["title"], "source": record["url"], "risk_category": record["risk_category"], "content": record["content"], "keywords": record["keywords"]})
    return {"plan_samples": plans, "regulations": regulations, "knowledge": knowledge, "sds": sds}


def try_fetch_candidate(candidate: dict[str, str]) -> tuple[dict[str, str], FetchResult | None, str]:
    """并发下载阶段的单项任务；解析仍在主线程执行，避免 Office 转换相互干扰。"""
    try:
        return candidate, fetch(candidate["url"]), ""
    except Exception as exc:  # 单个站点失败只记录，不阻塞其它来源
        return candidate, None, str(exc)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    for directory in (RAW_DIR, ATTACHMENT_DIR, TEXT_DIR, JSON_DIR, TEMPLATE_DIR, REPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    # 本地附件始终参与解析，即使使用 EMERGENCY_OFFLINE=1 也不会丢失目录知识。
    chemical_catalog, chemical_catalog_report = collect_local_chemical_catalog()
    chemical_catalog_records = [build_chemical_catalog_record(entry) for entry in chemical_catalog]

    # 离线模式只重用本地原始资料和历史 records，便于断网时重新分类、去重和生成报告。
    offline_mode = os.getenv("EMERGENCY_OFFLINE", "0") in {"1", "true", "True"}
    seeds_only_mode = os.getenv("EMERGENCY_SEEDS_ONLY", "0") in {"1", "true", "True"}
    search_records: list[dict[str, str]] = []
    search_tasks = [
        (query_type, query)
        for query_type, queries in SEARCH_QUERIES.items()
        for query in queries
    ]
    if offline_mode or seeds_only_mode:
        search_tasks = []

    def run_search_task(task: tuple[str, str]) -> list[dict[str, str]]:
        query_type, query = task
        try:
            return [
                {**item, "query_type": query_type}
                for item in search_web(query, allow_fallback=query_type != "sds")
            ]
        except Exception as exc:  # 单个搜索引擎请求失败不能中断其它查询
            return [{"query": query, "query_type": query_type, "error": str(exc)}]

    # 搜索请求并发执行，避免某一个响应慢的搜索服务阻塞整个关键词队列；
    # 后续文档下载仍然会进行 URL、来源和内容哈希校验。
    with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as executor:
        for result in executor.map(run_search_task, search_tasks):
            search_records.extend(result)

    for url in SEED_PAGES:
        search_records.append({"title": Path(urlparse(url).path).stem or url, "url": url, "query_type": "seed"})
    # 按 URL 去重，避免搜索结果中的同一附件重复下载。
    unique_candidates: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in search_records:
        url = item.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique_candidates.append(item)
    if offline_mode:
        unique_candidates = []
    # 先处理官方直链附件和预案类结果，优先保证样本数量，再处理法规/SDS的补充资料。
    unique_candidates.sort(
        key=lambda item: (
            0 if item.get("query_type") in {"chemical_leakage", "poisoning_asphyxia", "confined_space", "other_accident", "seed"} else 1,
            0 if Path(urlparse(item.get("url", "")).path).suffix.lower() in ALLOWED_DOC_EXTENSIONS else 1,
            0 if is_public_host(host_of(item.get("url", ""))) else 1,
        )
    )
    if MAX_CANDIDATES > 0:
        unique_candidates = unique_candidates[:MAX_CANDIDATES]
    write_json(REPORT_DIR / "search_results.json", unique_candidates)

    records: list[dict] = []
    seen_hashes: set[str] = set()
    # 首批候选并发下载；站点级失败仍记录在搜索报告中。
    with ThreadPoolExecutor(max_workers=8) as executor:
        fetched = list(executor.map(try_fetch_candidate, unique_candidates))
    followup_candidates: list[dict[str, str]] = []
    for candidate, response, error in fetched:
        if response is None:
            candidate["download_error"] = error
            cached_path = find_cached_raw(candidate)
            if cached_path is not None:
                try:
                    cached_raw_html = cached_path.read_text(encoding="utf-8", errors="ignore") if cached_path.suffix.lower() == ".html" else None
                    cached_digest = hashlib.sha256(cached_path.read_bytes()).hexdigest()
                    if cached_digest not in seen_hashes:
                        cached_record = build_record(
                            cached_path,
                            candidate["url"],
                            candidate.get("title", cached_path.stem),
                            candidate.get("query_type", ""),
                            raw_html=cached_raw_html,
                        )
                        if cached_record["content_bytes"] >= 1200:
                            seen_hashes.add(cached_digest)
                            records.append(cached_record)
                except Exception as exc:
                    candidate["cache_parse_error"] = str(exc)
            continue
        try:
            extension = extension_for(response.url, response)
            title_hint = candidate.get("title", "") or Path(urlparse(response.url).path).stem
            if extension == ".html":
                raw_html = response.text
                text = parse_file(Path("页面.html"), raw_html=raw_html)
                title = find_title(text, title_hint)
                category, record_type = infer_category(title, text, candidate.get("query_type", ""))
                # 普通网页只有具备足够正文并疑似资料页时才纳入，减少导航页噪音。
                if len(text) < 1200 or (record_type == "knowledge" and len(text) < 5000):
                    followup_candidates.extend(
                        {"title": title_hint, "url": attachment, "query_type": candidate.get("query_type", "")}
                        for attachment in discover_attachment_urls(response.url, raw_html)
                    )
                    continue
                digest = hashlib.sha256(response.content).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                page_path = RAW_DIR / f"{safe_name(title)}-{digest[:12]}.html"
                page_path.write_bytes(response.content)
                records.append(build_record(page_path, response.url, title_hint, candidate.get("query_type", ""), raw_html=raw_html))
                continue
            if len(response.content) < 10_000:
                continue
            digest = hashlib.sha256(response.content).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            path, _ = save_binary(response.url, response, title_hint)
            record = build_record(path, response.url, title_hint, candidate.get("query_type", ""))
            if record["content_bytes"] >= 1200:
                records.append(record)
        except Exception as exc:
            candidate["download_error"] = str(exc)

    # 对低正文页面发现的一层附件采用同一抓取器路由，数量通常较少，串行便于记录来源。
    for candidate in followup_candidates:
        candidate, response, error = try_fetch_candidate(candidate)
        if response is None:
            candidate["download_error"] = error
            continue
        try:
            if len(response.content) < 10_000:
                continue
            digest = hashlib.sha256(response.content).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            title_hint = candidate.get("title", "") or Path(urlparse(response.url).path).stem
            path, _ = save_binary(response.url, response, title_hint)
            record = build_record(path, response.url, title_hint, candidate.get("query_type", ""))
            if record["content_bytes"] >= 1200:
                records.append(record)
        except Exception as exc:
            candidate["download_error"] = str(exc)

    # 将上一阶段已核验的官方资料纳入同一结构，source 仍使用原始官方 URL。
    for manifest_path in LOCAL_MANIFESTS:
        if not manifest_path.exists():
            continue
        for item in json.loads(manifest_path.read_text(encoding="utf-8")):
            local_path = Path(item.get("local_path", ""))
            source_url = item.get("source_url", "")
            if not local_path.exists() or not source_url or local_path.suffix.lower() not in ALLOWED_DOC_EXTENSIONS | {".txt"}:
                continue
            try:
                digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
                seen_hashes.add(digest)
                record = build_record(local_path, source_url, item.get("title", local_path.stem), "regulation")
                records.append(record)
            except Exception:
                continue

    # 将用户提供的目录条目纳入统一 records，同时另存为目录专用 JSON 便于按 CAS/品名检索。
    records.extend(chemical_catalog_records)

    # PubChem 作为 SDS 专用补充抓取器，默认开启；关闭时设置 EMERGENCY_PUBCHEM_SDS=0。
    if not offline_mode and not seeds_only_mode and os.getenv("EMERGENCY_PUBCHEM_SDS", "1") not in {"0", "false", "False"}:
        records.extend(collect_pubchem_sds())

    records = split_plan_records(records)
    records = split_accident_records(records)

    # 累积历史输出，保证搜索服务偶发限流或结果波动时不会丢失上一轮已确认资料；
    # 最终仍按正文内容哈希去重，新的解析结果优先保留。
    previous_records_path = JSON_DIR / "records.json"
    if previous_records_path.exists():
        try:
            previous_records = json.loads(previous_records_path.read_text(encoding="utf-8"))
            if isinstance(previous_records, list):
                records.extend(item for item in previous_records if isinstance(item, dict))
        except Exception:
            pass

    # 历史输出是在旧分类规则下生成的；合并后统一刷新，保证重复运行不会保留旧误分类。
    for record in records:
        if record.get("type") != "emergency_plan":
            continue
        category, record_type = infer_category(record.get("title", ""), record.get("content", ""), "seed")
        record["category"] = category
        record["type"] = record_type
        if record_type == "emergency_plan":
            record["plan_type"] = infer_plan_type(record.get("content", ""))
        record["risk_category"] = infer_risks(record.get("content", ""))
        record["risk_type"] = record["risk_category"]

    # 以内容哈希做最终去重；同一正文被不同检索词命中时，优先保留更具体且已确认的类别。
    deduped: dict[str, dict] = {}
    category_priority = {
        "regulation": 50,
        "SDS": 50,
        "accident_case": 45,
        "chemical_leakage": 40,
        "poisoning_asphyxia": 40,
        "confined_space": 40,
        "chemical_catalog": 35,
        "other_accident": 20,
        "knowledge": 10,
    }
    for record in records:
        key = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
        current = deduped.get(key)
        if current is None:
            deduped[key] = record
            continue
        current_priority = category_priority.get(current.get("category", ""), 0)
        record_priority = category_priority.get(record.get("category", ""), 0)
        if record_priority > current_priority or (
            record_priority == current_priority and int(record.get("quality_score", 0)) > int(current.get("quality_score", 0))
        ):
            deduped[key] = record
    records = sorted(deduped.values(), key=lambda item: (-int(item["quality_score"]), item["title"]))
    specialized = build_specialized_records(records)
    plan_samples = specialized["plan_samples"]
    category_counts = Counter(r["category"] for r in records)
    unique_sds_chemicals = len({item["chemical"] for item in specialized["sds"] if item["chemical"]})
    measured_counts = dict(category_counts)
    measured_counts["SDS"] = unique_sds_chemicals
    target_gaps = {name: max(0, minimum - measured_counts.get(name, 0)) for name, minimum in MIN_TARGETS.items()}
    if any(target_gaps.values()):
        (REPORT_DIR / "采集警告.txt").write_text(
            "以下最低目标尚未达到（目标是最低量，不是封顶量）：\n"
            + "\n".join(f"{name}: 还缺 {gap} 条" for name, gap in target_gaps.items() if gap)
            + "\n请补充公开 URL 或增加搜索词后重跑。\n",
            encoding="utf-8",
        )
    else:
        # 目标达成后清除上一轮遗留的缺口提示，避免报告与警告文件不一致。
        (REPORT_DIR / "采集警告.txt").write_text(
            "当前最低采集目标已全部达到；目标数量是最低量，不是封顶量。\n",
            encoding="utf-8",
        )

    write_json(JSON_DIR / "records.json", records)
    write_json(JSON_DIR / "plan_samples.json", plan_samples)
    write_json(JSON_DIR / "regulations.json", specialized["regulations"])
    write_json(JSON_DIR / "knowledge.json", specialized["knowledge"])
    write_json(JSON_DIR / "sds.json", specialized["sds"])
    write_json(JSON_DIR / "chemical_catalog.json", chemical_catalog)
    write_json(JSON_DIR / "accident_cases.json", [r for r in records if r["category"] == "accident_case"])
    write_json(TEMPLATE_DIR / "leakage_template.json", build_template(plan_samples, "chemical_leakage"))
    write_json(TEMPLATE_DIR / "poisoning_template.json", build_template(plan_samples, "poisoning_asphyxia"))

    summary = {
        "generated_at": now_iso(),
        "record_count": len(records),
        "plan_sample_count": len(plan_samples),
        "category_counts": dict(category_counts),
        "chemical_catalog_count": len(chemical_catalog),
        "unique_sds_chemicals": unique_sds_chemicals,
        "industry_counts": dict(Counter(r["industry"] for r in records)),
        "source_count": len({r["url"] for r in records}),
        "minimum_targets": MIN_TARGETS,
        "target_gaps": target_gaps,
        "all_minimum_targets_met": not any(target_gaps.values()),
        "output_root": str(ROOT),
    }
    write_json(REPORT_DIR / "collection_report.json", summary)
    write_json(REPORT_DIR / "chemical_catalog_report.json", chemical_catalog_report)
    (REPORT_DIR / "README.md").write_text(
        "# 数据采集流水线结果\n\n"
        f"本次解析记录：{len(records)}；预案样本：{len(plan_samples)}。目标数量是最低量，不是封顶量。\n\n"
        "所有记录的 `url` 字段保留来源 URL；入库前应人工复核质量分和历史法规有效性。\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
