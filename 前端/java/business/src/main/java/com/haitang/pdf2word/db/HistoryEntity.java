package com.haitang.pdf2word.db;

import lombok.Data;

import javax.persistence.*;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "history")
public class HistoryEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "pdf_name", length = 255, nullable = false, unique = true)
    private String pdfName;

    @Column(name = "original_name", length = 255, nullable = false)
    private String originalName;

    @Column(name = "page_count", nullable = false)
    private Integer pageCount = 0;

    @Column(name = "ocr_cached_count", nullable = false)
    private Integer ocrCachedCount = 0;

    @Column(name = "ledger_recognized", nullable = false)
    private Boolean ledgerRecognized = false;

    @Column(name = "ledger_hallucination_count")
    private Integer ledgerHallucinationCount;

    @Column(name = "last_status", length = 16)
    private String lastStatus;

    @Column(name = "last_summary", columnDefinition = "TEXT")
    private String lastSummary;

    @Column(name = "last_error", columnDefinition = "TEXT")
    private String lastError;

    @Column(name = "last_result_json", columnDefinition = "LONGTEXT")
    private String lastResultJson;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;
}