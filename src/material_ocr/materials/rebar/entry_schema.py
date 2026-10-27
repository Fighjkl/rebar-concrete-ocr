"""Shared entry-form vocabulary. Geometry and raw OCR remain separate."""
import re
import unicodedata


ENTRY_TITLES = {
    "inspection_record": ("进场检验记录",),
    "entry_quantity_list": ("进场数量清单",),
}
FIELD_ALIASES = {
    "row_no": ("序号",),
    "material_name": ("材料名称", "材料品名", "名称"),
    "specification": ("规格型号", "规格"),
    "quantity": ("进场数量", "进场量"),
    "manufacturer": ("生产厂家", "生产单位"),
    "certificate_no": ("质量证明书编号", "质量证明书号", "质证书编号", "质证书号", "合格证号"),
}
OPTIONAL_ALIASES = {
    "entry_date": ("进场日期",),
    "use_location": ("使用部位",),
    "self_check_result": ("自检结果",),
}


def compact(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))


def quantity_header(text):
    """Return (recognized label, unit); never infer a unit from body values."""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    match = re.fullmatch(r"(进场数量|进场量)(?:[（(](t|kg|吨|千克)[)）])?", text, re.I)
    if not match:
        return None, None
    unit = match[2]
    return match[1], ("t" if unit and unit.lower() in ("t", "吨") else "kg" if unit else None)


def header_matches(name, text, aliases):
    if name == "quantity":
        return quantity_header(text)[0] is not None
    return compact(text) in aliases


def parse_specification(text):
    """Parse rebar notation; bare digits without a diameter symbol/unit fail."""
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    found = re.fullmatch(
        r"(?:(HRB\d{3}E?|HRBF\d{3}E?|HPB\d{3}|RRB\d{3})[/\-]?)?"
        r"([ΦφØø⌀])?(\d+(?:\.\d+)?)(mm|毫米)?", value, re.I)
    if not found or not (found[2] or found[4]):
        return None
    return {"grade": found[1].upper() if found[1] else None, "diameter_mm": found[3]}
