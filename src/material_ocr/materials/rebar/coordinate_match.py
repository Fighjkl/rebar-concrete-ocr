"""Offline coordinate matching, geometric grid and rebar field assembly.

Only consumes existing PaddleOCR JSON. Does NOT call Paddle, OCR or VLM.
The supported business profile requires explicit headers and a serial column.
"""
import argparse
import hashlib
import html
import json
import math
import re
import shutil
import statistics
import time
from decimal import Decimal, InvalidOperation
from datetime import date
from pathlib import Path
from material_ocr.materials.rebar.entry_schema import (
    FIELD_ALIASES, OPTIONAL_ALIASES, compact, header_matches, parse_specification, quantity_header,
)
from material_ocr.materials.rebar.confidence import load_policy, field_confidence_decision

ALIASES = FIELD_ALIASES


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(text):
    return re.sub(r"[\s:：]", "", text)


def rectangle(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Expected pixel bbox [x1,y1,x2,y2].")
    box = [float(v) for v in value]
    if not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"Invalid rectangle: {value}")
    return box


def text_coverage(cell, text):
    width = max(0, min(cell[2], text[2])-max(cell[0], text[0]))
    height = max(0, min(cell[3], text[3])-max(cell[1], text[1]))
    return width*height / ((text[2]-text[0])*(text[3]-text[1]))


def load_tokens(data):
    # Use ONE source, avoiding duplicates from overall OCR and table OCR.
    ocr = data.get("overall_ocr_res", {})
    source = "overall_ocr_res"
    if not ocr.get("rec_texts") and len(data.get("table_res_list", [])) == 1:
        ocr = data["table_res_list"][0].get("table_ocr_pred", {})
        source = "table_res_list[0].table_ocr_pred"
    texts, boxes, scores = (ocr.get(key, []) for key in ("rec_texts", "rec_boxes", "rec_scores"))
    if not (len(texts) == len(boxes) == len(scores)):
        raise ValueError("OCR texts/boxes/scores lengths differ; refusing partial alignment.")
    tokens = []
    for index, (text, box, score) in enumerate(zip(texts, boxes, scores)):
        # GLM cell OCR has no calibrated word confidence.  Preserve an
        # unknown score instead of coercing it to a fabricated value; the
        # geometry and text still go through the same row/column matcher.
        if score is not None:
            score = float(score)
        if (not isinstance(text, str) or
                (score is not None and (not math.isfinite(score) or not 0 <= score <= 1))):
            raise ValueError("Invalid OCR text or score.")
        tokens.append({"ocr_index": index, "source": source, "text": text,
                       "bbox": rectangle(box), "ocr_score": score})
    return tokens


def cluster_edges(values, tolerance):
    groups = []
    for value in sorted(values):
        # Median-based grouping avoids a long chain merging separate boundaries.
        if groups and abs(value-statistics.median(groups[-1])) <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [statistics.median(group) for group in groups]


