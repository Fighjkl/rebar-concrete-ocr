package com.haitang.pdf2word.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

@Data
@ConfigurationProperties(prefix = "ocr")
public class OcrProperties {
    private String baseUrl = "http://192.168.10.45:8000";
    private String apiKey = "123";
    private String imageFormat = "png";
    private Integer timeoutSeconds = 60;
    private Integer retry = 1;
}
