package com.haitang.pdf2word.utils;

import java.util.regex.Pattern;

public final class PdfNameUtils {

    private static final Pattern INVALID = Pattern.compile("[\\\\/:*?\"<>|\\s]");

    private PdfNameUtils() {}

    /** Strip the .pdf extension and sanitize for use as a directory name. */
    public static String deriveName(String originalFilename) {
        if (originalFilename == null || originalFilename.trim().isEmpty()) {
            return "upload";
        }
        String name = originalFilename.trim();
        int dot = name.toLowerCase().lastIndexOf(".pdf");
        if (dot > 0) name = name.substring(0, dot);
        name = name.replaceAll("[\\.\\s]+$", "");
        if (name.isEmpty()) name = "upload";
        if (INVALID.matcher(name).find()) {
            name = INVALID.matcher(name).replaceAll("_");
        }
        return name;
    }

    /** Suffix to disambiguate collisions: "name__2", "name__3", ... */
    public static String deriveUnique(java.util.function.Predicate<String> exists, String baseName) {
        if (!exists.test(baseName)) return baseName;
        for (int i = 2; i < 1000; i++) {
            String candidate = baseName + "__" + i;
            if (!exists.test(candidate)) return candidate;
        }
        return baseName + "__" + System.currentTimeMillis();
    }
}