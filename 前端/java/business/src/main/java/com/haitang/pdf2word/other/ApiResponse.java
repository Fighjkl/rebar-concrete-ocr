package com.haitang.pdf2word.other;

import lombok.Data;

import java.util.HashMap;
import java.util.Map;

@Data
public class ApiResponse<T> {

    private int code;
    private String msg;
    private T data;

    public static <T> Map<String, Object> ok(T data) {
        Map<String, Object> body = new HashMap<>();
        body.put("code", 200);
        body.put("msg", "success");
        body.put("data", data);
        return body;
    }

    public static Map<String, Object> error(int code, String msg) {
        Map<String, Object> body = new HashMap<>();
        body.put("code", code);
        body.put("msg", msg);
        body.put("data", null);
        return body;
    }
}
