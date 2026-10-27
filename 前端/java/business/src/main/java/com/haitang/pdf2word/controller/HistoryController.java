package com.haitang.pdf2word.controller;

import com.haitang.pdf2word.db.HistoryEntity;
import com.haitang.pdf2word.model.HistoryView;
import com.haitang.pdf2word.other.ApiResponse;
import com.haitang.pdf2word.service.LibraryService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/history")
@RequiredArgsConstructor
public class HistoryController {

    private final LibraryService libraryService;

    @GetMapping
    public Map<String, Object> list() {
        List<HistoryView> views = new ArrayList<>();
        for (HistoryEntity h : libraryService.historyList()) {
            HistoryView v = new HistoryView();
            v.setId(h.getId());
            v.setPdfName(h.getPdfName());
            v.setOriginalName(h.getOriginalName());
            v.setPageCount(h.getPageCount());
            v.setOcrCachedCount(h.getOcrCachedCount());
            v.setLedgerRecognized(h.getLedgerRecognized());
            v.setLedgerHallucinationCount(h.getLedgerHallucinationCount());
            v.setLastStatus(h.getLastStatus());
            v.setLastSummary(h.getLastSummary());
            v.setLastError(h.getLastError());
            v.setCreatedAt(h.getCreatedAt());
            v.setUpdatedAt(h.getUpdatedAt());
            views.add(v);
        }
        return ApiResponse.ok(views);
    }
}