def recover_grid(cells):
    issues = []
    if not cells:
        return {"x_edges": [], "y_edges": [], "issues": ["no_cells"]}
    heights = sorted(c["bbox"][3]-c["bbox"][1] for c in cells)
    widths = sorted(c["bbox"][2]-c["bbox"][0] for c in cells)
    # Relative tolerances, not a hardcoded six-row page template.
    # The lower quartile approximates the smallest ordinary cells.
    h = heights[len(heights)//4]
    w = widths[len(widths)//4]
    xtol, ytol = w*.10, h*.20
    xs = cluster_edges([c["bbox"][i] for c in cells for i in (0, 2)], xtol)
    ys = cluster_edges([c["bbox"][i] for c in cells for i in (1, 3)], ytol)
    occupied = {}
    for cell in cells:
        box = cell["bbox"]
        c0, c1 = [min(range(len(xs)), key=lambda j: abs(xs[j]-box[i])) for i in (0, 2)]
        r0, r1 = [min(range(len(ys)), key=lambda j: abs(ys[j]-box[i])) for i in (1, 3)]
        cell["grid"] = {"row_start": r0, "row_end": r1, "col_start": c0, "col_end": c1,
                        "rowspan": r1-r0, "colspan": c1-c0}
        if r1 <= r0 or c1 <= c0:
            cell["issues"].append("collapsed_grid_cell")
            issues.append(f"collapsed_grid_cell:{cell['cell_id']}")
            continue
        for row in range(r0, r1):
            for col in range(c0, c1):
                if (row, col) in occupied:
                    issues.append(f"overlapping_grid_cells:{occupied[(row,col)]}:{cell['cell_id']}")
                occupied[(row, col)] = cell["cell_id"]
    return {"x_edges": xs, "y_edges": ys, "x_tolerance": xtol, "y_tolerance": ytol,
            "issues": sorted(set(issues)),
            "note": "Primitive grid columns include metadata subdivisions; not the business-field count."}


def assign_texts(cells, tokens, min_coverage=.80, min_margin=.15, min_score=.95):
    matches = []
    for token in tokens:
        if not token["text"].strip():
            continue
        candidates = sorted(((text_coverage(c["bbox"], token["bbox"]), c["cell_id"])
                             for c in cells), reverse=True)
        best = candidates[0] if candidates else (0, None)
        second = candidates[1][0] if len(candidates) > 1 else 0
        status = "assigned"
        if best[0] < min_coverage:
            status = "unassigned"
        elif best[0]-second < min_margin:
            status = "ambiguous"
        match = {**token, "match_status": status, "assigned_cell_id": best[1] if status == "assigned" else None,
                 "best_text_coverage": round(best[0], 6), "second_text_coverage": round(second, 6),
                 "candidates": [{"cell_id": cid, "text_coverage": round(score, 6)}
                                for score, cid in candidates if score > 0]}
        matches.append(match)
        if status == "assigned":
            cell = next(c for c in cells if c["cell_id"] == best[1])
            cell["tokens"].append(match)
            if token["ocr_score"] is not None and token["ocr_score"] < min_score:
                cell["issues"].append(f"low_ocr_score:{token['ocr_index']}")
    for cell in cells:
        # Cluster OCR lines by centers, then sort each line by x. Preserve both
        # multiline evidence and normalized concatenation for exact identifiers.
        lines = []
        for token in sorted(cell["tokens"], key=lambda t: ((t["bbox"][1]+t["bbox"][3])/2, t["bbox"][0])):
            cy = (token["bbox"][1]+token["bbox"][3])/2
            height = token["bbox"][3]-token["bbox"][1]
            if lines and abs(cy-statistics.median(t["cy"] for t in lines[-1])) <= height*.40:
                lines[-1].append({"token": token, "cy": cy})
            else:
                lines.append([{"token": token, "cy": cy}])
        cell["lines"] = [" ".join(t["token"]["text"] for t in sorted(line, key=lambda t: t["token"]["bbox"][0]))
                         for line in lines]
        cell["raw_text"] = "\n".join(cell["lines"])
        cell["normalized_text"] = normalize(cell["raw_text"])
    return matches


def field_value(name, cell, header=None):
    raw = cell["raw_text"] if cell else ""
    text = normalize(raw)
    evidence = {"cell_id": cell["cell_id"], "cell_bbox": cell["bbox"], "grid": cell["grid"],
                "ocr_tokens": [{k: t[k] for k in ("ocr_index", "text", "bbox", "ocr_score", "best_text_coverage")}
                               for t in cell["tokens"]]} if cell else None
    issues = list(cell["issues"]) if cell else []
    value = text or None
    extra = {}
    if not text and name in OPTIONAL_ALIASES:
        pass
    elif not text:
        issues.append("missing_text")
    elif name == "row_no":
        if re.fullmatch(r"[1-9]\d{0,4}", text):
            value = int(text)
        else:
            value = None
            issues.append("invalid_row_no")
    elif name == "quantity":
        found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(t|T|吨|kg|KG|千克)?", text)
        header_unit = quantity_header(header["raw_text"])[1] if header else None
        explicit_unit = ("t" if found[2] in ("t", "T", "吨") else "kg") if found and found[2] else None
        unit = explicit_unit or header_unit
        if found and unit:
            try:
                number = Decimal(found[1])
                if number <= 0:
                    issues.append("nonpositive_quantity")
                value = {"value": str(number), "unit": unit}
                extra["unit_source"] = "cell" if explicit_unit else "column_header"
                if header_unit:
                    extra["unit_header_evidence"] = field_value("header", header)["evidence"]
                if explicit_unit and header_unit and explicit_unit != header_unit:
                    issues.append("quantity_unit_conflicts_with_header")
            except InvalidOperation:
                value = None
                issues.append("invalid_quantity")
        else:
            value = None
            issues.append("invalid_quantity_or_missing_unit")
    elif name == "specification":
        parsed = parse_specification(text)
        if parsed is None:
            issues.append("specification_format_not_validated")
        else:
            extra["parsed"] = parsed
    elif name == "entry_date":
        found = re.fullmatch(r"(20\d{2})(?:年|/|-|\.)(\d{1,2})(?:月|/|-|\.)(\d{1,2})日?", text)
        try:
            value = date(*map(int, found.groups())).isoformat() if found else None
        except ValueError:
            value = None
        if value is None:
            issues.append("invalid_entry_date")
    elif name == "certificate_no":
        # Format checks only. No O->0 conversion, guessing or fixed factory lengths.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]*", text) or not re.search(r"\d", text):
            issues.append("certificate_format_not_validated")
    elif name == "material_name" and "钢筋" not in text:
        issues.append("material_not_validated_as_rebar")
    return {"value": value, "raw_text": raw, "lines": cell["lines"] if cell else [],
            "evidence": evidence, "issues": sorted(set(issues)), **extra}


def split_shared_cell(cell):
    """Conservative business rule, not a language-model guess or OCR correction.

    Only applies to a full product cell under BOTH manufacturer/certificate
    headers. Require a Chinese company suffix followed by one ASCII identifier.
    An identifier split across successive OCR lines is allowed; two identifiers
    separated by whitespace on the same line are NOT silently concatenated.
    Both fields retain the full source cell and exact normalized character span.
    """
    text = cell["normalized_text"]
    candidates = []
    for suffix in re.finditer(r"公司|厂", text):
        cut = suffix.end()
        company, cert = text[:cut], text[cut:]
        if (re.search(r"[\u4e00-\u9fff]", company)
                and re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9（）()·.\-]+", company)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", cert)
                and re.search(r"\d", cert)):
            candidates.append((company, cert, cut))
    issues = []
    if len(candidates) != 1:
        issues.append("shared_cell_split_missing_or_ambiguous")
    # Keep line breaks distinct from spaces when checking multiple values.
    if any(re.search(r"[A-Za-z0-9]\s+[A-Za-z0-9]", line) for line in cell["lines"]):
        issues.append("shared_cell_multiple_ascii_parts_on_same_line")
    # If an identifier continues on later lines, accept digits only. A new
    # letter prefix may start a SECOND identifier, so do not concatenate it.
    if len(candidates) == 1:
        cut = candidates[0][2]
        cursor = 0
        started = False
        for line in cell["lines"]:
            part = normalize(line)
            tail = part[max(0, cut-cursor):] if cursor+len(part) > cut else ""
            if tail:
                if started and not re.fullmatch(r"\d+", tail):
                    issues.append("shared_cell_uncertain_identifier_continuation")
                started = True
            cursor += len(part)
    result = {}
    for name in ("manufacturer", "certificate_no"):
        field = field_value("manufacturer", cell)
        field["value"] = None
        field["issues"] = sorted(set(field["issues"] + issues))
        field["extraction_method"] = "shared_cell_company_suffix_then_identifier"
        if not issues:
            company, cert, cut = candidates[0]
            field["value"] = company if name == "manufacturer" else cert
            field["evidence"]["normalized_character_span"] = [0, cut] if name == "manufacturer" else [cut, len(text)]
        result[name] = field
    return result


