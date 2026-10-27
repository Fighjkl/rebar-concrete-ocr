package com.haitang.pdf2word;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableAsync;

@SpringBootApplication
@ConfigurationPropertiesScan
@EnableAsync
public class PdfToWordApplication {
    public static void main(String[] args) {
        SpringApplication.run(PdfToWordApplication.class, args);
    }
}
