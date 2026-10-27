package com.haitang.pdf2word.model;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 总台账中的一行（新版：一条材料记录一行，与 UI 对齐）。
 * cells 为 LinkedHashMap：字段名 → 单元格（单值）。
 */
@Data
public class MasterLedgerRow {
    @JsonProperty("material_type")
    private String materialType;
    @JsonProperty("pdf_name")
    private String pdfName;
    @JsonProperty("display_name")
    private String displayName;
    @JsonProperty("page_count")
    private Integer pageCount;
    @JsonProperty("ledger_recognized")
    private Boolean ledgerRecognized;
    @JsonProperty("record_index")
    private Integer recordIndex;
    @JsonProperty("hallucination_count")
    private Integer hallucinationCount;
    private Object diagnostics;
    @JsonProperty("match_candidates")
    private Object matchCandidates;
    @JsonProperty("match_status")
    private String matchStatus;
    @JsonProperty("review_confirmed")
    private boolean reviewConfirmed;
    private Map<String, MasterLedgerCell> cells = new LinkedHashMap<>();
}