def isolated_dot_blank_evidence(related, fields, unresolved):
    """Classify, never erase: only one tiny low-score dot in an otherwise
    OCR-empty, structurally complete row. A digit/letter is never noise here.
    This is a review candidate, not a claim that the source pixels are blank.
    """
    if unresolved or not all(f["evidence"] is not None or f.get("source_status") == "not_in_source"
                             for f in fields.values()):
        return None
    pairs = [(cell, token) for cell in related for token in cell["tokens"]]
    if len(pairs) != 1:
        return None
    cell, token = pairs[0]
    if (token["text"].strip() not in (".", "。", "·", "．") or
            token["ocr_score"] is None or token["ocr_score"] >= .30):
        return None
    b, c = token["bbox"], cell["bbox"]
    wr, hr = (b[2]-b[0])/(c[2]-c[0]), (b[3]-b[1])/(c[3]-c[1])
    if wr > .10 or hr > .15 or wr*hr > .01:
        return None
    return {"cell_id": cell["cell_id"], "cell_bbox": c,
            "ocr_index": token["ocr_index"], "text": token["text"], "bbox": b,
            "ocr_score": token["ocr_score"], "width_ratio": wr, "height_ratio": hr,
            "area_ratio": wr*hr,
            "rule": {"single_dot_only": True, "score_lt": .30,
                     "max_width_ratio": .10, "max_height_ratio": .15, "max_area_ratio": .01}}


