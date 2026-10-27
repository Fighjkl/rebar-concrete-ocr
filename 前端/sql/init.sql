-- pdf_to_word 初始化脚本 (library-only 模式)
-- 与 OCR UI 对齐：磁盘为唯一数据源，history 表只存摘要

CREATE DATABASE IF NOT EXISTS pdf_to_word
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_general_ci;

USE pdf_to_word;

DROP TABLE IF EXISTS history;
CREATE TABLE history (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    pdf_name      VARCHAR(255) NOT NULL UNIQUE,
    original_name VARCHAR(255) NOT NULL,
    page_count    INT NOT NULL DEFAULT 0,
    ocr_cached_count INT NOT NULL DEFAULT 0,
    ledger_recognized TINYINT(1) NOT NULL DEFAULT 0,
    ledger_hallucination_count INT,
    last_status   VARCHAR(16),
    last_summary  TEXT,
    last_error    TEXT,
    last_result_json LONGTEXT,
    created_at    DATETIME NOT NULL,
    updated_at    DATETIME NOT NULL,
    INDEX idx_history_updated (updated_at),
    INDEX idx_history_pdf_name (pdf_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;