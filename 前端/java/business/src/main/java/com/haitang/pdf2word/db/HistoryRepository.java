package com.haitang.pdf2word.db;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface HistoryRepository extends JpaRepository<HistoryEntity, Long> {

    Optional<HistoryEntity> findByPdfName(String pdfName);

    List<HistoryEntity> findAllByOrderByUpdatedAtDesc();

    @Modifying
    @Query("DELETE FROM HistoryEntity h WHERE h.pdfName = :pdfName")
    int deleteByPdfName(@Param("pdfName") String pdfName);
}