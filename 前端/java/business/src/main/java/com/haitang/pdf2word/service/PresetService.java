package com.haitang.pdf2word.service;

import com.haitang.pdf2word.model.Preset;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class PresetService {

    private final Preset preset;

    public Preset defaultPreset() {
        return preset;
    }

    public String defaultClassifyPrompt() {
        return preset.getClassifyPrompt();
    }

    public String defaultClassifySchema() {
        return preset.getClassifySchema();
    }
}
