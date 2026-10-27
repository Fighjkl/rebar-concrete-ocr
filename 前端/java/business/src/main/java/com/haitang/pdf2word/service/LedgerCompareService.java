package com.haitang.pdf2word.service;

import com.haitang.pdf2word.exception.BizException;
import com.haitang.pdf2word.model.MasterLedgerCell;
import com.haitang.pdf2word.model.MasterLedgerResponse;
import com.haitang.pdf2word.model.MasterLedgerRow;
import com.haitang.pdf2word.utils.LightXlsxReader;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 人工识别 Excel 与系统识别台账的比对：
 * 先对齐字段，再按“规格品种+炉批号+验收数量+质保书编号”唯一键对齐记录，
 * 键全空时退回按行顺序对齐，最后逐格比较并输出差异。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class LedgerCompareService {

    private static final List<String> KEY_FIELDS = Collections.unmodifiableList(Arrays.asList(
            "规格品种", "炉批号", "验收数量", "质保书编号"));

    private static final List<String> COMBINED_DELIVERY_ALIASES = Collections.unmodifiableList(Arrays.asList(
            "送货单位编号炉批号", "送货单编号炉批号", "送货单编号/炉批号", "送货单编号／炉批号"));

    private final LibraryService libraryService;

    public Map<String, Object> compare(String pdfName, MultipartFile file) {
        if (file == null || file.isEmpty()) {
            throw new BizException(400, "请选择要导入的人工识别 Excel");
        }
        String filename = file.getOriginalFilename();
        if (filename == null || !filename.toLowerCase().endsWith(".xlsx")) {
            throw new BizException(400, "暂只支持 .xlsx 格式的人工识别结果");
        }

        List<List<String>> rawRows;
        try {
            rawRows = LightXlsxReader.readFirstSheet(file.getInputStream());
        } catch (IOException e) {
            throw new BizException("Excel 读取失败: " + e.getMessage());
        }

        MasterLedgerResponse ledger = libraryService.getPdfLedgerRows(pdfName);

        HeaderMap headerMap = mapHeader(rawRows);
        if (!headerMap.ok) {
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("status", "field_unmatched");
            data.put("field_alignment", headerMap.alignment);
            data.put("record_alignment", emptyRecordAlignment());
            data.put("diff_count", 0);
            data.put("total_cells", 0);
            data.put("accuracy", null);
            data.put("diffs", Collections.emptyList());
            return data;
        }

        List<CompareRow> manualRows = buildManualRows(rawRows, headerMap.headerRowIndex, headerMap);
        List<CompareRow> recognizedRows = buildRecognizedRows(ledger);

        MatchResult match = alignRows(manualRows, recognizedRows);

        List<Map<String, Object>> diffs = new ArrayList<>();
        int totalCells = 0;
        int diffCount = 0;
        for (MatchedPair pair : match.pairs) {
            for (String col : LibraryService.MASTER_COLUMNS) {
                totalCells++;
                String manualValue = normCompare(pair.manual.cells.get(col));
                String recognizedValue = normCompare(pair.recognized.cells.get(col));
                if (!manualValue.equals(recognizedValue)) {
                    diffCount++;
                    Map<String, Object> diff = new LinkedHashMap<>();
                    diff.put("manual_label", pair.manual.label);
                    diff.put("recognized_label", pair.recognized.label);
                    diff.put("record_key", pairKeyLabel(pair.manual, pair.recognized));
                    diff.put("field", col);
                    diff.put("manual_value", nullToEmpty(pair.manual.cells.get(col)));
                    diff.put("recognized_value", nullToEmpty(pair.recognized.cells.get(col)));
                    Integer page = pair.recognized.pages.get(col);
                    if (page != null) diff.put("page", page);
                    diffs.add(diff);
                }
            }
        }

        Map<String, Object> recordAlignment = new LinkedHashMap<>();
        recordAlignment.put("manual_count", manualRows.size());
        recordAlignment.put("recognized_count", recognizedRows.size());
        recordAlignment.put("matched_count", match.pairs.size());
        recordAlignment.put("unmatched_manual", unmatchedLabels(match.unmatchedManual));
        recordAlignment.put("unmatched_recognized", unmatchedLabels(match.unmatchedRecognized));

        String status;
        if (!match.unmatchedManual.isEmpty() || !match.unmatchedRecognized.isEmpty()) {
            status = "record_unmatched";
        } else if (diffCount > 0) {
            status = "diff";
        } else {
            status = "matched";
        }

        double accuracy = totalCells == 0 ? 1.0 : 1.0 - (double) diffCount / totalCells;
        accuracy = Math.round(accuracy * 10000.0) / 10000.0;

        Map<String, Object> data = new LinkedHashMap<>();
        data.put("status", status);
        data.put("field_alignment", headerMap.alignment);
        data.put("record_alignment", recordAlignment);
        data.put("diff_count", diffCount);
        data.put("total_cells", totalCells);
        data.put("accuracy", accuracy);
        data.put("diffs", diffs);
        return data;
    }

    // ============== 字段对齐 ==============

    private static HeaderMap mapHeader(List<List<String>> rawRows) {
        int headerRowIndex = -1;
        for (int i = 0; i < rawRows.size(); i++) {
            if (hasAnyNonBlank(rawRows.get(i))) {
                headerRowIndex = i;
                break;
            }
        }
        if (headerRowIndex < 0) {
            throw new BizException(400, "Excel 表头为空，请使用系统导出的台账模板填写");
        }

        List<String> header = rawRows.get(headerRowIndex);
        Map<String, Integer> colByField = new LinkedHashMap<>();
        Map<Integer, Boolean> combinedCols = new HashMap<>();
        List<String> extraColumns = new ArrayList<>();

        for (int ci = 0; ci < header.size(); ci++) {
            String raw = header.get(ci);
            if (isBlank(raw)) continue;
            String norm = normHeader(raw);
            String field = exactField(norm);
            if (field != null) {
                colByField.putIfAbsent(field, ci);
                continue;
            }
            if (COMBINED_DELIVERY_ALIASES.contains(norm)) {
                combinedCols.put(ci, Boolean.TRUE);
                colByField.putIfAbsent("送货单编号", ci);
                colByField.putIfAbsent("炉批号", ci);
                continue;
            }
            extraColumns.add(raw);
        }

        List<String> missing = new ArrayList<>();
        for (String col : LibraryService.MASTER_COLUMNS) {
            if (!colByField.containsKey(col)) missing.add(col);
        }

        Map<String, Object> alignment = new LinkedHashMap<>();
        alignment.put("ok", missing.isEmpty());
        alignment.put("missing_columns", missing);
        alignment.put("extra_columns", extraColumns);

        HeaderMap result = new HeaderMap();
        result.ok = missing.isEmpty();
        result.alignment = alignment;
        result.headerRowIndex = headerRowIndex;
        result.colByField = colByField;
        result.combinedCols = combinedCols;
        return result;
    }

    private static String exactField(String normHeader) {
        for (String col : LibraryService.MASTER_COLUMNS) {
            if (normHeader.equals(normHeader(col))) return col;
        }
        return null;
    }

    // ============== 记录构建 ==============

    private static List<CompareRow> buildManualRows(List<List<String>> rawRows,
                                                    int headerRowIndex,
                                                    HeaderMap headerMap) {
        List<CompareRow> rows = new ArrayList<>();
        for (int i = headerRowIndex + 1; i < rawRows.size(); i++) {
            List<String> row = rawRows.get(i);
            if (!hasAnyNonBlank(row)) continue;
            CompareRow cr = new CompareRow();
            cr.label = "Excel 第" + (i + 1) + "行";
            for (String col : LibraryService.MASTER_COLUMNS) {
                Integer idx = headerMap.colByField.get(col);
                String value = "";
                if (idx != null && idx < row.size()) {
                    value = nullToEmpty(row.get(idx));
                    if (headerMap.combinedCols.containsKey(idx)) {
                        value = splitCombined(value, col);
                    }
                }
                cr.cells.put(col, value);
            }
            computeKey(cr);
            rows.add(cr);
        }
        return rows;
    }

    private static List<CompareRow> buildRecognizedRows(MasterLedgerResponse ledger) {
        List<CompareRow> rows = new ArrayList<>();
        for (MasterLedgerRow row : ledger.getRows()) {
            CompareRow cr = new CompareRow();
            cr.label = "识别记录 #" + (row.getRecordIndex() == null ? rows.size() + 1 : row.getRecordIndex() + 1);
            for (String col : LibraryService.MASTER_COLUMNS) {
                MasterLedgerCell cell = row.getCells().get(col);
                cr.cells.put(col, cell == null ? "" : nullToEmpty(cell.getValue()));
                if (cell != null && cell.getPage() != null) {
                    cr.pages.put(col, cell.getPage());
                }
            }
            computeKey(cr);
            rows.add(cr);
        }
        return rows;
    }

    private static void computeKey(CompareRow row) {
        StringBuilder key = new StringBuilder();
        List<String> parts = new ArrayList<>();
        boolean any = false;
        for (String field : KEY_FIELDS) {
            String raw = row.cells.get(field);
            String norm = normCompare(raw);
            if (!norm.isEmpty()) {
                key.append('|').append(norm);
                parts.add(raw == null ? "" : raw.trim());
                any = true;
            }
        }
        row.keyFilled = any;
        row.key = any ? key.substring(1) : null;
        row.keyLabel = any ? joinNonEmpty(parts, " / ") : "键为空";
    }

    private static String splitCombined(String raw, String field) {
        String v = raw == null ? "" : raw.trim();
        int slash = -1;
        for (int i = 0; i < v.length(); i++) {
            char ch = v.charAt(i);
            if (ch == '/' || ch == '／') {
                slash = i;
                break;
            }
        }
        if (slash < 0) {
            return "送货单编号".equals(field) ? v : "";
        }
        String first = v.substring(0, slash).trim();
        String second = v.substring(slash + 1).trim();
        return "送货单编号".equals(field) ? first : second;
    }

    // ============== 记录对齐 ==============

    private static MatchResult alignRows(List<CompareRow> manualRows, List<CompareRow> recognizedRows) {
        Map<String, Deque<CompareRow>> manualByKey = new LinkedHashMap<>();
        Map<String, Deque<CompareRow>> recognizedByKey = new LinkedHashMap<>();
        List<CompareRow> manualEmpty = new ArrayList<>();
        List<CompareRow> recognizedEmpty = new ArrayList<>();

        for (CompareRow row : manualRows) {
            if (row.keyFilled) {
                manualByKey.computeIfAbsent(row.key, k -> new ArrayDeque<>()).add(row);
            } else {
                manualEmpty.add(row);
            }
        }
        for (CompareRow row : recognizedRows) {
            if (row.keyFilled) {
                recognizedByKey.computeIfAbsent(row.key, k -> new ArrayDeque<>()).add(row);
            } else {
                recognizedEmpty.add(row);
            }
        }

        List<MatchedPair> pairs = new ArrayList<>();
        List<CompareRow> unmatchedManual = new ArrayList<>();
        List<CompareRow> unmatchedRecognized = new ArrayList<>();

        Set<String> allKeys = new LinkedHashSet<>();
        allKeys.addAll(manualByKey.keySet());
        allKeys.addAll(recognizedByKey.keySet());
        for (String key : allKeys) {
            Deque<CompareRow> manualQueue = manualByKey.get(key);
            Deque<CompareRow> recognizedQueue = recognizedByKey.get(key);
            if (manualQueue == null) manualQueue = new ArrayDeque<>();
            if (recognizedQueue == null) recognizedQueue = new ArrayDeque<>();
            while (!manualQueue.isEmpty() && !recognizedQueue.isEmpty()) {
                pairs.add(new MatchedPair(manualQueue.pollFirst(), recognizedQueue.pollFirst()));
            }
            unmatchedManual.addAll(manualQueue);
            unmatchedRecognized.addAll(recognizedQueue);
        }

        int emptyMatched = Math.min(manualEmpty.size(), recognizedEmpty.size());
        for (int i = 0; i < emptyMatched; i++) {
            pairs.add(new MatchedPair(manualEmpty.get(i), recognizedEmpty.get(i)));
        }
        unmatchedManual.addAll(manualEmpty.subList(emptyMatched, manualEmpty.size()));
        unmatchedRecognized.addAll(recognizedEmpty.subList(emptyMatched, recognizedEmpty.size()));

        MatchResult result = new MatchResult();
        result.pairs = pairs;
        result.unmatchedManual = unmatchedManual;
        result.unmatchedRecognized = unmatchedRecognized;
        return result;
    }

    // ============== 工具方法 ==============

    private static String pairKeyLabel(CompareRow manual, CompareRow recognized) {
        if (manual.keyFilled) return manual.keyLabel;
        if (recognized.keyFilled) return recognized.keyLabel;
        return "键为空";
    }

    private static List<String> unmatchedLabels(List<CompareRow> rows) {
        List<String> labels = new ArrayList<>();
        for (CompareRow row : rows) {
            labels.add(row.label + "（" + row.keyLabel + "）");
        }
        return labels;
    }

    private static Map<String, Object> emptyRecordAlignment() {
        Map<String, Object> alignment = new LinkedHashMap<>();
        alignment.put("manual_count", 0);
        alignment.put("recognized_count", 0);
        alignment.put("matched_count", 0);
        alignment.put("unmatched_manual", Collections.emptyList());
        alignment.put("unmatched_recognized", Collections.emptyList());
        return alignment;
    }

    private static String normCompare(String s) {
        if (s == null) return "";
        String v = s.trim();
        v = v.replaceAll("(?i)\\s*\\(p\\d+\\)\\s*$", "").trim();
        v = v.replaceAll("(?i)\\s*（p\\d+）\\s*$", "").trim();
        v = v.replaceAll("^\\s*⚠\\s*", "");
        v = fullWidthToHalfWidth(v);
        v = v.replaceAll("[\\s\\u3000]", "");
        v = v.toLowerCase();
        v = v.replace("³", "3").replace("²", "2");
        return v;
    }

    private static String normHeader(String s) {
        if (s == null) return "";
        String v = fullWidthToHalfWidth(s.trim());
        v = v.replaceAll("[\\s\\u3000]", "");
        return v.toLowerCase();
    }

    private static String fullWidthToHalfWidth(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch >= 0xFF01 && ch <= 0xFF5E) {
                sb.append((char) (ch - 0xFEE0));
            } else {
                sb.append(ch);
            }
        }
        return sb.toString();
    }

    private static String joinNonEmpty(List<String> parts, String separator) {
        StringBuilder sb = new StringBuilder();
        for (String part : parts) {
            if (isBlank(part)) continue;
            if (sb.length() > 0) sb.append(separator);
            sb.append(part);
        }
        return sb.toString();
    }

    private static boolean hasAnyNonBlank(List<String> row) {
        for (String s : row) {
            if (!isBlank(s)) return true;
        }
        return false;
    }

    private static boolean isBlank(String s) {
        return s == null || s.trim().isEmpty();
    }

    private static String nullToEmpty(String s) {
        return s == null ? "" : s;
    }

    private static class HeaderMap {
        private boolean ok;
        private Map<String, Object> alignment;
        private int headerRowIndex;
        private Map<String, Integer> colByField;
        private Map<Integer, Boolean> combinedCols;
    }

    private static class CompareRow {
        private String label;
        private final Map<String, String> cells = new LinkedHashMap<>();
        private final Map<String, Integer> pages = new HashMap<>();
        private String key;
        private boolean keyFilled;
        private String keyLabel;
    }

    private static class MatchedPair {
        private final CompareRow manual;
        private final CompareRow recognized;

        private MatchedPair(CompareRow manual, CompareRow recognized) {
            this.manual = manual;
            this.recognized = recognized;
        }
    }

    private static class MatchResult {
        private List<MatchedPair> pairs;
        private List<CompareRow> unmatchedManual;
        private List<CompareRow> unmatchedRecognized;
    }
}
