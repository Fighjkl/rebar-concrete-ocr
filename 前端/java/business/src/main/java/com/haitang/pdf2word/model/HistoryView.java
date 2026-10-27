package com.haitang.pdf2word.model;

import com.fasterxml.jackson.annotation.JsonFormat;
import lombok.Data;
import java.time.LocalDateTime;

@Data
public class HistoryView {
    private Long id;
    private String pdfName;
    private String originalName;
    private Integer pageCount;
    private Integer ocrCachedCount;
    private Boolean ledgerRecognized;
    private Integer ledgerHallucinationCount;
    private String lastStatus;
    private String lastSummary;
    private String lastError;
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss")
    private LocalDateTime createdAt;
    @JsonFormat(pattern = "yyyy-MM-dd HH:mm:ss")
    private LocalDateTime updatedAt;
}