def assemble_records(cells, grid, matches, min_score=.95):
    issues = list(grid["issues"])
    headers = {}
    serial_candidates = [c for c in cells if compact(c["raw_text"]) == "序号"]
    # Title must precede the serial header, in this table's horizontal region.
    # A missing certificate header on the OLD form is still an error.
    quantity_list = False
    if len(serial_candidates) == 1 and cells:
        serial_y = serial_candidates[0]["bbox"][1]
        left, right = min(c["bbox"][0] for c in cells), max(c["bbox"][2] for c in cells)
        title_tokens = sorted([m for m in matches if
            (m["ocr_score"] is None or m["ocr_score"] >= .90)
            and (m["bbox"][1]+m["bbox"][3])/2 < serial_y
            and left <= (m["bbox"][0]+m["bbox"][2])/2 <= right],
            key=lambda m: (m["bbox"][1], m["bbox"][0]))
        quantity_list = any("进场数量清单" in "".join(compact(m["text"]) for m in title_tokens[i:i+3])
                            for i in range(len(title_tokens)))
    for name, aliases in ALIASES.items():
        found = [c for c in cells if header_matches(name, c["raw_text"], aliases)]
        if name == "certificate_no" and quantity_list and not found:
            continue
        if len(found) != 1:
            issues.append(f"header_missing_or_ambiguous:{name}:{len(found)}")
        else:
            headers[name] = found[0]
    required = set(ALIASES) - ({"certificate_no"} if quantity_list else set())
    if not required.issubset(headers) or issues:
        return [], [], headers, sorted(set(issues))
    # Header cells must overlap the serial header's vertical band.
    serial = headers["row_no"]["grid"]
    start, end = serial["row_start"], serial["row_end"]
    header_warnings = {}
    header_confidence = {}
    confidence_policy = load_policy() if quantity_list else None
    for name, aliases in OPTIONAL_ALIASES.items():
        found = [c for c in cells if start <= c["grid"]["row_start"] and c["grid"]["row_end"] <= end
                 and header_matches(name, c["raw_text"], aliases)]
        if len(found) == 1:
            headers[name] = found[0]
        elif len(found) > 1:
            issues.append(f"header_missing_or_ambiguous:{name}:{len(found)}")
    for name, cell in headers.items():
        g = cell["grid"]
        if g["row_start"] < start or g["row_end"] > end:
            issues.append(f"header_outside_serial_band:{name}")
        if cell["issues"]:
            decision = (field_confidence_decision(cell, 'quantity_header', 'entry_quantity_list',
                        min_score, confidence_policy) if quantity_list and name == 'quantity' else None)
            if decision:
                header_confidence[name] = decision
            elif quantity_list and all(i.startswith("low_ocr_score:") for i in cell["issues"]):
                # Exact, unique label + valid geometry still permits a DRAFT.
                # Confidence is not upgraded: warn on each affected row/field.
                header_warnings[name] = list(cell["issues"])
            else:
                issues.append(f"unreliable_header:{name}")
    if issues:
        return [], [], headers, sorted(set(issues))

    # Do not confuse an unreadable heading with an absent certificate column.
    unknown_headers = [c["cell_id"] for c in cells
        if start <= c["grid"]["row_start"] and c["grid"]["row_end"] <= end
        and c["cell_id"] not in {h["cell_id"] for h in headers.values()}
        and compact(c["raw_text"]) != "备注"]

    def same_columns(cell, header):
        g, h = cell["grid"], header["grid"]
        return g["col_start"] == h["col_start"] and g["col_end"] == h["col_end"]

    # Explicit full-width conclusion ends the product section, NOT a fixed row
    # count or the last successfully recognized serial. Unrecognized footers
    # remain subject to the ordinary fail-closed structural checks.
    left = min(c["grid"]["col_start"] for c in cells)
    right = max(c["grid"]["col_end"] for c in cells)
    footers = [c for c in cells if c["grid"]["row_start"] >= end
               and c["grid"]["col_start"] == left and c["grid"]["col_end"] == right
               and c["normalized_text"].startswith(("检验结论", "检查意见", "验收意见"))]
    stop = min((c["grid"]["row_start"] for c in footers), default=len(grid["y_edges"]))
    body = [c for c in cells if end <= c["grid"]["row_start"] < stop]
    anchors = sorted([c for c in cells if same_columns(c, headers["row_no"])
                      and end <= c["grid"]["row_start"] < stop], key=lambda c: c["grid"]["row_start"])
    if not anchors:
        issues.append("no_serial_column_product_cells")
    # Do not silently omit a product when a detector substitutes a tiny OCR box
    # for the full merged serial cell (observed in the other supplied mode).
    for cell in body:
        g = cell["grid"]
        inside_serial = serial["col_start"] <= g["col_start"] and g["col_end"] <= serial["col_end"]
        if g["row_start"] >= end and inside_serial and not same_columns(cell, headers["row_no"]):
            issues.append(f"partial_serial_column_cell:{cell['cell_id']}")
        if g["row_start"] >= end and any(same_columns(cell,h) for h in headers.values()):
            owners = [a for a in anchors if a["grid"]["row_start"] <= g["row_start"]
                      and g["row_end"] <= a["grid"]["row_end"]]
            if len(owners) != 1:
                issues.append(f"unanchored_or_shared_data_cell:{cell['cell_id']}")
    if issues:
        return [], [], headers, sorted(set(issues))
    records, blanks = [], []
    for anchor in anchors:
        a = anchor["grid"]
        row_issues = list(issues)
        fields = {}
        shared_cell = None
        if "certificate_no" in headers and same_columns(headers["manufacturer"], headers["certificate_no"]):
            shared = [c for c in body if same_columns(c, headers["manufacturer"])
                      and c["grid"]["row_start"] == a["row_start"]
                      and c["grid"]["row_end"] == a["row_end"]]
            if len(shared) == 1:
                shared_cell = shared[0]
                fields.update(split_shared_cell(shared_cell))
        for name, header in headers.items():
            if shared_cell is not None and name in ("manufacturer", "certificate_no"):
                continue
            h = header["grid"]
            same_header_column = [other for other in headers.values() if same_columns(other, header)]
            split = len(same_header_column) > 1
            candidates = []
            for cell in cells:
                g = cell["grid"]
                if not same_columns(cell, header) or g["row_start"] < a["row_start"] or g["row_end"] > a["row_end"]:
                    continue
                if split:
                    lo = (g["row_start"]-a["row_start"])/a["rowspan"]
                    hi = (g["row_end"]-a["row_start"])/a["rowspan"]
                    hlo = (h["row_start"]-start)/(end-start)
                    hhi = (h["row_end"]-start)/(end-start)
                    if lo < hlo-.01 or hi > hhi+.01:
                        continue
                candidates.append(cell)
            chosen = candidates[0] if len(candidates) == 1 else None
            fields[name] = field_value(name, chosen, header)
            if name in header_warnings:
                fields[name]["issues"].extend("header:"+i for i in header_warnings[name])
                fields[name]["header_evidence"] = field_value("header", header)["evidence"]
            if name in header_confidence:
                fields[name]["header_confidence_decision"] = header_confidence[name]
                fields[name]["header_evidence"] = field_value("header", header)["evidence"]
            if len(candidates) != 1:
                fields[name]["issues"].append(f"cell_missing_or_ambiguous:{len(candidates)}")
            if quantity_list and name == 'specification' and not any(
                    not i.startswith('low_ocr_score:') for i in fields[name]['issues']):
                decision = field_confidence_decision(chosen, 'specification', 'entry_quantity_list',
                                                     min_score, confidence_policy)
                if decision:
                    fields[name]['confidence_decision'] = decision
                    fields[name]['issues'] = [i for i in fields[name]['issues']
                                             if i not in decision['resolved_issues']]
        if "certificate_no" not in fields:
            fields["certificate_no"] = {"value": None, "raw_text": "", "lines": [], "evidence": None,
                "source_status": "header_not_detected" if unknown_headers else "not_in_source",
                "extraction_method": "quantity_list_without_certificate_column",
                "absence_basis": {"title_ocr_indices": [m["ocr_index"] for m in title_tokens],
                                  "header_cell_ids": [h["cell_id"] for h in headers.values()],
                                  "unrecognized_header_cell_ids": unknown_headers},
                "issues": ["unrecognized_header_cannot_confirm_absence"] if unknown_headers else []}
        # Fail closed when unassigned text lies in this product's physical band.
        top = grid["y_edges"][a["row_start"]]
        bottom = grid["y_edges"][a["row_end"]]
        unresolved = [m["ocr_index"] for m in matches if m["match_status"] != "assigned"
                      and top <= (m["bbox"][1]+m["bbox"][3])/2 < bottom
                      and grid["x_edges"][0] <= (m["bbox"][0]+m["bbox"][2])/2 <= grid["x_edges"][-1]]
        if unresolved:
            row_issues.append("unresolved_ocr_in_product_band")
        related = [c for c in cells if a["row_start"] <= c["grid"]["row_start"]
                   and c["grid"]["row_end"] <= a["row_end"]]
        if not any(c["raw_text"].strip() for c in related) and not unresolved:
            blanks.append({"anchor_cell_id": anchor["cell_id"], "grid_rows": [a["row_start"], a["row_end"]],
                           "status": "blank_candidate", "note": "No OCR text, not proof the image is blank."})
            continue
        noise_evidence = isolated_dot_blank_evidence(related, fields, unresolved)
        if noise_evidence is not None:
            blanks.append({"anchor_cell_id": anchor["cell_id"],
                           "grid_rows": [a["row_start"], a["row_end"]],
                           "status": "blank_candidate_noise", "requires_review": True,
                           "note": "Only one tiny low-score dot; source evidence retained, not confirmed blank.",
                           "noise_evidence": [noise_evidence]})
            continue
        for name, field in fields.items():
            row_issues.extend(f"{name}:{problem}" for problem in field["issues"])
        records.append({"record_id": f"{anchor['cell_id']}:product", "anchor_cell_id": anchor["cell_id"],
                        "entry_form_type": "entry_quantity_list" if quantity_list else "inspection_record",
                        "grid_rows": [a["row_start"], a["row_end"]],
                        "row_no": fields["row_no"]["value"], "fields": fields,
                        "unresolved_ocr_indices": unresolved, "issues": sorted(set(row_issues)),
                        "status": "rule_pass_review_required" if not row_issues else "needs_review",
                        "ready_for_search": False})
    serial_values = [record["row_no"] for record in records if record["row_no"] is not None]
    for record in records:
        if record["row_no"] is not None and serial_values.count(record["row_no"]) > 1:
            record["issues"].append("duplicate_row_no")
            record["status"] = "needs_review"
    return records, blanks, headers, sorted(set(issues))


