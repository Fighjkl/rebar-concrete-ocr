package com.haitang.pdf2word.model;

import lombok.Data;

@Data
public class LibraryPage {
    private Integer page;
    private String image;
    private Boolean ocrCached = false;
}