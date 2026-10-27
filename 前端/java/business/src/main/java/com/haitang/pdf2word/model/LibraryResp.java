package com.haitang.pdf2word.model;

import lombok.Data;
import java.util.List;

@Data
public class LibraryResp {
    private String root;
    private List<LibraryPdf> pdfs;
}