def analyze_candidate(table, tokens, options, table_index):
    cells = [{"cell_id": f"t{table_index}_c{index:04d}", "bbox": rectangle(box), "tokens": [], "issues": []}
             for index, box in enumerate(table.get("cell_box_list", []))]
    matches = assign_texts(cells, tokens, options.min_coverage, options.min_margin, options.min_score)
    grid = recover_grid(cells)
    records, blanks, headers, issues = assemble_records(cells, grid, matches, options.min_score)
    bounds = ([min(c["bbox"][0] for c in cells), min(c["bbox"][1] for c in cells),
               max(c["bbox"][2] for c in cells), max(c["bbox"][3] for c in cells)] if cells else None)
    for m in matches:
        if bounds and text_coverage(bounds, m["bbox"]) == 0:
            m["match_status"] = "outside_table"
    # Audit the material column against full-page OCR, even below the detected
    # table bottom. Otherwise a truncated candidate could silently drop a row.
    material_ids = []
    material = headers.get("material_name")
    serial = headers.get("row_no")
    if material and serial:
        box = material["bbox"]
        footer_tops = [c["bbox"][1] for c in cells
                      if c["grid"]["col_start"] == 0
                      and c["grid"]["col_end"] == len(grid["x_edges"])-1
                      and c["bbox"][1] > serial["bbox"][3]
                      and c["normalized_text"].startswith(("检验结论", "检查意见", "验收意见"))]
        stop_y = min(footer_tops, default=float("inf"))
        material_ids = [t["ocr_index"] for t in tokens if "钢筋" in normalize(t["text"])
                        and box[0] <= (t["bbox"][0]+t["bbox"][2])/2 <= box[2]
                        and serial["bbox"][3] <= (t["bbox"][1]+t["bbox"][3])/2 < stop_y]
        covered = {t["ocr_index"] for r in records
                   for t in (r["fields"]["material_name"]["evidence"] or {}).get("ocr_tokens", [])}
        if records and set(material_ids) - covered:
            issues.append("product_material_text_not_covered_by_records")
    for record in records:
        record["source_table_candidate"] = table_index
    record_unresolved = {i for r in records for i in r["unresolved_ocr_indices"]}
    noise_blanks = sum(b.get("requires_review", False) for b in blanks)
    accepted_tokens = {decision['ocr_index'] for r in records for f in r['fields'].values()
                       for key in ('confidence_decision', 'header_confidence_decision')
                       if (decision := f.get(key)) is not None}
    return {"schema_version": "coordinate_match_experiment_v2_1", "production_ready": False,
        "coordinate_system": "input_image_pixels; original PDF coordinates require render_meta offset",
        "profile": "rebar_explicit_headers_serial_anchor", "table_issues": issues,
        "headers": {k: {"cell_id": v["cell_id"], "raw_text": v["raw_text"], "grid": v["grid"]}
                    for k,v in headers.items()},
        "candidate_index": table_index, "candidate_bbox": bounds,
        "serial_header_ocr_indices": [t["ocr_index"] for t in serial["tokens"]] if serial else [],
        "material_ocr_indices": material_ids,
        "grid": grid, "cells": cells, "ocr_matches": matches,
        "records": records, "blank_candidates": blanks,
        "summary": {"ocr_text_count": len(tokens), "cell_count": len(cells),
            "empty_ocr_text_count": sum(not t["text"].strip() for t in tokens),
            "assigned_text_count": sum(m["match_status"] == "assigned" for m in matches),
            "unassigned_text_count": sum(m["match_status"] == "unassigned" for m in matches),
            "unassigned_record_band_text_count": sum(m["match_status"] == "unassigned" and m["ocr_index"] in record_unresolved for m in matches),
            "unassigned_other_text_count": sum(m["match_status"] == "unassigned" and m["ocr_index"] not in record_unresolved for m in matches),
            "outside_table_text_count": sum(m["match_status"] == "outside_table" for m in matches),
            "ambiguous_text_count": sum(m["match_status"] == "ambiguous" for m in matches),
            "product_count": len(records), "blank_candidate_count": len(blanks),
            "noise_blank_candidate_count": noise_blanks,
            "blank_needs_review_count": noise_blanks,
            "needs_review_count": sum(r["status"] == "needs_review" for r in records),
            "field_policy_accepted_token_count": len(accepted_tokens),
            "table_issue_count": len(issues),
            "status": "needs_review" if issues or noise_blanks or any(r["issues"] for r in records)
                      else "rule_pass_review_required" if records else "no_product_records"}}


