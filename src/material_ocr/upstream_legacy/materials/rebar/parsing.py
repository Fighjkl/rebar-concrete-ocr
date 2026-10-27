"""纯规则层：不依赖OCR/GPU，保留原文，不把O/I猜成0/1。"""
import re
import unicodedata
from decimal import Decimal


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def parse_weight(text):
    value = compact(text)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(t|T|吨|kg|KG|千克)", value)
    if not match:
        return None, "weight_unreadable_or_unit_missing"
    amount = Decimal(match[1])
    if amount <= 0:
        return None, "weight_not_positive"
    if match[2] in ("kg", "KG", "千克"):
        amount /= Decimal(1000)
    return format(amount, "f"), None


def parse_manufacturer_certificate(text):
    # 此样例同格先写厂家、再写编号；只支持已观察到的这一种排法。
    value = compact(text)
    match = re.fullmatch(r"(.+?公司)([A-Za-z0-9-]+)", value)
    if not match:
        return None, None, "manufacturer_certificate_not_separable"
    company, certificate = match.groups()
    # 仅做宽松格式筛查，不保证编号正确，不把样例编号写死。
    if not re.fullmatch(r"[A-Za-z]*\d[A-Za-z0-9-]{5,29}", certificate):
        return company, None, "certificate_format_unexpected"
    return company, certificate, None


def parse_row(raw, expected_row=None):
    issues = []
    number = compact(raw.get("row_no", ""))
    row_no = int(number) if re.fullmatch(r'[0-9]{1,6}',number) and int(number)>0 else None
    if row_no is None or row_no <= 0:
        issues.append("row_number_missing_or_invalid")
    if expected_row is not None and row_no != expected_row:
        issues.append("row_number_mismatch")
    weight, issue = parse_weight(raw.get("weight", ""))
    if issue:
        issues.append(issue)
    manufacturer, certificate, issue = parse_manufacturer_certificate(
        raw.get("manufacturer_certificate", ""))
    if issue:
        issues.append(issue)
    spec = compact(raw.get("specification", ""))
    match = re.fullmatch(r"(?:Φ|φ|∅)?(\d+(?:\.\d+)?)(?:mm|MM|毫米)", spec)
    diameter = match[1] if match else None
    if not match:
        issues.append("specification_unparsed")
    return {
        "row_no": row_no, "diameter_mm": diameter,
        "material_name": raw.get("material_name", ""),
        "weight_t": weight, "manufacturer": manufacturer,
        "certificate_no": certificate, "issues": issues,
    }
