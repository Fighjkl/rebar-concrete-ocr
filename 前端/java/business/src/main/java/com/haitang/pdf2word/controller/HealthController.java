package com.haitang.pdf2word.controller;

import com.haitang.pdf2word.other.ApiResponse;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.HashMap;
import java.util.Map;

@RestController
public class HealthController {
    @GetMapping("/api/ping")
    public Map<String, Object> ping() {
        Map<String, Object> data = new HashMap<>();
        data.put("ok", true);
        return ApiResponse.ok(data);
    }
}