def record_signature(candidate):
    """Compare actual values AND raw OCR ownership, not just row counts/weights."""
    return [{"fields": {k: f["value"] for k, f in r["fields"].items()},
             "field_tokens": {k: sorted(t["ocr_index"] for t in (f["evidence"] or {}).get("ocr_tokens", []))
                              for k, f in r["fields"].items()},
             "issues": r["issues"]} for r in candidate["records"]]


def analyze(data, options):
    data = data.get("res", data)
    if data.get("model_settings", {}).get("use_doc_preprocessor"):
        raise ValueError("This prototype requires unwarping/orientation preprocessing disabled.")
    tables = data.get("table_res_list", [])
    if not tables:
        raise ValueError("No detected table candidates in input JSON.")
    # Multi-candidate comparison requires one common OCR source / coordinate frame.
    if len(tables) > 1 and not data.get("overall_ocr_res", {}).get("rec_texts"):
        raise ValueError("Multiple candidates require overall_ocr_res; cannot compare local OCR indices.")
    tokens = load_tokens(data)
    candidates = [analyze_candidate(table, tokens, options, i+1) for i, table in enumerate(tables)]
    usable = [c for c in candidates if not c["table_issues"] and c["records"]]
    chosen = usable[0] if usable else candidates[0]
    decision = "single_candidate"
    selection_issues = []
    if len(candidates) > 1:
        if not usable:
            selection_issues.append("no_usable_table_candidate")
        else:
            signature = record_signature(chosen)
            for other in candidates:
                a, b = chosen["candidate_bbox"], other["candidate_bbox"]
                overlap = max(text_coverage(a, b), text_coverage(b, a)) if a and b else 0
                same_header = (bool(chosen["serial_header_ocr_indices"])
                               and chosen["serial_header_ocr_indices"] == other["serial_header_ocr_indices"])
                if overlap < .80 or not same_header:
                    selection_issues.append("multiple_independent_or_unresolved_table_candidates")
                elif not set(other["material_ocr_indices"]).issubset(chosen["material_ocr_indices"]):
                    selection_issues.append("candidate_has_additional_product_evidence")
                if other["records"] and record_signature(other) != signature:
                    selection_issues.append("overlapping_candidates_disagree_on_records_or_evidence")
                if other["records"] and (len(other["blank_candidates"]) != len(chosen["blank_candidates"])
                        or sorted(e["ocr_index"] for b in other["blank_candidates"] for e in b.get("noise_evidence", []))
                        != sorted(e["ocr_index"] for b in chosen["blank_candidates"] for e in b.get("noise_evidence", []))):
                    selection_issues.append("overlapping_candidates_disagree_on_blank_evidence")
            if not selection_issues:
                # Equal product values / OCR ownership: retain the compact complete
                # candidate, not simply the largest box or the model's first result.
                chosen = min(usable, key=lambda c: ((c["candidate_bbox"][2]-c["candidate_bbox"][0])
                             * (c["candidate_bbox"][3]-c["candidate_bbox"][1]), c["candidate_index"]))
                decision = "overlapping_same_header_and_consistent_product_evidence"
    audits = [{"candidate_index": c["candidate_index"], "bbox": c["candidate_bbox"],
               "serial_header_ocr_indices": c["serial_header_ocr_indices"],
               "material_ocr_indices": c["material_ocr_indices"], "summary": dict(c["summary"]),
               "table_issues": list(c["table_issues"]), "blank_candidates": list(c["blank_candidates"]),
               "record_signature": record_signature(c)} for c in candidates]
    # A table-level completeness problem must not expose a partial list as final.
    blocked = sorted(set(selection_issues + chosen["table_issues"]))
    if blocked:
        chosen["records"] = []
        chosen["blank_candidates"] = []
        chosen["table_issues"] = blocked
        chosen["summary"].update(product_count=0, blank_candidate_count=0,
                                 noise_blank_candidate_count=0, blank_needs_review_count=0,
                                 field_policy_accepted_token_count=0,
                                 needs_review_count=0, table_issue_count=len(blocked), status="needs_review")
        decision = "blocked_review_candidate_diagnostics"
    chosen["table_selection"] = {"candidate_count": len(candidates),
        "selected_candidate": None if blocked else chosen["candidate_index"],
        "decision": decision, "issues": blocked,
        "note": "Only overlapping candidates for one logical table are supported; independent tables require review."}
    chosen["table_candidates"] = audits
    chosen["summary"].update(table_candidate_count=len(candidates),
        selected_table_candidate=chosen["table_selection"]["selected_candidate"],
        suppressed_duplicate_candidate_count=len(candidates)-1 if not blocked else 0)
    return chosen


