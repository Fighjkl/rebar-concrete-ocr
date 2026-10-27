package com.haitang.pdf2word.model;

import lombok.Data;
import java.util.List;

@Data
public class LibraryPdf {
    private String name;
    private String materialType = "rebar";
    private Integer pageCount;
    private Integer ocrCachedCount;
    private Boolean ledgerRecognized = false;
    private Integer ledgerHallucinationCount;
    private String lastStatus;
    private String originalName;
    private Boolean hasSourcePdf = false;
    private List<LibraryPage> pages;
}
