package com.haitang.pdf2word.service;

import com.alibaba.fastjson2.JSON;
import com.alibaba.fastjson2.JSONArray;
import com.alibaba.fastjson2.JSONObject;
import com.haitang.pdf2word.exception.BizException;
import com.haitang.pdf2word.utils.StringUtils;
import java.math.BigDecimal;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/** Concrete approval uses strength and volume; rebar approval stays in its branch. */
public final class ConcreteLedgerRules {
    private ConcreteLedgerRules() {}
    public static final List<String> COLUMNS = Collections.unmodifiableList(Arrays.asList(
            "文件编号", "材料名称", "规格品种", "生产单位", "供应单位", "供货日期",
            "使用部位", "炉批号", "验收数量", "质保书编号"));

    public static JSONObject field(JSONObject fields, String column) {
        if (fields == null) return null;
        JSONObject cell = fields.getJSONObject(column);
        if (cell == null && "供货日期".equals(column))
            cell = fields.getJSONObject("供货日期与工程材料报审签认日期");
        return cell;
    }

    public static JSONObject consistency(JSONArray records) {
        JSONArray checks = new JSONArray();
        int missingOrUncertain = 0;
        for (int i = 0; i < records.size(); i++) {
            JSONObject fields = records.getJSONObject(i).getJSONObject("fields");
            for (String column : COLUMNS) {
                JSONObject source = field(fields, column);
                JSONObject check = source == null ? new JSONObject() : JSON.parseObject(source.toJSONString());
                check.put("path", "records[" + i + "]." + column);
                boolean found = source != null && source.getBooleanValue("found_in_ocr", false);
                check.put("found_in_ocr", found);
                if (!found) missingOrUncertain++;
                checks.add(check);
            }
        }
        JSONObject result = new JSONObject();
        result.put("field_checks", checks);
        result.put("field_count", checks.size());
        result.put("hallucination_count", missingOrUncertain);
        result.put("summary", "混凝土台账 " + records.size() + " 条记录 · 数量单位 m³ · " + missingOrUncertain + " 个字段待核对");
        return result;
    }

    public static void validateForExport(JSONObject ledger) {
        if (!"m3".equals(ledger.getString("quantity_unit")))
            throw new BizException(400, "混凝土数量单位须为立方米（m³）");
        List<String> required = Arrays.asList("材料名称", "规格品种", "生产单位", "供应单位",
                "供货日期", "使用部位", "验收数量");
        JSONArray records = ledger.getJSONArray("records");
        for (int i = 0; i < records.size(); i++) {
            JSONObject fields = records.getJSONObject(i).getJSONObject("fields");
            for (String key : required) {
                JSONObject cell = field(fields, key);
                if (cell == null || StringUtils.isBlank(cell.getString("value")))
                    throw new BizException(409, "第" + (i + 1) + "行「" + key + "」未补全，请双击单元格编辑");
            }
            String cellUnit = fields.getJSONObject("验收数量").getString("unit");
            if (cellUnit != null && !"m3".equals(cellUnit))
                throw new BizException(400, "混凝土数量单位须为立方米（m³）");
            try {
                if (new BigDecimal(fields.getJSONObject("验收数量").getString("value")).signum() <= 0)
                    throw new NumberFormatException();
            } catch (NumberFormatException e) {
                throw new BizException(400, "混凝土验收数量须为大于零的立方米数（m³）");
            }
        }
    }
}
