package com.haitang.pdf2word.utils;

public final class StringUtils {
    private StringUtils() {}
    public static boolean isBlank(CharSequence s) {
        if (s == null) return true;
        int len = s.length();
        for (int i = 0; i < len; i++) {
            if (!Character.isWhitespace(s.charAt(i))) return false;
        }
        return true;
    }
    public static boolean isNotBlank(CharSequence s) {
        return !isBlank(s);
    }
    public static String firstNotBlank(String... values) {
        if (values == null) return null;
        for (String v : values) {
            if (isNotBlank(v)) return v;
        }
        return null;
    }
}
