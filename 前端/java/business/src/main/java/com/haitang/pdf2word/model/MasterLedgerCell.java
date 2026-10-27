package com.haitang.pdf2word.model;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 总台账单元格：单个字段下的单条取值。
 * value: 字段值；page: 来源页（1-based，null 表示无关联页）；
 * found_in_ocr: 是否能在来源页 OCR 文本中被匹配到（用于一致性红框）。
 */
@Data
public class MasterLedgerCell {
    private String value;
    private String unit;
    private Object polygon;
    private Object evidence;
    private Object diagnostics;
    private String status;
    private Integer page;
    @JsonProperty("found_in_ocr")
    private Boolean foundInOcr;
}
