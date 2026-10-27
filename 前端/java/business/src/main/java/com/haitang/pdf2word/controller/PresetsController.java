package com.haitang.pdf2word.controller;

import com.haitang.pdf2word.model.Preset;
import com.haitang.pdf2word.other.ApiResponse;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/presets")
@RequiredArgsConstructor
public class PresetsController {

    private final Preset preset;

    @GetMapping
    public Map<String, Object> list() {
        List<Map<String, Object>> presets = new ArrayList<>();
        presets.add(toPresetView("default", "默认提取", "通用工程监理资料台账提取", preset));
        Map<String, Object> data = new HashMap<>();
        data.put("presets", presets);
        return ApiResponse.ok(data);
    }

    private Map<String, Object> toPresetView(String id, String name, String description, Preset p) {
        Map<String, Object> m = new HashMap<>();
        m.put("id", id);
        m.put("name", name);
        m.put("description", description);
        m.put("prompt", p.getPrompt());
        m.put("fields", p.getFields());
        m.put("classify_prompt", p.getClassifyPrompt());
        m.put("classify_schema", p.getClassifySchema());
        return m;
    }
}