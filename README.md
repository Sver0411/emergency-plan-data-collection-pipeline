# 安全与危化品预案资料采集流水线

这里保存资料采集和离线清洗程序，不保存企业预案、扫描件、SDS 原件、爬取结果或运行日志。流水线与业务应用分开运行：它生成待审核的知识候选，不能把输出直接视为经过专业审核的知识库。

## 组成

| 程序 | 职责 |
| --- | --- |
| `crawler.py` | 检索公开来源，抓取 PDF/Word/网页，解析并按主题生成初始 JSON；使用网络。 |
| `quality_control.py`、`precision_review.py` | 对已有记录做去重、质量筛选和人工复核辅助；不应改写原始文件。 |
| `pipeline_v2/parsers.py`、`normalization.py`、`file_registry.py` | 文件登记、文本解析与标准化。 |
| `pipeline_v2/classification*.py`、`section_splitter*.py` | 区分预案、SDS、法规、事故案例等资料，并切分章节。 |
| `pipeline_v2/deduplication.py`、`redaction.py`、`validators.py` | 去重、敏感信息处理及结构校验。 |
| `pipeline_v2/full_runner_v1.py` | 对指定 `raw` 目录执行完整离线处理，写出分层记录、待审核队列与审计结果。 |
| `pipeline_v2/repair_full_v1.py`、`semantic_fix_v1.py`、`title_exclusion_fix_v1.py` | 对已有处理结果作语义、标题和分类修复；按各入口的参数运行。 |
| `pipeline_v2/pilot_runner*.py` | 100 份样本试运行与不同规则版本回归；不是生产知识库导入命令。 |
| `tests_v2*/` | 解析、分类、去重、脱敏、标题修复等回归用例。 |

## 使用边界

1. 在独立 Python 环境安装 `pipeline_v2/requirements-lock.txt`。完整解析和 OCR 还依赖 Poppler、Tesseract/Docling 等系统工具；具体入口缺少工具时会报错。
2. 把有权处理的输入文件放在本目录下的 `raw/`，或为全量处理命令指定其他输入目录。不要把 `raw/`、生成的 `cleaned*/`、`reports*/`、`checkpoints*/`、`logs*/` 提交到 Git。
3. 在本目录下运行离线全量处理，例如：

   ```bash
   python -m pipeline_v2.full_runner_v1 --root . --raw ./raw
   ```

   断点续跑时在确认原始文件未变化后加 `--resume`。先查看命令的 `--help`，再运行修复脚本。`crawler.py` 会访问公网，应由负责人确认采集来源、版权与访问规则后单独执行；本仓库没有随附原始资料。
4. 输出记录保留来源、分类、章节和审核状态。进入业务系统前需要核对来源、时效、脱敏结果及适用性，由知识导入流程审核后才可供生成参考；本流水线不自动批准、不自动写业务数据库。

`crawler.py` 可通过 `EMERGENCY_CHEMICAL_CATALOG` 指定本地化学品目录文件。不设置时仅查找 `raw/attachments/危险化学品目录指南(2)(1).xlsx`，缺失则不导入该目录。可选本地法规清单约定放在 `local_manifests/资料清单.json`，此目录同样不入库。

## 注意

- `pipeline_v2` 保留了历次规则迭代的脚本，版本后缀表示历史规则，不表示每个脚本都应依次运行。新增处理优先使用可复现的全量入口与当前规则，避免误用旧试运行产物。
- 克隆仓库只得到程序，不得到任何原始数据或已有知识库。重建知识候选必须重新提供合规输入，且审核流程不能省略。
- 本目录是独立数据工程工具，不是网页上传企业资料时同步调用的在线服务。企业附件识别与预案生成由 `app/` 中的业务服务负责。
