package com.haitang.pdf2word.utils;

import com.haitang.pdf2word.exception.BizException;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * 轻量 xlsx 读取器：只读第一个 sheet，支持 inlineStr / sharedStrings / 数字单元格。
 * 不引入 Apache POI，与现有手写 ZIP 导出保持同一套实现风格。
 */
public final class LightXlsxReader {

    private LightXlsxReader() {
    }

    public static List<List<String>> readFirstSheet(InputStream in) {
        Map<String, byte[]> entries = readZipEntries(in);
        byte[] workbookXml = entries.get("xl/workbook.xml");
        if (workbookXml == null) {
            throw new BizException("不是有效的 xlsx 文件（缺少 workbook）");
        }
        String sheetTarget = firstSheetTarget(parseXml(workbookXml), entries);
        byte[] sheetXml = entries.get(sheetTarget);
        if (sheetXml == null) {
            throw new BizException("xlsx 缺少工作表: " + sheetTarget);
        }
        List<String> sharedStrings = readSharedStrings(entries.get("xl/sharedStrings.xml"));
        return readSheetRows(sheetXml, sharedStrings);
    }

    private static Map<String, byte[]> readZipEntries(InputStream in) {
        Map<String, byte[]> entries = new HashMap<>();
        try (ZipInputStream zis = new ZipInputStream(in)) {
            ZipEntry entry;
            while ((entry = zis.getNextEntry()) != null) {
                if (entry.isDirectory()) continue;
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = zis.read(buf)) > 0) {
                    out.write(buf, 0, n);
                }
                entries.put(normalizePath(entry.getName()), out.toByteArray());
            }
        } catch (IOException e) {
            throw new BizException("不是有效的 xlsx 文件: " + e.getMessage());
        }
        if (entries.isEmpty()) {
            throw new BizException("不是有效的 xlsx 文件（zip 内容为空）");
        }
        return entries;
    }

    private static String normalizePath(String name) {
        String p = name == null ? "" : name.replace('\\', '/');
        while (p.startsWith("/")) p = p.substring(1);
        return p;
    }

    private static String firstSheetTarget(Document workbook, Map<String, byte[]> entries) {
        NodeList sheets = workbook.getElementsByTagName("sheet");
        String rid = "";
        if (sheets.getLength() > 0) {
            Element sheet = (Element) sheets.item(0);
            rid = sheet.getAttribute("r:id");
            if (rid.isEmpty()) rid = sheet.getAttribute("id");
        }
        if (!rid.isEmpty()) {
            byte[] relsXml = entries.get("xl/_rels/workbook.xml.rels");
            if (relsXml != null) {
                Document rels = parseXml(relsXml);
                NodeList relList = rels.getElementsByTagName("Relationship");
                for (int i = 0; i < relList.getLength(); i++) {
                    Element rel = (Element) relList.item(i);
                    if (rid.equals(rel.getAttribute("Id"))) {
                        return resolveTarget(rel.getAttribute("Target"));
                    }
                }
            }
        }
        return "xl/worksheets/sheet1.xml";
    }

    private static String resolveTarget(String target) {
        String t = target == null ? "" : target.replace('\\', '/');
        if (t.startsWith("/")) return t.substring(1);
        if (t.startsWith("xl/")) return t;
        return "xl/" + t;
    }

    private static List<String> readSharedStrings(byte[] xml) {
        List<String> list = new ArrayList<>();
        if (xml == null) return list;
        Document doc = parseXml(xml);
        NodeList items = doc.getElementsByTagName("si");
        for (int i = 0; i < items.getLength(); i++) {
            list.add(textOfAll((Element) items.item(i), "t"));
        }
        return list;
    }

    private static List<List<String>> readSheetRows(byte[] xml, List<String> sharedStrings) {
        Document doc = parseXml(xml);
        NodeList rows = doc.getElementsByTagName("row");
        List<List<String>> result = new ArrayList<>();
        for (int i = 0; i < rows.getLength(); i++) {
            Element rowEl = (Element) rows.item(i);
            NodeList cells = rowEl.getElementsByTagName("c");
            List<String> rowVals = new ArrayList<>();
            int nextCol = 0;
            for (int j = 0; j < cells.getLength(); j++) {
                Element c = (Element) cells.item(j);
                int col = columnIndex(c.getAttribute("r"));
                if (col < 0) col = nextCol;
                String value = cellValue(c, sharedStrings);
                while (rowVals.size() <= col) rowVals.add("");
                rowVals.set(col, value);
                nextCol = col + 1;
            }
            if (hasAnyNonBlank(rowVals)) result.add(rowVals);
        }
        return result;
    }

    private static String cellValue(Element c, List<String> sharedStrings) {
        String type = c.getAttribute("t");
        if ("s".equals(type)) {
            String idxText = textOfFirst(c, "v").trim();
            if (idxText.isEmpty()) return "";
            try {
                int idx = Integer.parseInt(idxText);
                return idx >= 0 && idx < sharedStrings.size() ? sharedStrings.get(idx) : "";
            } catch (NumberFormatException e) {
                return "";
            }
        }
        if ("inlineStr".equals(type)) {
            return textOfAll(c, "t");
        }
        return textOfFirst(c, "v");
    }

    private static int columnIndex(String ref) {
        if (ref == null || ref.isEmpty()) return -1;
        int idx = 0;
        boolean seenLetter = false;
        for (int i = 0; i < ref.length(); i++) {
            char ch = ref.charAt(i);
            if (ch >= 'A' && ch <= 'Z') {
                idx = idx * 26 + (ch - 'A' + 1);
                seenLetter = true;
            } else if (ch >= 'a' && ch <= 'z') {
                idx = idx * 26 + (ch - 'a' + 1);
                seenLetter = true;
            } else {
                break;
            }
        }
        return seenLetter ? idx - 1 : -1;
    }

    private static String textOfFirst(Element parent, String tag) {
        NodeList list = parent.getElementsByTagName(tag);
        if (list.getLength() == 0) return "";
        return list.item(0).getTextContent();
    }

    private static String textOfAll(Element parent, String tag) {
        NodeList list = parent.getElementsByTagName(tag);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < list.getLength(); i++) {
            sb.append(list.item(i).getTextContent());
        }
        return sb.toString();
    }

    private static boolean hasAnyNonBlank(List<String> row) {
        for (String s : row) {
            if (s != null && !s.trim().isEmpty()) return true;
        }
        return false;
    }

    private static Document parseXml(byte[] xml) {
        try {
            DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            try {
                factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
                factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
                factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
            } catch (Exception ignored) {
                // 旧 JDK 不支持部分特性时继续使用默认安全配置
            }
            factory.setXIncludeAware(false);
            factory.setExpandEntityReferences(false);
            DocumentBuilder builder = factory.newDocumentBuilder();
            return builder.parse(new ByteArrayInputStream(xml));
        } catch (Exception e) {
            throw new BizException("xlsx XML 解析失败: " + e.getMessage());
        }
    }
}
