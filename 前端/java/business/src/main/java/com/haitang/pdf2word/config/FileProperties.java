package com.haitang.pdf2word.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

@Data
@ConfigurationProperties(prefix = "file")
public class FileProperties {
    private String root = "./image";
    private String frontend = "../dist";
}