def draw_coordinate_svg(result):
    cells = result["cells"]
    if not cells:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="80"><text x="10" y="40">No cells</text></svg>'
    width = max(c["bbox"][2] for c in cells)+10
    height = max(c["bbox"][3] for c in cells)+10
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
             '<rect width="100%" height="100%" fill="white"/>']
    for cell in cells:
        x1,y1,x2,y2 = cell["bbox"]
        parts.append(f'<rect x="{x1}" y="{y1}" width="{x2-x1}" height="{y2-y1}" fill="none" stroke="#7b8ba1" stroke-width="2"/>')
    for match in result["ocr_matches"]:
        x1,y1,x2,y2 = match["bbox"]
        color = "#176145" if match["match_status"] == "assigned" else "#c43131"
        size = min((y2-y1)*.8, (x2-x1)/max(len(match["text"]),1)*.9)
        parts.append(f'<rect x="{x1}" y="{y1}" width="{x2-x1}" height="{y2-y1}" fill="none" stroke="{color}" stroke-width="1"/>')
        parts.append(f'<text x="{x1}" y="{y1+size}" font-size="{size}" fill="{color}">{html.escape(match["text"])}</text>')
    return "".join(parts)+"</svg>"


def write_review(result, out, with_image=False):
    grid = result["grid"]
    occupied = {}
    starts = {}
    for cell in result["cells"]:
        g = cell["grid"]
        starts[(g["row_start"],g["col_start"])] = cell
        for row in range(g["row_start"],g["row_end"]):
            for col in range(g["col_start"],g["col_end"]):
                occupied[(row,col)] = cell["cell_id"]
    rows = []
    if not grid["issues"]:
        for row in range(max(0,len(grid["y_edges"])-1)):
            pieces = []
            for col in range(max(0,len(grid["x_edges"])-1)):
                cell = starts.get((row,col))
                if cell:
                    g = cell["grid"]
                    pieces.append(f'<td rowspan="{g["rowspan"]}" colspan="{g["colspan"]}" title="{cell["cell_id"]}">'
                                  +html.escape(cell["raw_text"]).replace("\n","<br>")+"</td>")
                elif (row,col) not in occupied:
                    pieces.append('<td class="gap">未检测到格子</td>')
            rows.append("<tr>"+"".join(pieces)+"</tr>")
    field_rows = []
    for record in result["records"]:
        f = record["fields"]
        texts = [str(record["row_no"] if record["row_no"] is not None else "待复核")]
        for name in ("material_name","specification","quantity","manufacturer","certificate_no"):
            value = f[name]["value"]
            texts.append((str(value) if value is not None else "待复核")
                         if f[name].get("extraction_method") else f[name]["raw_text"])
        texts += [record["status"], "; ".join(record["issues"])]
        field_rows.append("<tr>"+"".join("<td>"+html.escape(t).replace("\n","<br>")+"</td>" for t in texts)+"</tr>")
    style = """body{font-family:Arial,'Microsoft YaHei',sans-serif;background:#f4f6fa;color:#172333;margin:24px}
table{border-collapse:collapse;background:white;width:100%}td,th{border:1px solid #8f9db0;padding:10px;text-align:left}
.scroll{overflow:auto;margin:16px 0}.gap{background:#ffe8dd}img{max-width:100%}a{color:#1262b0}
pre{white-space:pre-wrap}h2{margin-top:28px}"""
    warning = html.escape(json.dumps(result["table_issues"],ensure_ascii=False))
    content = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>坐标配对测试</title><style>'+style+'</style>'
    content += '<h1>单元格坐标＋OCR坐标：离线配对测试</h1><p>不调用模型；不读取原错误 HTML；规则通过仍需人工核对。</p>'
    content += '<p><a href="records.json">产品字段字典</a> · <a href="coordinate_result.json">完整配对证据</a> · <a href="summary.json">统计</a></p>'
    content += '<pre>'+html.escape(json.dumps(result["summary"],ensure_ascii=False,indent=2))+'</pre><p>表级问题：'+warning+'</p>'
    content += '<p>候选表选择：'+html.escape(json.dumps(result.get("table_selection",{}),ensure_ascii=False))+'</p>'
    content += '<h2>产品记录</h2><div class="scroll"><table><tr><th>序号</th><th>材料名称</th><th>规格</th><th>数量</th><th>厂家</th><th>质证书号</th><th>状态</th><th>问题</th></tr>'+"".join(field_rows)+'</table></div>'
    content += '<h2>由单元格坐标重建的网格</h2><div class="scroll"><table>'+"".join(rows)+'</table></div>'
    content += '<h2>坐标证据示意图（不是原图）</h2><p>灰色为单元格框，绿色为已配对文字，红色为未确定文字。</p><img src="coordinates.svg" alt="Coordinate evidence">'
    if with_image:
        content += '<h2>已验证哈希的输入图</h2><img src="input.png" alt="Input image">'
    content += '</html>'
    (out / "index.html").write_text(content,encoding="utf-8")
    (out / "coordinates.svg").write_text(draw_coordinate_svg(result),encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--image",type=Path,help="Optional exact inference input; requires --summary for hash validation")
    parser.add_argument("--summary",type=Path,help="Optional original inference summary.json")
    parser.add_argument("--no-html", action="store_true", help="Export JSON only; no HTML/SVG is required for extraction")
    parser.add_argument("--min-coverage",type=float,default=.80)
    parser.add_argument("--min-margin",type=float,default=.15)
    parser.add_argument("--min-score",type=float,default=.95,
                        help="General OCR threshold; nondefault values disable the quantity-list field exceptions")
    args = parser.parse_args()
    try:
        if args.out.exists():
            raise ValueError("Output exists; use a new --out. Original results are never overwritten.")
        for key in ("min_coverage","min_margin","min_score"):
            if not 0 < getattr(args,key) <= 1:
                raise ValueError(f"Invalid {key}: expected (0,1].")
        source_bytes = args.result.read_bytes()
        source = json.loads(source_bytes.decode("utf-8-sig"))
        inference_summary = json.loads(args.summary.read_text(encoding="utf-8-sig")) if args.summary else None
        if args.image:
            if not inference_summary:
                raise ValueError("--image requires --summary to verify the exact inference input.")
            image_hash = hashlib.sha256(args.image.read_bytes()).hexdigest()
            if image_hash != inference_summary.get("input_image_sha256"):
                raise ValueError("Input image hash differs from inference summary; refusing misleading overlay.")
        started = time.perf_counter()
        result = analyze(source,args)
        result["provenance"] = {"source_result": str(args.result.resolve()),
            "source_result_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "input_image_sha256": inference_summary.get("input_image_sha256") if inference_summary else None,
            "image_verified": bool(args.image),
            "thresholds": {k:getattr(args,k) for k in ("min_coverage","min_margin","min_score")}}
        result["summary"]["offline_processing_seconds"] = round(time.perf_counter()-started,4)
        args.out.mkdir(parents=True)
        write_json(args.out / "coordinate_result.json",result)
        write_json(args.out / "records.json",{k:result[k] for k in ("schema_version","production_ready","provenance","table_selection","records","blank_candidates","table_issues")})
        write_json(args.out / "summary.json",result["summary"])
        if args.image:
            shutil.copy2(args.image,args.out / "input.png")
        if not args.no_html:
            write_review(result,args.out,bool(args.image))
        print(json.dumps(result["summary"],ensure_ascii=False,indent=2))
        print(f"Output: {args.out}/records.json (diagnostics: coordinate_result.json). No OCR/VLM was called.")
    except (ValueError,FileNotFoundError,json.JSONDecodeError) as exc:
        parser.exit(2,f"Error: {exc}\n")


if __name__ == "__main__":
    main()
