package com.haitang.pdf2word.utils;

import java.nio.file.Path;
import java.nio.file.Paths;

public final class PathUtils {

    private static Path ROOT;

    private PathUtils() {
    }

    public static void setRoot(Path root) {
        ROOT = root.toAbsolutePath().normalize();
    }

    public static Path getRoot() {
        ensureRoot();
        return ROOT;
    }

    public static Path libraryRoot() {
        ensureRoot();
        return ROOT.resolve("library");
    }

    public static Path libraryDir(String pdfName) {
        return libraryRoot().resolve(pdfName);
    }

    public static Path ocrDir(String pdfName) {
        return libraryDir(pdfName).resolve("ocr");
    }

    public static Path sourcePdf(String pdfName) {
        return libraryDir(pdfName).resolve("source.pdf");
    }

    public static Path pageImage(String pdfName, int page) {
        return libraryDir(pdfName).resolve(String.format("page_%04d.png", page));
    }

    public static Path pageOcrJson(String pdfName, int page) {
        return ocrDir(pdfName).resolve(String.format("page_%04d.ocr.json", page));
    }

    public static Path ledgerJson(String pdfName) {
        return libraryDir(pdfName).resolve("ledger.json");
    }

    public static Path resolveUnderLibrary(String pdfName, String relative) {
        Path base = libraryDir(pdfName).normalize();
        Path target = base.resolve(relative).normalize();
        if (!target.startsWith(base)) {
            throw new IllegalArgumentException("非法的文件路径: " + relative);
        }
        return target;
    }

    private static void ensureRoot() {
        if (ROOT == null) {
            setRoot(Paths.get("./image").toAbsolutePath().normalize());
        }
    }
}