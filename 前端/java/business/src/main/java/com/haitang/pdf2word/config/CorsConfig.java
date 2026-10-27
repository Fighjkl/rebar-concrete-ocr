package com.haitang.pdf2word.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class CorsConfig implements WebMvcConfigurer {
    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/**")
                .allowedOriginPatterns("*")
                // PATCH is used by the ledger cell correction endpoint.  Omitting it
                // makes the browser's preflight fail with Spring's plain-text
                // "Invalid CORS request" response, which the Vue client then tries
                // to parse as JSON ("Unexpected token 'I'").
                .allowedMethods("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
                .allowedHeaders("*")
                .allowCredentials(true)
                .maxAge(3600);
    }
}
