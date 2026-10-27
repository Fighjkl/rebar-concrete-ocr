package com.haitang.pdf2word.model;

import lombok.Data;
import java.util.List;

/**
 * 总台账响应：所有已识别 PDF 的台账字段聚合视图。
 * 行 = 一条材料记录；钢筋 12 列，混凝土 10 列，按类别分别返回。
 */
@Data
public class MasterLedgerResponse {
    private List<String> columns;
    private List<MasterLedgerRow> rows;
    private boolean empty;
    private String revision;
    private Object diagnostics;
}
