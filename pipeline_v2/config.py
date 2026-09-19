from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw"
SOURCE_RECORDS = ROOT / "json" / "records.json"
CLEANED = ROOT / "cleaned_v2"
REPORTS = ROOT / "reports_v2"
PILOT_SEED = 20260824
PILOT_SIZE = 100
ALLOWED_TYPES = {"enterprise_special_plan", "organization_special_plan", "government_or_regional_plan", "onsite_disposal_plan", "embedded_special_section", "sds", "chemical_catalog", "guidance_reference", "regulation", "accident_case", "reject", "manual_review"}
