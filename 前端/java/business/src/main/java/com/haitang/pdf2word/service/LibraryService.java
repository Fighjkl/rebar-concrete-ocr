package com.haitang.pdf2word.service;

import com.alibaba.fastjson2.JSON;
import com.alibaba.fastjson2.JSONArray;
import com.alibaba.fastjson2.JSONObject;
import com.haitang.pdf2word.client.HttpOcrClient;
import com.haitang.pdf2word.db.HistoryEntity;
import com.haitang.pdf2word.db.HistoryRepository;
import com.haitang.pdf2word.enums.LibraryStatus;
import com.haitang.pdf2word.exception.BizException;
import com.haitang.pdf2word.model.*;
import com.haitang.pdf2word.model.MasterLedgerCell;
import com.haitang.pdf2word.model.MasterLedgerResponse;
import com.haitang.pdf2word.model.MasterLedgerRow;
import com.haitang.pdf2word.utils.PathUtils;
import com.haitang.pdf2word.utils.StringUtils;
import com.haitang.pdf2word.utils.PdfNameUtils;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.LocalDateTime;
import java.util.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

@Slf4j
@Service
@RequiredArgsConstructor
public class LibraryService {

    private static final String ENC = "UTF-8";
    private static final java.time.format.DateTimeFormatter TS_ID =
            java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss");

    /** 时间戳加UUID避免并发上传碰撞；本地与远端库保持同一标识。 */
    private String nextTimestampId() {
        return LocalDateTime.now().format(TS_ID) + "_" + UUID.randomUUID().toString().replace("-", "");
    }

    private final HttpOcrClient ocrClient;
    private final HistoryRepository historyRepository;
    private final Map<String, Object> recognitionLocks = new java.util.concurrent.ConcurrentHashMap<>();

    private Object recognitionLock(String pdfName) {
        return recognitionLocks.computeIfAbsent(pdfName, key -> new Object());
    }

    public JSONObject ocrModels() {
        return ocrClient.ocrModels();
    }

    @Transactional
        public UploadResult uploadPdf(MultipartFile file, Integer dpi, String imageFormat, Boolean overwrite, String nameOverride) {
        return uploadPdf(file, dpi, imageFormat, overwrite, nameOverride, "rebar");
    }

    @Transactional
    public UploadResult uploadPdf(MultipartFile file, Integer dpi, String imageFormat, Boolean overwrite, String nameOverride, String materialType) {
        if (!Arrays.asList("rebar", "concrete", "steel_beam").contains(materialType))
            throw new BizException(422, "无效材料类别");
        if (file == null || file.isEmpty()) {
            throw new BizException("无效的 PDF 文件");
        }
        String originalName = Optional.ofNullable(file.getOriginalFilename()).orElse("upload.pdf");
        // 每次主动上传生成时间戳 ID 作为库目录/历史记录标识，前端仍显示原文件名
        String pdfName = nextTimestampId();
        String displayName = StringUtils.isNotBlank(nameOverride) ? nameOverride : originalName;
        byte[] pdfBytes;
        try {
            pdfBytes = file.getBytes();
        } catch (IOException e) {
            throw new BizException("文件读取失败: " + e.getMessage());
        }
        int dpiVal = (dpi == null || dpi <= 0) ? 200 : dpi;
        String fmt = StringUtils.isBlank(imageFormat) ? "png" : imageFormat.trim().toLowerCase();
        // 先渲染成功，再清理旧目录，避免渲染失败导致旧数据丢失
        List<HttpOcrClient.PdfPageImage> pages;
        try {
            pages = ocrClient.pdfToImages(pdfBytes, originalName, fmt, dpiVal);
        } catch (Exception e) {
            throw new BizException("PDF 渲染失败: " + e.getMessage());
        }
        if (pages.isEmpty()) {
            throw new BizException("PDF 页数为空");
        }
        // 注册到远端 OCR 库（name 用同一时间戳 ID），保证后续 recognize / 单页 OCR 可用
        try {
            ocrClient.uploadToLibrary(pdfBytes, originalName, pdfName, dpiVal, fmt, Boolean.TRUE.equals(overwrite), materialType);
        } catch (Exception e) {
            throw new BizException("注册到 OCR 服务失败: " + e.getMessage());
        }
        boolean existed = libraryExists(pdfName);
        if (existed) {
            if (!Boolean.TRUE.equals(overwrite)) {
                throw new BizException(409, "同名 PDF「" + pdfName + "」已存在，是否覆盖？");
            }
            deleteDir(PathUtils.libraryDir(pdfName));
            historyRepository.findByPdfName(pdfName).ifPresent(historyRepository::delete);
        }
        Path dir = PathUtils.libraryDir(pdfName);
        try {
            Files.createDirectories(dir);
            Files.write(PathUtils.sourcePdf(pdfName), pdfBytes);
            JSONObject material = new JSONObject();
            material.put("material_type", materialType);
            writeFile(dir.resolve("material.json"), material.toJSONString());
        } catch (IOException e) {
            throw new BizException("目录/原始 PDF 保存失败: " + e.getMessage());
        }
        for (HttpOcrClient.PdfPageImage page : pages) {
            byte[] imageBytes = HttpOcrClient.decodeBase64(page.getImageBase64());
            Path target = PathUtils.pageImage(pdfName, page.getPage());
            try {
                Files.write(target, imageBytes);
            } catch (IOException e) {
                throw new BizException("图片写入失败: " + target);
            }
        }
        HistoryEntity h = historyRepository.findByPdfName(pdfName).orElseGet(HistoryEntity::new);
        h.setPdfName(pdfName);
        h.setOriginalName(displayName);
        h.setPageCount(pages.size());
        h.setOcrCachedCount(0);
        h.setLedgerRecognized(false);
        LocalDateTime now = LocalDateTime.now();
        if (h.getCreatedAt() == null) h.setCreatedAt(now);
        h.setUpdatedAt(now);
        h.setLastStatus(LibraryStatus.READY.name());
        historyRepository.save(h);
        log.info("PDF uploaded, pdf_name={}, pages={}, dpi={}, material_type={}, overwrite={}",
                pdfName, pages.size(), dpiVal, materialType, overwrite);
        UploadResult r = new UploadResult();
        r.pdfName = pdfName;
        r.originalName = originalName;
        r.pageCount = pages.size();
        r.dpi = dpiVal;
        r.format = fmt;
        r.sourcePdf = "source.pdf";
        r.overwritten = existed && Boolean.TRUE.equals(overwrite);
        r.materialType = materialType;
        return r;
    }

    /**
     * 重新切分：用本地 source.pdf 按新 DPI 重新渲染图片，清掉 OCR 缓存与台账。
     */
    public Map<String, Object> resplitPdf(String pdfName, Integer dpi, String imageFormat) {
        Path source = PathUtils.sourcePdf(pdfName);
        if (!Files.exists(source)) {
            throw new BizException(409, "该卷没有原始 PDF（旧卷），请重新上传后再重切");
        }
        byte[] pdfBytes;
        try {
            pdfBytes = Files.readAllBytes(source);
        } catch (IOException e) {
            throw new BizException("读取原始 PDF 失败: " + e.getMessage());
        }
        int dpiVal = (dpi == null || dpi <= 0) ? 200 : dpi;
        String fmt = StringUtils.isBlank(imageFormat) ? "png" : imageFormat.trim().toLowerCase();
        // 先同步远端重切，保证远端图片与本地一致
        try {
            ocrClient.resplitLibrary(pdfName, dpiVal, fmt);
        } catch (Exception e) {
            throw new BizException("同步重切到 OCR 服务失败: " + e.getMessage());
        }
        List<HttpOcrClient.PdfPageImage> pages;
        try {
            pages = ocrClient.pdfToImages(pdfBytes, pdfName + ".pdf", fmt, dpiVal);
        } catch (Exception e) {
            throw new BizException("PDF 重切失败: " + e.getMessage());
        }
        if (pages.isEmpty()) {
            throw new BizException("PDF 页数为空");
        }
        int clearedOcr = countOcrCached(pdfName);
        boolean clearedLedger = Files.exists(PathUtils.ledgerJson(pdfName));
        Path dir = PathUtils.libraryDir(pdfName);
        if (Files.exists(dir)) {
            try (Stream<Path> list = Files.list(dir)) {
                list.filter(p -> p.getFileName().toString().matches("page_\\d{4}\\.png"))
                        .forEach(this::deleteFile);
            } catch (IOException ignored) {
            }
            deleteDir(PathUtils.ocrDir(pdfName));
            deleteFile(PathUtils.ledgerJson(pdfName));
        }
        for (HttpOcrClient.PdfPageImage page : pages) {
            byte[] imageBytes = HttpOcrClient.decodeBase64(page.getImageBase64());
            try {
                Files.write(PathUtils.pageImage(pdfName, page.getPage()), imageBytes);
            } catch (IOException e) {
                throw new BizException("图片写入失败: " + PathUtils.pageImage(pdfName, page.getPage()));
            }
        }
        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            h.setPageCount(pages.size());
            h.setOcrCachedCount(0);
            h.setLedgerRecognized(false);
            h.setLedgerHallucinationCount(null);
            h.setLastSummary(null);
            h.setLastError(null);
            h.setLastResultJson(null);
            h.setLastStatus(LibraryStatus.READY.name());
            h.setUpdatedAt(LocalDateTime.now());
            historyRepository.save(h);
        });
        log.info("PDF resplit, pdf_name={}, pages={}, dpi={}", pdfName, pages.size(), dpiVal);
        Map<String, Object> data = new HashMap<>();
        data.put("name", pdfName);
        data.put("page_count", pages.size());
        data.put("dpi", dpiVal);
        data.put("format", fmt);
        data.put("cleared_ocr", clearedOcr);
        data.put("cleared_ledger", clearedLedger);
        return data;
    }

    /**
     * 单页旋转（90 的倍数，无损转置）。只刷新显示坐标，保留台账及导出前核验结果。
     */
    public Map<String, Object> rotatePage(String pdfName, int page, Integer angle) {
        int a = (angle == null) ? 90 : angle;
        if (a != 90 && a != 180 && a != 270) {
            throw new BizException("角度仅支持 90 / 180 / 270");
        }
        // 先同步远端旋转，保证远端图片与本地一致。
        // 注意：远端 rotate(angle) 为逆时针（PIL 语义），本地 rotate90 为正角度顺时针，
        // 因此传互补角（90<->270），避免两端方向相反（实测会相差 180°）。
        int remoteAngle = (a == 90) ? 270 : (a == 270 ? 90 : a);
        try {
            ocrClient.rotateLibraryPage(pdfName, page, remoteAngle);
        } catch (Exception e) {
            throw new BizException("同步旋转到 OCR 服务失败: " + e.getMessage());
        }
        Path image = PathUtils.pageImage(pdfName, page);
        if (!Files.exists(image)) {
            throw new BizException(404, "页码不存在: " + page);
        }
        BufferedImage src;
        try {
            src = ImageIO.read(Files.newInputStream(image));
        } catch (IOException e) {
            throw new BizException("读取图片失败: " + e.getMessage());
        }
        if (src == null) {
            throw new BizException("图片解析失败: " + image.getFileName());
        }
        BufferedImage rotated = rotate90(src, a);
        try {
            ImageIO.write(rotated, "png", image.toFile());
        } catch (IOException e) {
            throw new BizException("旋转图片写入失败: " + e.getMessage());
        }
        boolean clearedOcr = Files.exists(PathUtils.pageOcrJson(pdfName, page));
        deleteFile(PathUtils.pageOcrJson(pdfName, page));
        boolean refreshedOcr = false;
        try {
            // The OCR service retains original-page evidence and maps it to the
            // current display rotation. Replace only this page's coordinate cache.
            JSONObject ocr = ocrClient.getLibraryOcr(pdfName, page);
            if (ocr != null && ocr.containsKey("items") && StringUtils.isBlank(ocr.getString("error"))) {
                Files.createDirectories(PathUtils.ocrDir(pdfName));
                writeFile(PathUtils.pageOcrJson(pdfName, page), JSON.toJSONString(ocr));
                refreshedOcr = true;
            }
        } catch (Exception e) {
            // Rotation already succeeded. Hide stale boxes until the cache can
            // be fetched again, without deleting verified fields or human edits.
            log.warn("旋转后同步 OCR 坐标失败: {}#{} -> {}", pdfName, page, e.getMessage());
        }
        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            h.setOcrCachedCount(countOcrCached(pdfName));
            h.setUpdatedAt(LocalDateTime.now());
            historyRepository.save(h);
        });
        log.info("PDF page rotate, pdf_name={}, page={}, angle={}", pdfName, page, a);
        Map<String, Object> data = new HashMap<>();
        data.put("name", pdfName);
        data.put("page", page);
        data.put("angle", a);
        data.put("image", image.getFileName().toString());
        data.put("width", rotated.getWidth());
        data.put("height", rotated.getHeight());
        data.put("cleared_ocr", clearedOcr && !refreshedOcr);
        data.put("ocr_refreshed", refreshedOcr);
        data.put("cleared_ledger", false);
        return data;
    }

    private static BufferedImage rotate90(BufferedImage src, int angle) {
        int w = src.getWidth();
        int h = src.getHeight();
        int type = (src.getType() == BufferedImage.TYPE_CUSTOM) ? BufferedImage.TYPE_INT_ARGB : src.getType();
        java.awt.geom.AffineTransform tx = new java.awt.geom.AffineTransform();
        BufferedImage dst;
        switch (angle) {
            case 90:
                dst = new BufferedImage(h, w, type);
                tx.translate(h, 0);
                tx.rotate(Math.PI / 2);
                break;
            case 180:
                dst = new BufferedImage(w, h, type);
                tx.translate(w, h);
                tx.rotate(Math.PI);
                break;
            default:
                dst = new BufferedImage(h, w, type);
                tx.translate(0, w);
                tx.rotate(-Math.PI / 2);
                break;
        }
        java.awt.Graphics2D g = dst.createGraphics();
        g.setRenderingHint(java.awt.RenderingHints.KEY_INTERPOLATION, java.awt.RenderingHints.VALUE_INTERPOLATION_BILINEAR);
        g.drawImage(src, tx, null);
        g.dispose();
        return dst;
    }
public LibraryResp list() {
        LibraryResp resp = new LibraryResp();
        resp.setRoot(PathUtils.libraryRoot().toString());
        Path root = PathUtils.libraryRoot();
        if (!Files.exists(root)) {
            resp.setPdfs(Collections.emptyList());
            return resp;
        }
        List<LibraryPdf> pdfs = new ArrayList<>();
        try (Stream<Path> dirs = Files.list(root)) {
            dirs.filter(Files::isDirectory)
                .sorted(Comparator.comparing(p -> p.getFileName().toString()))
                .forEach(dir -> pdfs.add(buildLibraryPdf(dir.getFileName().toString())));
        } catch (IOException e) {
            throw new BizException("扫描 library 失败: " + e.getMessage());
        }
        resp.setPdfs(pdfs);
        return resp;
    }

    @Transactional
    public void clearAll() {
        Path root = PathUtils.libraryRoot();
        if (Files.exists(root)) {
            deleteDir(root);
        }
        try {
            Files.createDirectories(root);
        } catch (IOException ignored) {
        }
        historyRepository.deleteAll();
    }

    /**
     * 拉取 OCR 服务端的 library 列表（含图片 / OCR / ledger）落到本地，作为历史。
     * 不传 names = 全部；传则按名字精确过滤。
     * 容错：单 PDF 失败不阻断其他。
     */
    public ImportResult importFromRemote(List<String> names) {
        ImportResult result = new ImportResult();
        try {
            JSONObject remote = ocrClient.libraryList();
            JSONArray pdfs = remote.getJSONArray("pdfs");
            if (pdfs == null) {
                result.fatalError = "OCR 服务返回的 pdfs 为空";
                return result;
            }
            Set<String> filter = (names == null || names.isEmpty())
                    ? null : new java.util.LinkedHashSet<>(names);
            for (int i = 0; i < pdfs.size(); i++) {
                JSONObject pdf = pdfs.getJSONObject(i);
                String pdfName = pdf.getString("name");
                if (pdfName == null || pdfName.isEmpty()) continue;
                if (filter != null && !filter.contains(pdfName)) continue;
                ImportResult.Item item = importOneFromRemote(pdfName, pdf);
                result.results.add(item);
                switch (item.status) {
                    case "ok":
                    case "partial":
                        result.imported++;
                        break;
                    case "skipped":
                        result.skipped++;
                        break;
                    default:
                        result.errors++;
                }
            }
        } catch (Exception e) {
            result.fatalError = e.getMessage();
            log.warn("importFromRemote 失败: {}", e.getMessage());
        }
        return result;
    }

    private ImportResult.Item importOneFromRemote(String pdfName, JSONObject pdfInfo) {
        ImportResult.Item it = new ImportResult.Item();
        it.name = pdfName;
        try {
            int pageCount = pdfInfo.getIntValue("page_count", 0);
            JSONArray pages = pdfInfo.getJSONArray("pages");
            it.pages = pageCount;
            Path dir = PathUtils.libraryDir(pdfName);
            Files.createDirectories(dir);
            Files.createDirectories(PathUtils.ocrDir(pdfName));
            JSONObject material = new JSONObject();
            material.put("material_type", pdfInfo.getString("material_type") == null ? "rebar" : pdfInfo.getString("material_type"));
            writeFile(dir.resolve("material.json"), material.toJSONString());
            // 可选 ledger
            JSONObject ledger = null;
            try {
                ledger = ocrClient.getLibraryLedger(pdfName);
            } catch (Exception ledgerEx) {
                log.info("OCR 服务无台账: {} -> {}", pdfName, ledgerEx.getMessage());
            }
            // 逐页拉图片和 ocr
            int imgOk = 0;
            int ocrOk = 0;
            if (pages != null) {
                for (int pi = 0; pi < pages.size(); pi++) {
                    JSONObject pageInfo = pages.getJSONObject(pi);
                    int page = pageInfo.getIntValue("page", pi + 1);
                    boolean ocrCached = pageInfo.getBooleanValue("ocr_cached", false);
                    // 拉图片
                    try {
                        byte[] bytes = ocrClient.getLibraryImage(pdfName, page);
                        if (bytes != null && bytes.length > 0) {
                            Files.write(PathUtils.pageImage(pdfName, page), bytes);
                            imgOk++;
                        }
                    } catch (Exception imgEx) {
                        log.warn("图片拉取失败: {}#{}, {}", pdfName, page, imgEx.getMessage());
                    }
                    if (ocrCached) {
                        try {
                            JSONObject ocr = ocrClient.getLibraryOcr(pdfName, page);
                            injectPageFields(ocr, ledger, page);
                            writeFile(PathUtils.pageOcrJson(pdfName, page), JSON.toJSONString(ocr));
                            ocrOk++;
                        } catch (Exception ocrEx) {
                            log.warn("OCR 拉取失败: {}#{}, {}", pdfName, page, ocrEx.getMessage());
                        }
                    }
                }
            }
            it.imageCount = imgOk;
            it.ocrCount = ocrOk;
            // ledger
            if (ledger != null) {
                try {
                    JSONObject local = normalizeRemoteLedger(ledger, pageCount);
                    writeFile(PathUtils.ledgerJson(pdfName), JSON.toJSONString(local));
                    it.ledgerRecognized = true;
                    it.ledgerHallucination = local.getJSONObject("consistency").getIntValue("hallucination_count", 0);
                } catch (Exception ledgerSaveEx) {
                    log.warn("ledger 写入失败: {} -> {}", pdfName, ledgerSaveEx.getMessage());
                }
            }
            // history 行（无论识别状态都写）
            writeImportedHistory(pdfName, pageCount, ocrOk, it.ledgerRecognized, it.ledgerHallucination, ledger);
            if (it.ledgerRecognized) it.status = "ok";
            else if (ocrOk > 0 || imgOk > 0) it.status = "partial";
            else it.status = "skipped";
        } catch (Exception ex) {
            it.status = "error";
            it.message = ex.getMessage();
            log.warn("拉取失败: {} -> {}", pdfName, ex.getMessage());
        }
        return it;
    }

    private void injectPageFields(JSONObject ocr, JSONObject ledger, int page) {
        if (ledger == null) return;
        JSONObject cons = ledger.getJSONObject("consistency");
        if (cons == null) return;
        JSONArray chks = cons.getJSONArray("field_checks");
        if (chks == null || chks.isEmpty()) return;
        JSONArray pageFields = new JSONArray();
        for (int i = 0; i < chks.size(); i++) {
            JSONObject c = chks.getJSONObject(i);
            if (c == null) continue;
            if (c.getIntValue("page", -1) != page) continue;
            String name = extractCheckFieldName(c.getString("path"));
            String value = c.getString("value");
            if (name == null || name.isEmpty()) continue;
            JSONObject f = new JSONObject();
            f.put("name", name);
            f.put("value", value);
            pageFields.add(f);
        }
        ocr.put("fields", pageFields);
    }

    /**
     * 从 consistency field_check 的 path 里提取字段名：
     * 新版 "records[2].材料名称" → "材料名称"；旧版 "材料名称[0]" → "材料名称"。
     */
    private static String extractCheckFieldName(String path) {
        if (path == null) return null;
        String p = path.trim();
        if (p.startsWith("records[")) {
            int dot = p.indexOf('.');
            return dot > 0 ? p.substring(dot + 1) : p;
        }
        int br = p.lastIndexOf('[');
        return br > 0 ? p.substring(0, br) : p;
    }

    private JSONObject normalizeRemoteLedger(JSONObject remote, int pageCount) {
        JSONObject local = new JSONObject();
        local.put("pdf_name", remote.getString("pdf_name"));
        local.put("revision", java.util.UUID.randomUUID().toString());
        if (remote.getString("revision") != null) local.put("source_revision", remote.getString("revision"));
        for (String key : Arrays.asList("diagnostics", "matching_requires_human", "review_stage", "schema_version", "run_directory",
                "material_type", "quantity_unit", "material_matching", "ocr_backends", "columns", "registration_version")) {
            if (remote.containsKey(key)) local.put(key, remote.get(key));
        }
        if ("concrete".equals(remote.getString("material_type")) && remote.containsKey("certificate_number_deduplication"))
            local.put("certificate_number_deduplication", remote.get("certificate_number_deduplication"));
        local.put("page_count", pageCount > 0 ? pageCount : remote.getIntValue("page_count", 0));
        local.put("ts_utc", remote.getString("ts_utc"));
        if (remote.getString("model") != null) local.put("model", remote.getString("model"));
        if (remote.getString("model_id") != null) local.put("model_id", remote.getString("model_id"));
        if (remote.getString("ocr_cache_identity") != null) local.put("ocr_cache_identity", remote.get("ocr_cache_identity"));
        if (remote.getString("source_pdf_sha256") != null) local.put("source_pdf_sha256", remote.getString("source_pdf_sha256"));
        if (remote.getString("error") != null) local.put("error", remote.getString("error"));
        if (remote.getString("raw_response") != null) local.put("raw_response", remote.getString("raw_response"));
        JSONArray chunks = remote.getJSONArray("chunks");
        if (chunks != null) local.put("chunks", chunks);
        JSONArray chunkErrors = remote.getJSONArray("chunk_errors");
        if (chunkErrors != null) local.put("chunk_errors", chunkErrors);
        JSONObject rcons = remote.getJSONObject("consistency");
        JSONArray records = remote.getJSONArray("records");
        JSONObject lcons;
        if (records != null && !records.isEmpty()) {
            // 新版：records 为主，consistency 的 field_checks 统一重写为 records[i].字段
            local.put("records", records);
            // Current OCR contract already uses stable records[i].field paths;
            // retain missing checks, reasons and coordinate evidence verbatim.
            if ("automatic-ledger-v1".equals(remote.getString("schema_version")) && rcons != null) {
                lcons = rcons;
            } else if ("concrete".equals(remote.getString("material_type"))) {
                lcons = ConcreteLedgerRules.consistency(records);
            } else {
                lcons = rebuildConsistency(records, rcons);
            }
        } else {
            // 旧版兜底（兼容旧远端返回）：透传 ledger 与原始 consistency
            JSONObject ledgerFields = remote.getJSONObject("ledger");
            if (ledgerFields != null) local.put("ledger", ledgerFields);
            lcons = new JSONObject();
            JSONArray chks = rcons == null ? null : rcons.getJSONArray("field_checks");
            int fieldCount = chks == null ? 0 : chks.size();
            int hallu = rcons == null ? 0 : rcons.getIntValue("hallucination_count", 0);
            java.util.Map<String, Integer> perName = new java.util.LinkedHashMap<>();
            if (chks != null) {
                for (int i = 0; i < chks.size(); i++) {
                    JSONObject c = chks.getJSONObject(i);
                    String n = c == null ? null : extractCheckFieldName(c.getString("path"));
                    if (n != null && !n.isEmpty()) perName.merge(n, 1, Integer::sum);
                }
            }
            lcons.put("field_count", fieldCount);
            lcons.put("hallucination_count", hallu);
            lcons.put("fields_per_name", perName);
            lcons.put("field_checks", chks);
            String summary = rcons == null ? null : rcons.getString("summary");
            if (StringUtils.isBlank(summary)) {
                StringBuilder sb = new StringBuilder();
                sb.append("已生成 ").append(fieldCount).append(" 条记录 · ");
                sb.append(perName.size()).append(" 个字段");
                if (hallu > 0) sb.append(" · ⚠ ").append(hallu).append(" 处疑似幻觉");
                summary = sb.toString();
            }
            lcons.put("summary", summary);
        }
        String summary = lcons.getString("summary");
        local.put("summary", summary);
        local.put("consistency", lcons);
        return local;
    }

    /**
     * 按新版 records 重新生成 consistency：
     * field_checks 的 path 统一为 "records[{i}].{字段}"，
     * found_in_ocr 优先复用远端旧式校验结果（含合并字段 送货单位编号炉批号 的拆分匹配）。
     */
    private JSONObject rebuildConsistency(JSONArray records, JSONObject rcons) {
        JSONArray remoteChecks = rcons == null ? null : rcons.getJSONArray("field_checks");
        Map<String, java.util.Deque<RemoteCheck>> queueByName = new LinkedHashMap<>();
        if (remoteChecks != null) {
            for (int i = 0; i < remoteChecks.size(); i++) {
                JSONObject c = remoteChecks.getJSONObject(i);
                if (c == null) continue;
                String name = extractCheckFieldName(c.getString("path"));
                if (name == null || name.isEmpty()) continue;
                RemoteCheck rc = new RemoteCheck();
                rc.value = c.getString("value");
                rc.page = c.getIntValue("page", -1);
                rc.found = c.getBooleanValue("found_in_ocr", true);
                queueByName.computeIfAbsent(name, k -> new java.util.ArrayDeque<>()).add(rc);
            }
        }

        JSONArray checks = new JSONArray();
        Map<String, Integer> perName = new LinkedHashMap<>();
        int hallu = 0;
        for (int ri = 0; ri < records.size(); ri++) {
            JSONObject rec = records.getJSONObject(ri);
            if (rec == null) continue;
            JSONObject fields = rec.getJSONObject("fields");
            if (fields == null) continue;
            for (String fieldName : fields.keySet()) {
                JSONObject item = fields.getJSONObject(fieldName);
                if (item == null) continue;
                String value = item.getString("value");
                if (StringUtils.isBlank(value)) continue;
                int page = item.getIntValue("page", -1);
                boolean found = consumeFound(queueByName, fieldName, value, page);
                JSONObject check = new JSONObject();
                check.put("path", "records[" + ri + "]." + fieldName);
                check.put("value", value);
                check.put("page", page > 0 ? page : null);
                check.put("found_in_ocr", found);
                checks.add(check);
                perName.merge(fieldName, 1, Integer::sum);
                if (!found) hallu++;
            }
        }

        JSONObject lcons = new JSONObject();
        lcons.put("field_count", checks.size());
        lcons.put("hallucination_count", hallu);
        lcons.put("fields_per_name", perName);
        lcons.put("field_checks", checks);
        String summary = rcons == null ? null : rcons.getString("summary");
        if (StringUtils.isBlank(summary)) {
            summary = buildLedgerSummary(records.size(), checks.size(), hallu);
        }
        lcons.put("summary", summary);
        return lcons;
    }

    private boolean consumeFound(Map<String, java.util.Deque<RemoteCheck>> queueByName,
                                 String fieldName, String value, int page) {
        RemoteCheck hit = popMatching(queueByName.get(fieldName), value, page);
        if (hit != null) return hit.found;
        // 旧版合并字段 送货单位编号炉批号 -> 送货单编号 / 炉批号
        java.util.Deque<RemoteCheck> combined = queueByName.get("送货单位编号炉批号");
        if (combined != null && !combined.isEmpty()) {
            hit = popCombinedMatching(combined, value, page);
            if (hit != null) return hit.found;
        }
        // 远端无对应校验记录时默认视为命中，避免误报幻觉
        return true;
    }

    private static RemoteCheck popMatching(java.util.Deque<RemoteCheck> q, String value, int page) {
        if (q == null || q.isEmpty()) return null;
        RemoteCheck best = null;
        for (RemoteCheck c : q) {
            if (c.page == page && value != null && value.equals(c.value)) { best = c; break; }
        }
        if (best == null) {
            for (RemoteCheck c : q) {
                if (c.page == page) { best = c; break; }
            }
        }
        if (best != null) q.remove(best);
        return best;
    }

    private static RemoteCheck popCombinedMatching(java.util.Deque<RemoteCheck> q, String value, int page) {
        if (value == null) return null;
        RemoteCheck best = null;
        for (RemoteCheck c : q) {
            if (c.page == page && c.value != null && containsSegment(c.value, value)) { best = c; break; }
        }
        if (best == null) {
            for (RemoteCheck c : q) {
                if (c.page == page) { best = c; break; }
            }
        }
        if (best != null) q.remove(best);
        return best;
    }

    private static boolean containsSegment(String combined, String value) {
        for (String seg : combined.split("/")) {
            if (seg.trim().equals(value.trim())) return true;
        }
        return false;
    }

    private static String buildLedgerSummary(int recordCount, int checkCount, int hallu) {
        StringBuilder sb = new StringBuilder();
        sb.append("共 ").append(recordCount).append(" 条材料记录 · ").append(checkCount).append(" 个取值");
        if (hallu > 0) sb.append(" · ⚠ ").append(hallu).append(" 处疑似幻觉");
        return sb.toString();
    }

    @lombok.Data
    private static class RemoteCheck {
        private String value;
        private int page = -1;
        private boolean found = true;
    }

    private void writeImportedHistory(String pdfName, int pageCount, int ocrOk,
                                      boolean ledgerRecognized, int ledgerHallucination,
                                      JSONObject ledger) {
        try {
            HistoryEntity h = historyRepository.findByPdfName(pdfName).orElseGet(HistoryEntity::new);
            h.setPdfName(pdfName);
            if (h.getOriginalName() == null || h.getOriginalName().isEmpty()) {
                h.setOriginalName(pdfName);
            }
            h.setPageCount(pageCount);
            h.setOcrCachedCount(ocrOk);
            h.setLedgerRecognized(ledgerRecognized);
            if (ledgerRecognized) {
                h.setLedgerHallucinationCount(ledgerHallucination);
            }
            LocalDateTime now = LocalDateTime.now();
            if (h.getCreatedAt() == null) h.setCreatedAt(now);
            h.setUpdatedAt(now);
            h.setLastStatus(ledgerRecognized ? LibraryStatus.SUCCESS.name() : LibraryStatus.READY.name());
            if (ledger != null) {
                try {
                    h.setLastResultJson(JSON.toJSONString(ledger));
                } catch (Exception ignored) {
                }
                if (ledger.getString("summary") != null) {
                    h.setLastSummary(ledger.getString("summary"));
                }
            }
            historyRepository.save(h);
        } catch (Exception ex) {
            log.warn("写入 history 行失败: {} -> {}", pdfName, ex.getMessage());
        }
    }

    private LibraryPdf buildLibraryPdf(String pdfName) {
        Path dir = PathUtils.libraryDir(pdfName);
        LibraryPdf pdf = new LibraryPdf();
        pdf.setName(pdfName);
        pdf.setMaterialType(materialType(pdfName));
        int pageCount = 0;
        List<LibraryPage> pages = new ArrayList<>();
        int ocrCachedCount = 0;
        if (Files.exists(dir)) {
            try (Stream<Path> pageImages = Files.list(dir)) {
                List<Path> sorted = pageImages
                        .filter(p -> p.getFileName().toString().matches("page_\\d{4}\\.png"))
                        .sorted()
                        .collect(Collectors.toList());
                pageCount = sorted.size();
                for (Path p : sorted) {
                    String name = p.getFileName().toString();
                    int pageNum = Integer.parseInt(name.substring(5, 9));
                    LibraryPage lp = new LibraryPage();
                    lp.setPage(pageNum);
                    lp.setImage(name);
                    Path ocrFile = PathUtils.pageOcrJson(pdfName, pageNum);
                    boolean cached = isValidOcrCache(ocrFile);
                    if (cached) ocrCachedCount++;
                    lp.setOcrCached(cached);
                    pages.add(lp);
                }
            } catch (IOException ignored) {
            }
        }
        pdf.setPageCount(pageCount);
        pdf.setOcrCachedCount(ocrCachedCount);
        pdf.setPages(pages);
        pdf.setLedgerRecognized(Files.exists(PathUtils.ledgerJson(pdfName)));
        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            if (h.getLedgerHallucinationCount() != null) {
                pdf.setLedgerHallucinationCount(h.getLedgerHallucinationCount());
            }
            pdf.setLastStatus(h.getLastStatus());
            pdf.setOriginalName(h.getOriginalName());
        });
        pdf.setHasSourcePdf(Files.exists(PathUtils.sourcePdf(pdfName)));
        return pdf;
    }

    public Path pageImagePath(String pdfName, int page) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在");
        Path img = PathUtils.pageImage(pdfName, page);
        if (!Files.exists(img)) throw new BizException(404, "页面图片不存在");
        return img;
    }

    /** 识别/单页 OCR 前确保远端 OCR 库已注册该卷；本地历史卷缺远端目录时自动上传同步。 */
    private static final Object REMOTE_REGISTER_LOCK = new Object();

    private void ensureRemoteRegistered(String pdfName) {
        if (ocrClient.remoteLibraryExists(pdfName)) return;
        synchronized (REMOTE_REGISTER_LOCK) {
            if (ocrClient.remoteLibraryExists(pdfName)) return;
            uploadRemoteForce(pdfName);
            log.info("远端 OCR 库缺少该卷，已自动上传同步: pdf_name={}", pdfName);
        }
    }

    /** 强制把本地 source.pdf 上传到远端 OCR 库（name 与本地一致，overwrite=true 幂等）。 */
    private void uploadRemoteForce(String pdfName) {
        Path src = PathUtils.sourcePdf(pdfName);
        if (!Files.exists(src)) {
            throw new BizException("本地缺少 source.pdf，无法同步到远端 OCR 库，请重新上传该卷");
        }
        try {
            HistoryEntity h = historyRepository.findByPdfName(pdfName).orElse(null);
            String originalName = (h != null && StringUtils.isNotBlank(h.getOriginalName())) ? h.getOriginalName() : pdfName;
            ocrClient.uploadToLibrary(Files.readAllBytes(src), originalName, pdfName, 200, "png", true, materialType(pdfName));
        } catch (BizException e) {
            throw e;
        } catch (Exception e) {
            throw new BizException("同步到远端 OCR 库失败: " + e.getMessage());
        }
    }

    /** 判断远端错误是否因"该卷在远端不存在"，用于识别失败后自动重传重试。 */
    private boolean isRemoteMissingError(String msg) {
        if (msg == null) return false;
        String m = msg.toLowerCase();
        return m.contains("404") || m.contains("not found") || m.contains("目录不存在");
    }

    private String materialType(String pdfName) {
        Path path = PathUtils.libraryDir(pdfName).resolve("material.json");
        if (!Files.exists(path)) return "rebar";
        try {
            String type = JSON.parseObject(new String(Files.readAllBytes(path), ENC)).getString("material_type");
            if (!Arrays.asList("rebar", "concrete", "steel_beam").contains(type))
                throw new BizException(422, "无效材料类别");
            return type;
        } catch (IOException e) {
            throw new BizException("读取材料类别失败: " + e.getMessage());
        }
    }

    /** OCR 缓存有效判定：带 error 的错误响应视为无效，不参与缓存计数/渲染。 */
    private boolean isValidOcrCache(Path ocrFile) {
        if (!Files.exists(ocrFile)) return false;
        try {
            JSONObject o = JSON.parseObject(new String(Files.readAllBytes(ocrFile), ENC));
            if (o == null) return false;
            if (StringUtils.isNotBlank(o.getString("error"))) return false;
            return o.containsKey("items");
        } catch (Exception e) {
            return false;
        }
    }

    public JSONObject getPageOcr(String pdfName, int page) {
        Path ocr = PathUtils.pageOcrJson(pdfName, page);
        if (!isValidOcrCache(ocr)) throw new BizException(404, "页面 OCR 不存在");
        try {
            return JSON.parseObject(new String(Files.readAllBytes(ocr), ENC));
        } catch (IOException e) {
            throw new BizException("OCR 读取失败: " + e.getMessage());
        }
    }

    @Transactional
    public JSONObject runPageOcr(String pdfName, int page,
                                  String prompt, String fields,
                                  String classifyPrompt, String classifySchema) {
        pageImagePath(pdfName, page); // 仅校验本地图片存在
        ensureRemoteRegistered(pdfName);
        JSONObject resp;
        try {
            resp = ocrClient.runLibraryPageOcr(pdfName, page, prompt);
        } catch (Exception e) {
            throw new BizException("OCR 调用失败: " + e.getMessage());
        }
        savePageOcrCache(pdfName, page, resp);
        return resp;
    }

    @Transactional
    public JSONObject recognize(String pdfName,
                                String prompt, String fields,
                                String classifyPrompt, String classifySchema,
                                String ocrPrompt, String model,
                                boolean forceOcrRefresh) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在");
        ensureRemoteRegistered(pdfName);
        int[] pages = listPagesOnDisk(pdfName);
        if (pages.length == 0) throw new BizException("没有可识别的页面");
        log.info("识别开始: pdf_name={}, pages={}, force_ocr_refresh={}", pdfName, pages.length, forceOcrRefresh);
        JSONObject remote;
        try {
            remote = ocrClient.runLibraryRecognize(pdfName, prompt, ocrPrompt, model, forceOcrRefresh);
        } catch (Exception e) {
            if (isRemoteMissingError(e.getMessage())) {
                log.warn("远端 OCR 库缺少该卷，自动重传后重试: pdf_name={}", pdfName);
                uploadRemoteForce(pdfName);
                remote = ocrClient.runLibraryRecognize(pdfName, prompt, ocrPrompt, model, forceOcrRefresh);
            } else {
                throw new BizException("调用 OCR 识别失败: " + e.getMessage());
            }
        }
        String remoteError = remote == null ? "无返回" : remote.getString("error");
        if (StringUtils.isNotBlank(remoteError)) {
            throw new BizException("识别失败: " + remoteError);
        }
        synchronized (recognitionLock(pdfName)) {
            JSONObject state = ocrClient.getLibraryRecognition(pdfName);
            return saveRecognitionResult(pdfName, pages, remote, state);
        }
    }

    public JSONObject startRecognition(String pdfName, String prompt, String ocrPrompt,
                                        String model, boolean forceOcrRefresh) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在");
        if (listPagesOnDisk(pdfName).length == 0) throw new BizException("没有可识别的页面");
        ensureRemoteRegistered(pdfName);
        return ocrClient.startLibraryRecognition(pdfName, prompt, ocrPrompt, model, forceOcrRefresh);
    }

    /** 查询不持全局服务锁；只有完成后短暂按卷同步一次结果。 */
    public JSONObject getRecognition(String pdfName) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在");
        JSONObject state;
        try {
            state = ocrClient.getLibraryRecognition(pdfName);
        } catch (org.springframework.web.client.HttpClientErrorException ex) {
            if (ex.getStatusCode() != org.springframework.http.HttpStatus.NOT_FOUND) throw ex;
            // Historical local imports are registered only when a task starts.
            JSONObject idle = new JSONObject();
            idle.put("pdf_name", pdfName); idle.put("status", "idle"); idle.put("stage", "idle");
            idle.put("message", "尚未启动识别"); return idle;
        }
        if (state != null && "completed".equals(state.getString("status"))) {
            synchronized (recognitionLock(pdfName)) {
                String taskId = state.getString("task_id");
                // Python's cached flag does not imply that this Java copy
                // belongs to the same model or remote revision. Preserve human
                // edits only while the underlying remote source is unchanged.
                JSONObject local = Files.exists(PathUtils.ledgerJson(pdfName)) ? getLedger(pdfName) : null;
                String localModel = local == null ? null : (local.getString("model_id") != null ?
                        local.getString("model_id") : local.getString("model"));
                boolean taskChanged = !state.getBooleanValue("cached") && (local == null ||
                        !Objects.equals(taskId, local.getString("recognition_task_id")));
                boolean stateModelChanged = state.getString("model_id") != null &&
                        !Objects.equals(state.getString("model_id"), localModel);
                boolean stateIdentityChanged = state.containsKey("ocr_cache_identity") &&
                        !Objects.equals(state.get("ocr_cache_identity"), local == null ? null : local.get("ocr_cache_identity"));
                boolean stateRevisionChanged = state.getString("source_revision") != null &&
                        !Objects.equals(state.getString("source_revision"), local == null ? null : local.getString("source_revision"));
                boolean needRemoteLedger = local == null || taskChanged || stateModelChanged || stateIdentityChanged || stateRevisionChanged;
                JSONObject remoteLedger = needRemoteLedger ? ocrClient.getLibraryLedger(pdfName) : null;
                String remoteModel = remoteLedger == null ? null :
                        (remoteLedger.getString("model_id") != null ? remoteLedger.getString("model_id") : remoteLedger.getString("model"));
                boolean modelChanged = remoteModel != null && !Objects.equals(remoteModel, local == null ? null :
                        (local.getString("model_id") != null ? local.getString("model_id") : local.getString("model")));
                boolean identityChanged = remoteLedger != null && remoteLedger.containsKey("ocr_cache_identity")
                        && !Objects.equals(remoteLedger.get("ocr_cache_identity"), local == null ? null : local.get("ocr_cache_identity"));
                boolean revisionChanged = remoteLedger != null && remoteLedger.getString("revision") != null
                        && !Objects.equals(remoteLedger.getString("revision"), local == null ? null : local.getString("source_revision"));
                if (local == null || modelChanged || identityChanged || revisionChanged || (!state.getBooleanValue("cached")
                        && !Objects.equals(taskId, local.getString("recognition_task_id")))) {
                    saveRecognitionResult(pdfName, listPagesOnDisk(pdfName), remoteLedger, state);
                }
            }
            int[] pages = listPagesOnDisk(pdfName);
            if (Arrays.stream(pages).anyMatch(p -> !isValidOcrCache(PathUtils.pageOcrJson(pdfName, p)))) {
                syncRemotePageOcr(pdfName, pages, getLedger(pdfName));
            }
            state.put("local_ready", Arrays.stream(pages).allMatch(p -> isValidOcrCache(PathUtils.pageOcrJson(pdfName, p))));
        }
        return state;
    }

    private JSONObject saveRecognitionResult(String pdfName, int[] pages, JSONObject remote, JSONObject state) {
        String taskId = state == null ? null : state.getString("task_id");
        if (Files.exists(PathUtils.ledgerJson(pdfName))) {
            JSONObject local = getLedger(pdfName);
            String remoteModel = remote == null ? null :
                    (remote.getString("model_id") != null ? remote.getString("model_id") : remote.getString("model"));
            String localModel = local.getString("model_id") != null ? local.getString("model_id") : local.getString("model");
            Object remoteIdentity = remote == null ? null : remote.get("ocr_cache_identity");
            Object localIdentity = local.get("ocr_cache_identity");
            boolean sameModel = remoteModel == null || Objects.equals(remoteModel, localModel);
            boolean sameIdentity = remoteIdentity == null || Objects.equals(remoteIdentity, localIdentity);
            String remoteRevision = remote == null ? null : remote.getString("revision");
            boolean sameRevision = remoteRevision == null || Objects.equals(remoteRevision, local.getString("source_revision"));
            if (sameModel && sameIdentity && sameRevision && ((state != null && state.getBooleanValue("cached"))
                    || (taskId != null && taskId.equals(local.getString("recognition_task_id"))))) return local;
        }
        JSONObject ledger = normalizeRemoteLedger(remote, pages.length);
        ledger.put("recognition_task_id", taskId);
        saveLedgerCache(pdfName, ledger);
        syncRemotePageOcr(pdfName, pages, ledger);
        return ledger;
    }

    public JSONObject getLedger(String pdfName) {
        Path p = PathUtils.ledgerJson(pdfName);
        if (!Files.exists(p)) throw new BizException(404, "台账不存在");
        try {
            return JSON.parseObject(new String(Files.readAllBytes(p), ENC));
        } catch (IOException e) {
            throw new BizException("台账读取失败: " + e.getMessage());
        }
    }


    // ============== 总台账（跨 PDF） ==============

    /** 固定的 12 个监理字段，顺序即为表格列顺序（与新版 UI LEDGER_FIELDS 一致）。 */
    public static final List<String> MASTER_COLUMNS = Collections.unmodifiableList(Arrays.asList(
            "供应商是否报审", "文件编号", "材料名称", "规格品种",
            "生产单位", "供应单位", "供货日期与工程材料报审签认日期",
            "使用部位", "送货单编号", "炉批号", "验收数量", "质保书编号"));

    /**
     * 汇总所有已识别 PDF 的 ledger.json，形成总台账视图。
     * 读不到 ledger 的 PDF 不会出现在 rows 中。
     */
    public MasterLedgerResponse getMasterLedger() {
        return getMasterLedger("rebar");
    }

    public MasterLedgerResponse getMasterLedger(String materialType) {
        if (!Arrays.asList("rebar", "concrete").contains(materialType))
            throw new BizException(400, "无效台账类别");
        MasterLedgerResponse resp = new MasterLedgerResponse();
        resp.setColumns("concrete".equals(materialType) ? ConcreteLedgerRules.COLUMNS : MASTER_COLUMNS);

        Path root = PathUtils.libraryRoot();
        if (!Files.exists(root)) {
            resp.setRows(Collections.emptyList());
            resp.setEmpty(true);
            return resp;
        }
        List<String> pdfNames = new ArrayList<>();
        try (Stream<Path> dirs = Files.list(root)) {
            dirs.filter(Files::isDirectory)
                .forEach(d -> pdfNames.add(d.getFileName().toString()));
        } catch (IOException e) {
            throw new BizException("扫描 library 失败: " + e.getMessage());
        }
        Collections.sort(pdfNames);

        List<MasterLedgerRow> rows = new ArrayList<>();
        for (String name : pdfNames) {
            Path ledger = PathUtils.ledgerJson(name);
            if (!Files.exists(ledger)) continue;
            try {
                JSONObject obj = JSON.parseObject(new String(Files.readAllBytes(ledger), ENC));
                if (!materialType.equals(ledgerMaterial(name, obj))) continue;
                rows.addAll(toMasterRows(name, obj));
            } catch (Exception ex) {
                log.warn("读取 ledger 失败: {} -> {}", name, ex.getMessage());
            }
        }
        resp.setRows(rows);
        resp.setEmpty(rows.isEmpty());
        return resp;
    }

    private List<MasterLedgerRow> toMasterRows(String pdfName, JSONObject ledger) {
        List<MasterLedgerRow> result = new ArrayList<>();
        JSONArray records = ledger.getJSONArray("records");
        if (records == null || records.isEmpty()) return result; // 存量旧格式无 records，需重新识别后才会出现在总台账
        JSONObject cons = ledger.getJSONObject("consistency");
        int halluc = cons == null ? 0 : cons.getIntValue("hallucination_count", 0);
        Map<String, Boolean> foundByPath = indexFieldChecksByPath(cons == null ? null : cons.getJSONArray("field_checks"));
        String displayName = resolveDisplayName(pdfName);
        boolean concrete = "concrete".equals(ledgerMaterial(pdfName, ledger));
        for (int ri = 0; ri < records.size(); ri++) {
            JSONObject rec = records.getJSONObject(ri);
            JSONObject fields = rec == null ? null : rec.getJSONObject("fields");
            MasterLedgerRow row = new MasterLedgerRow();
            row.setPdfName(pdfName);
            row.setMaterialType(ledgerMaterial(pdfName, ledger));
            row.setDisplayName(displayName);
            row.setPageCount(ledger.getIntValue("page_count", 0));
            row.setLedgerRecognized(true);
            row.setHallucinationCount(halluc);
            row.setRecordIndex(ri);
            row.setDiagnostics(rec == null ? null : rec.get("diagnostics"));
            row.setMatchCandidates(rec == null ? null : rec.get("match_candidates"));
            row.setMatchStatus(rec == null ? null : rec.getString("match_status"));
            JSONObject approval = ledger.getJSONObject("export_review");
            row.setReviewConfirmed(approval != null && ledger.getString("revision") != null
                    && ledger.getString("revision").equals(approval.getString("revision")));
            Map<String, MasterLedgerCell> cells = new LinkedHashMap<>();
            for (String col : concrete ? ConcreteLedgerRules.COLUMNS : MASTER_COLUMNS) {
                JSONObject it = concrete ? ConcreteLedgerRules.field(fields, col) : fields == null ? null : fields.getJSONObject(col);
                MasterLedgerCell cell = new MasterLedgerCell();
                if (it != null) {
                    cell.setValue(it.getString("value"));
                    if (concrete && "验收数量".equals(col))
                        cell.setUnit("m3");
                    cell.setPolygon(it.get("polygon"));
                    cell.setEvidence(it.get("evidence"));
                    cell.setDiagnostics(it.get("diagnostics"));
                    cell.setStatus(it.getString("status"));
                    int pg = it.getIntValue("page", -1);
                    cell.setPage(pg > 0 ? pg : null);
                }
                Boolean found = concrete ? Boolean.valueOf(it != null && it.getBooleanValue("found_in_ocr", false))
                        : foundByPath.get("records[" + ri + "]." + col);
                cell.setFoundInOcr(found);
                cells.put(col, cell);
            }
            row.setCells(cells);
            result.add(row);
        }
        return result;
    }

    private String ledgerMaterial(String pdfName, JSONObject ledger) {
        String type = ledger.getString("material_type");
        return type == null ? materialType(pdfName) : type;
    }


    /** 优先用 history 中的原始文件名；无记录或为空时回退为对 ID 的去后缀名。 */
    private String resolveDisplayName(String pdfName) {
        try {
            HistoryEntity h = historyRepository.findByPdfName(pdfName).orElse(null);
            if (h != null && StringUtils.isNotBlank(h.getOriginalName())) {
                return h.getOriginalName();
            }
        } catch (Exception ignored) {
        }
        return PdfNameUtils.deriveName(pdfName);
    }

    /** 把 field_checks 索引成 path → found_in_ocr。 */
    private static Map<String, Boolean> indexFieldChecksByPath(JSONArray checks) {
        Map<String, Boolean> map = new HashMap<>();
        if (checks == null) return map;
        for (int i = 0; i < checks.size(); i++) {
            JSONObject c = checks.getJSONObject(i);
            if (c == null) continue;
            String p = c.getString("path");
            if (p == null) continue;
            map.put(p, c.getBooleanValue("found_in_ocr"));
        }
        return map;
    }

    /**
     * 将总台账导出为 CSV 或 XLSX 二进制。
     * format: csv | xlsx（不引入 Apache POI；xlsx 用最简手写 ZIP 实现）。
     */
    public synchronized byte[] exportMasterLedger(String format) {
        return exportMasterLedger(format, "rebar");
    }

    public synchronized byte[] exportMasterLedger(String format, String materialType) {
        MasterLedgerResponse data = getMasterLedger(materialType);
        for (MasterLedgerRow row : data.getRows()) {
            if (!row.isReviewConfirmed()) throw new BizException(409, "请先在单卷台账核验并确认导出：" + row.getPdfName());
        }
        return renderLedgerExport(data.getRows(), data.getColumns(), format);
    }

    /** The only human approval stage: final fields immediately before export. */
    public synchronized JSONObject confirmExportReview(String pdfName, String revision, String note) {
        JSONObject ledger = getLedger(pdfName);
        if (revision == null || !revision.equals(ledger.getString("revision")))
            throw new BizException(409, "台账版本已变化，请刷新并重新核验");
        JSONArray records = ledger.getJSONArray("records");
        if (records == null || records.isEmpty()) throw new BizException(409, "没有可核验记录");
        if ("concrete".equals(ledger.getString("material_type"))) {
            ConcreteLedgerRules.validateForExport(ledger);
        } else {
        List<String> required = Arrays.asList("材料名称", "规格品种", "生产单位", "供应单位",
                "供货日期与工程材料报审签认日期", "使用部位", "炉批号", "验收数量", "质保书编号");
        for (int i = 0; i < records.size(); i++) {
            JSONObject fields = records.getJSONObject(i).getJSONObject("fields");
            for (String key : required) {
                JSONObject cell = fields == null ? null : fields.getJSONObject(key);
                if (cell == null || StringUtils.isBlank(cell.getString("value")))
                    throw new BizException(409, "第" + (i+1) + "行「" + key + "」未补全，请双击单元格编辑");
            }
            try {
                java.math.BigDecimal quantity = new java.math.BigDecimal(fields.getJSONObject("验收数量").getString("value"));
                if (quantity.signum() <= 0) throw new NumberFormatException();
            } catch (NumberFormatException e) {
                throw new BizException(400, "验收数量须为大于零的吨数");
            }
        }
        }
        JSONObject review = new JSONObject();
        review.put("revision", revision);
        review.put("confirmed_at", java.time.Instant.now().toString());
        review.put("note", note == null ? "" : note);
        ledger.put("export_review", review);
        try { writeFile(PathUtils.ledgerJson(pdfName), JSON.toJSONString(ledger)); }
        catch (IOException e) { throw new BizException("保存导出核验失败: " + e.getMessage()); }
        return review;
    }

    /** 导出单卷台账（与总台账同款行列结构）：format = csv | xlsx。 */
    public synchronized byte[] exportPdfLedger(String pdfName, String format) {
        MasterLedgerResponse data = getPdfLedgerRows(pdfName);
        if (data.getRows().isEmpty()) throw new BizException(409, "没有可导出记录");
        for (MasterLedgerRow row : data.getRows()) {
            if (!row.isReviewConfirmed()) throw new BizException(409, "请先核验本卷关键字段并确认导出");
        }
        return renderLedgerExport(data.getRows(), data.getColumns(), format);
    }

    /** 单卷台账行视图：与总台账返回结构一致（columns + rows + empty），供前端以总台账样式渲染。 */
    public MasterLedgerResponse getPdfLedgerRows(String pdfName) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在: " + pdfName);
        Path p = PathUtils.ledgerJson(pdfName);
        if (!Files.exists(p)) throw new BizException(404, "该卷未生成台账");
        JSONObject ledger;
        try {
            ledger = JSON.parseObject(new String(Files.readAllBytes(p), ENC));
        } catch (IOException e) {
            throw new BizException("读取台账失败: " + e.getMessage());
        }
        MasterLedgerResponse resp = new MasterLedgerResponse();
        resp.setColumns("concrete".equals(ledgerMaterial(pdfName, ledger)) ? ConcreteLedgerRules.COLUMNS : MASTER_COLUMNS);
        resp.setRevision(ledger.getString("revision"));
        resp.setDiagnostics(ledger.get("diagnostics"));
        List<MasterLedgerRow> rows = toMasterRows(pdfName, ledger);
        resp.setRows(rows);
        resp.setEmpty(rows.isEmpty());
        return resp;
    }

    /** 统一的台账表格渲染入口：csv | xlsx。 */
    private byte[] renderLedgerExport(List<MasterLedgerRow> rows, List<String> columns, String format) {
        if ("csv".equalsIgnoreCase(format)) {
            return renderCsv(rows, columns);
        }
        return renderXlsx(rows, columns);
    }

    private byte[] renderCsv(List<MasterLedgerRow> rows, List<String> columns) {
        StringBuilder sb = new StringBuilder();
        sb.append("\uFEFF"); // UTF-8 BOM，Excel 打开中文不乱码
        sb.append("PDF,");
        for (int i = 0; i < columns.size(); i++) {
            sb.append(escapeCsv(columns.get(i)));
            sb.append(i == columns.size() - 1 ? "\n" : ",");
        }
        for (MasterLedgerRow row : rows) {
            String pdfLabel = nullToEmpty(row.getDisplayName())
                    + (row.getRecordIndex() == null ? "" : (" #" + (row.getRecordIndex() + 1)));
            sb.append(escapeCsv(pdfLabel));
            sb.append(",");
            for (int i = 0; i < columns.size(); i++) {
                String col = columns.get(i);
                MasterLedgerCell cell = row.getCells().get(col);
                sb.append(escapeCsv(joinCell(cell)));
                sb.append(i == columns.size() - 1 ? "\n" : ",");
            }
        }
        return sb.toString().getBytes(StandardCharsets.UTF_8);
    }

    private static String joinCell(MasterLedgerCell cell) {
        if (cell == null) return "";
        String v = exportCellValue(cell);
        String p = cell.getPage() == null ? "" : ("p" + cell.getPage());
        if (v.isEmpty() && p.isEmpty()) return "";
        return v + (p.isEmpty() ? "" : " (" + p + ")");
    }

    private static String exportCellValue(MasterLedgerCell cell) {
        if (cell == null) return "";
        String value = nullToEmpty(cell.getValue());
        return value + (!value.isEmpty() && "m3".equals(cell.getUnit()) ? " m³" : "");
    }

    private static String escapeCsv(String s) {
        if (s == null) return "";
        boolean needQuote = s.indexOf(',') >= 0 || s.indexOf('"') >= 0 || s.indexOf('\n') >= 0 || s.indexOf('\r') >= 0;
        String escaped = s.replace("\"", "\"\"");
        return needQuote ? ("\"" + escaped + "\"") : escaped;
    }

    private static String nullToEmpty(String s) { return s == null ? "" : s; }

    /**
     * XLSX：台账及字段溯源（保留OCR诊断与坐标）。
     * 结构：ZIP = [Content_Types].xml + _rels/.rels + xl/workbook.xml + xl/worksheets/sheet1.xml
     */
    private byte[] renderXlsx(List<MasterLedgerRow> rows, List<String> columns) {
        try (java.io.ByteArrayOutputStream baos = new java.io.ByteArrayOutputStream();
             java.util.zip.ZipOutputStream zip = new java.util.zip.ZipOutputStream(baos)) {
            zip.putNextEntry(new java.util.zip.ZipEntry("[Content_Types].xml"));
            zip.write(("<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
                    + "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
                    + "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
                    + "<Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
                    + "<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
                    + "<Override PartName=\"/xl/worksheets/sheet2.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
                    + "</Types>").getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();

            zip.putNextEntry(new java.util.zip.ZipEntry("_rels/.rels"));
            zip.write(("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                    + "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
                    + "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/>"
                    + "</Relationships>").getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();

                        // workbook.xml 引用的 rId1 在此映射到 sheet1.xml
            zip.putNextEntry(new java.util.zip.ZipEntry("xl/_rels/workbook.xml.rels"));
            zip.write(("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                    + "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
                    + "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/>"
                    + "<Relationship Id=\"rId2\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet2.xml\"/>"
                    + "</Relationships>").getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();

            zip.putNextEntry(new java.util.zip.ZipEntry("xl/workbook.xml"));
            zip.write(("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                    + "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
                    + "<sheets><sheet name=\"台账\" sheetId=\"1\" r:id=\"rId1\"/><sheet name=\"字段溯源\" sheetId=\"2\" r:id=\"rId2\"/></sheets>"
                    + "</workbook>").getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();

            StringBuilder sh = new StringBuilder();
            sh.append("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>");
            sh.append("<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">");
            sh.append(buildXlsxCols(rows, columns));
            sh.append("<sheetData>");
            int rowIdx = 1;
            sh.append("<row r=\"").append(rowIdx++).append("\">");
            sh.append(cell("A1", "PDF", 1));
            for (int i = 0; i < columns.size(); i++) {
                sh.append(cell(colLetter(i + 2) + "1", columns.get(i), 1));
            }
            sh.append("</row>");
            for (MasterLedgerRow row : rows) {
                String pdfLabel = nullToEmpty(row.getDisplayName())
                        + (row.getRecordIndex() == null ? "" : (" #" + (row.getRecordIndex() + 1)));
                sh.append("<row r=\"").append(rowIdx).append("\">");
                sh.append(cell("A" + rowIdx, pdfLabel, 1));
                for (int i = 0; i < columns.size(); i++) {
                    String col = columns.get(i);
                    MasterLedgerCell cell = row.getCells().get(col);
                    sh.append(cell(colLetter(i + 2) + rowIdx, exportCellValue(cell), 1));
                }
                sh.append("</row>");
                rowIdx++;
            }
            sh.append("</sheetData></worksheet>");
            zip.putNextEntry(new java.util.zip.ZipEntry("xl/worksheets/sheet1.xml"));
            zip.write(sh.toString().getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();

            StringBuilder audit = new StringBuilder("<?xml version=\"1.0\" encoding=\"UTF-8\"?><worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData>");
            int ar=1;
            audit.append("<row r=\"1\">");
            String[] headers={"PDF","记录行","字段","最终值","来源页","来源证据与坐标","字段诊断","匹配诊断","核验状态"};
            for(int i=0;i<headers.length;i++)audit.append(cell(colLetter(i+1)+ar,headers[i],1));
            audit.append("</row>");
            for(MasterLedgerRow row:rows)for(String col:columns){
                MasterLedgerCell f=row.getCells().get(col);ar++;
                String[] vals={row.getDisplayName(),String.valueOf(row.getRecordIndex()+1),col,f==null?"":f.getValue(),
                    f==null||f.getPage()==null?"":("p"+f.getPage()),f==null?"":JSON.toJSONString(f.getEvidence()),
                    f==null?"":JSON.toJSONString(f.getDiagnostics()),JSON.toJSONString(row.getDiagnostics()),row.isReviewConfirmed()?"已核验":"未核验"};
                audit.append("<row r=\"").append(ar).append("\">");
                for(int i=0;i<vals.length;i++)audit.append(cell(colLetter(i+1)+ar,nullToEmpty(vals[i]),1));
                audit.append("</row>");
            }
            audit.append("</sheetData></worksheet>");
            zip.putNextEntry(new java.util.zip.ZipEntry("xl/worksheets/sheet2.xml"));
            zip.write(audit.toString().getBytes(StandardCharsets.UTF_8));zip.closeEntry();
            zip.finish();
            return baos.toByteArray();
        } catch (java.io.IOException e) {
            throw new BizException("生成 xlsx 失败: " + e.getMessage());
        }
    }

    /**
     * 按内容自适应计算各列宽（ASCII 记 1 字符、中文/全角记 2 字符），
     * 受 min/max 约束，返回 <cols> 片段。宽度值保留 1 位小数。
     */
    private static String buildXlsxCols(List<MasterLedgerRow> rows, List<String> columns) {
        int colCount = columns.size() + 1; // +1 为 PDF 列
        double[] widths = new double[colCount];
        widths[0] = 14.0;
        for (int i = 0; i < columns.size(); i++) {
            widths[i + 1] = displayWidth(columns.get(i));
        }
        for (MasterLedgerRow row : rows) {
            String pdfLabel = nullToEmpty(row.getDisplayName())
                    + (row.getRecordIndex() == null ? "" : (" #" + (row.getRecordIndex() + 1)));
            widths[0] = Math.max(widths[0], displayWidth(pdfLabel));
            for (int i = 0; i < columns.size(); i++) {
                String col = columns.get(i);
                widths[i + 1] = Math.max(widths[i + 1], displayWidth(joinCell(row.getCells().get(col))));
            }
        }
        double min = 10.0, max = 60.0, pad = 2.5;
        StringBuilder sb = new StringBuilder("<cols>");
        for (int i = 0; i < colCount; i++) {
            double w = Math.min(max, Math.max(min, widths[i] + pad));
            sb.append("<col min=\"").append(i + 1).append("\" max=\"").append(i + 1)
              .append("\" width=\"").append(String.format(java.util.Locale.ROOT, "%.1f", w))
              .append("\" customWidth=\"1\"/>");
        }
        return sb.append("</cols>").toString();
    }

    /** 文本显示宽度：ASCII/拉丁记 1 字符，中文/全角等按 2 字符估算。 */
    private static double displayWidth(String s) {
        if (s == null || s.isEmpty()) return 0;
        double w = 0;
        for (int i = 0; i < s.length(); i++) {
            w += s.charAt(i) <= 0xFF ? 1.0 : 2.0;
        }
        return w;
    }

    private static String colLetter(int idx) {
        StringBuilder s = new StringBuilder();
        while (idx > 0) {
            idx--;
            s.insert(0, (char) ('A' + idx % 26));
            idx /= 26;
        }
        return s.toString();
    }

    private static String cell(String ref, String text, int style) {
        StringBuilder sb = new StringBuilder();
        sb.append("<c r=\"").append(ref).append("\"");
        if (style > 0) sb.append(" s=\"").append(style).append("\"");
        sb.append(" t=\"inlineStr\"><is><t xml:space=\"preserve\">");
        sb.append(xmlEscape(text));
        sb.append("</t></is></c>");
        return sb.toString();
    }

    private static String xmlEscape(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    /**
     * 修改单条记录字段值（新版 records 结构）：
     * 写 ledger.json 的 records[recordIndex].fields[field].value，并同步 consistency 与 history。
     */
    @Transactional
    public synchronized void updateMasterField(String pdfName, int recordIndex, String fieldName, String newValue) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在: " + pdfName);
        Path p = PathUtils.ledgerJson(pdfName);
        if (!Files.exists(p)) throw new BizException(404, "PDF 未识别,无法编辑: " + pdfName);
        JSONObject obj;
        try {
            obj = JSON.parseObject(new String(Files.readAllBytes(p), ENC));
        } catch (IOException e) {
            throw new BizException("读取 ledger 失败: " + e.getMessage());
        }
        List<String> columns = "concrete".equals(ledgerMaterial(pdfName, obj)) ? ConcreteLedgerRules.COLUMNS : MASTER_COLUMNS;
        if (!columns.contains(fieldName)) throw new BizException(400, "未知字段: " + fieldName);
        JSONArray records = obj.getJSONArray("records");
        if (records == null || records.isEmpty()) throw new BizException(400, "台账无 records 数据");
        if (recordIndex < 0 || recordIndex >= records.size()) throw new BizException(400, "record_index 越界: " + recordIndex);
        JSONObject rec = records.getJSONObject(recordIndex);
        if (rec == null) throw new BizException(400, "记录为空: " + recordIndex);
        JSONObject fields = rec.getJSONObject("fields");
        if (fields == null) {
            fields = new JSONObject();
            rec.put("fields", fields);
        }
        JSONObject it = "concrete".equals(ledgerMaterial(pdfName, obj)) ? ConcreteLedgerRules.field(fields, fieldName) : fields.getJSONObject(fieldName);
        if (it == null) {
            it = new JSONObject();
            fields.put(fieldName, it);
        }
        it.put("original_value", it.getOrDefault("original_value", it.get("value")));
        it.put("value", newValue == null ? "" : newValue);
        it.put("human_edited", true);
        obj.put("revision", java.util.UUID.randomUUID().toString());
        obj.remove("export_review");

        // 同步一致性表：手动修正视为已核对，并重算幻觉数
        JSONObject cons = obj.getJSONObject("consistency");
        final String path = "records[" + recordIndex + "]." + fieldName;
        if (cons != null) {
            JSONArray checks = cons.getJSONArray("field_checks");
            if (checks != null) {
                for (int i = 0; i < checks.size(); i++) {
                    JSONObject c = checks.getJSONObject(i);
                    if (c != null && path.equals(c.getString("path"))) {
                        c.put("value", newValue == null ? "" : newValue);
                        c.put("human_edited", true); // OCR observations remain immutable.
                    }
                }
                int hallu = 0;
                for (int i = 0; i < checks.size(); i++) {
                    JSONObject c = checks.getJSONObject(i);
                    if (c != null && !c.getBooleanValue("found_in_ocr")) hallu++;
                }
                cons.put("hallucination_count", hallu);
            }
        }

        try {
            writeFile(p, JSON.toJSONString(obj));
        } catch (IOException e) {
            throw new BizException("写入 ledger 失败: " + e.getMessage());
        }

        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            try {
                h.setLastResultJson(JSON.toJSONString(obj));
                if (cons != null) h.setLedgerHallucinationCount(cons.getIntValue("hallucination_count", 0));
            } catch (Exception ignored) {
            }
        });
    }

    /**
     * 清空所有 PDF 的台账（保留 OCR 缓存与图片），并同步 history 状态。
     * 返回 { cleared_pdfs, cleared_pdf_count }。
     */
    @Transactional
    public synchronized Map<String, Object> clearAllLedgers() {
        List<String> cleared = new ArrayList<>();
        Path root = PathUtils.libraryRoot();
        if (Files.exists(root)) {
            try (Stream<Path> dirs = Files.list(root)) {
                dirs.filter(Files::isDirectory).forEach(dir -> {
                    String name = dir.getFileName().toString();
                    Path ledger = PathUtils.ledgerJson(name);
                    if (Files.exists(ledger)) {
                        ocrClient.clearLibraryCache(name, "ledger");
                        deleteFile(ledger);
                        cleared.add(resolveDisplayName(name));
                        historyRepository.findByPdfName(name).ifPresent(h -> {
                            h.setLedgerRecognized(false);
                            h.setLedgerHallucinationCount(null);
                            h.setLastSummary(null);
                            h.setLastResultJson(null);
                            h.setLastStatus(LibraryStatus.READY.name());
                            h.setUpdatedAt(LocalDateTime.now());
                            historyRepository.save(h);
                        });
                    }
                });
            } catch (IOException e) {
                throw new BizException("清空台账失败: " + e.getMessage());
            }
        }
        Map<String, Object> data = new HashMap<>();
        data.put("cleared_pdfs", cleared);
        data.put("cleared_pdf_count", cleared.size());
        return data;
    }

    @Transactional
    public synchronized void clearCache(String pdfName, String scope) {
        if (!libraryExists(pdfName)) throw new BizException(404, "PDF 不存在");
        if (!"all".equalsIgnoreCase(scope) && !"ledger".equalsIgnoreCase(scope)) throw new BizException("未知缓存范围");
        ocrClient.clearLibraryCache(pdfName, scope.toLowerCase(java.util.Locale.ROOT));
        if ("all".equalsIgnoreCase(scope)) {
            deleteDir(PathUtils.ocrDir(pdfName));
            deleteFile(PathUtils.ledgerJson(pdfName));
        } else if ("ledger".equalsIgnoreCase(scope)) {
            deleteFile(PathUtils.ledgerJson(pdfName));
        } else {
            throw new BizException("未知 scope: " + scope);
        }
        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            h.setLedgerRecognized(false);
            h.setLedgerHallucinationCount(null);
            h.setLastSummary(null);
            h.setLastError(null);
            h.setLastResultJson(null);
            h.setLastStatus(LibraryStatus.READY.name());
            h.setUpdatedAt(LocalDateTime.now());
            historyRepository.save(h);
        });
    }

    @Transactional
    public void delete(String pdfName) {
        Path dir = PathUtils.libraryDir(pdfName);
        if (Files.exists(dir)) deleteDir(dir);
        historyRepository.findByPdfName(pdfName).ifPresent(historyRepository::delete);
    }

    public HistoryEntity getHistoryRow(String pdfName) {
        return historyRepository.findByPdfName(pdfName).orElse(null);
    }

    public List<HistoryEntity> historyList() {
        return historyRepository.findAllByOrderByUpdatedAtDesc();
    }

    public boolean libraryExists(String pdfName) {
        return Files.exists(PathUtils.libraryDir(pdfName));
    }

    private void syncRemotePageOcr(String pdfName, int[] pages, JSONObject ledger) {
        int ok = 0;
        try {
            Files.createDirectories(PathUtils.ocrDir(pdfName));
        } catch (IOException e) {
            log.warn("创建 OCR 缓存目录失败: {} -> {}", pdfName, e.getMessage());
            return;
        }
        for (int p : pages) {
            try {
                JSONObject ocr = ocrClient.getLibraryOcr(pdfName, p);
                if (StringUtils.isNotBlank(ocr.getString("error"))) {
                    log.warn("同步远端 OCR 缓存跳过（远端返回错误）: {}#{}, {}", pdfName, p, ocr.getString("error"));
                    continue;
                }
                injectPageFields(ocr, ledger, p);
                writeFile(PathUtils.pageOcrJson(pdfName, p), JSON.toJSONString(ocr));
                ok++;
            } catch (Exception e) {
                log.warn("同步远端 OCR 缓存失败: {}#{}, {}", pdfName, p, e.getMessage());
            }
        }
        final int ocrCount = ok;
        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            h.setOcrCachedCount(ocrCount);
            h.setUpdatedAt(LocalDateTime.now());
            historyRepository.save(h);
        });
    }

    private void savePageOcrCache(String pdfName, int page, JSONObject resp) {
        if (resp == null) throw new BizException("OCR 返回为空");
        if (StringUtils.isNotBlank(resp.getString("error"))) {
            throw new BizException("OCR 识别失败: " + resp.getString("error"));
        }
        try {
            Files.createDirectories(PathUtils.ocrDir(pdfName));
            writeFile(PathUtils.pageOcrJson(pdfName, page), JSON.toJSONString(resp));
        } catch (IOException e) {
            throw new BizException("OCR 缓存写入失败: " + e.getMessage());
        }
        historyRepository.findByPdfName(pdfName).ifPresent(h -> {
            int cached = countOcrCached(pdfName);
            h.setOcrCachedCount(cached);
            h.setUpdatedAt(LocalDateTime.now());
            String status = Files.exists(PathUtils.ledgerJson(pdfName))
                    ? LibraryStatus.SUCCESS.name()
                    : (hasAnyFailed(pdfName) ? LibraryStatus.FAILED.name() : LibraryStatus.PROCESSING.name());
            h.setLastStatus(status);
            historyRepository.save(h);
        });
    }

    private void saveLedgerCache(String pdfName, JSONObject ledger) {
        try {
            writeFile(PathUtils.ledgerJson(pdfName), JSON.toJSONString(ledger));
        } catch (IOException e) {
            throw new BizException("台账写入失败: " + e.getMessage());
        }
        HistoryEntity h = historyRepository.findByPdfName(pdfName).orElseGet(HistoryEntity::new);
        if (h.getId() == null) {
            h.setPdfName(pdfName);
            h.setCreatedAt(LocalDateTime.now());
        }
        if (StringUtils.isBlank(h.getOriginalName())) h.setOriginalName(pdfName);
        if (h.getPageCount() == null || h.getPageCount() == 0) h.setPageCount(listPagesOnDisk(pdfName).length);
        h.setLedgerRecognized(true);
        h.setLastStatus(LibraryStatus.SUCCESS.name());
        h.setLastResultJson(JSON.toJSONString(ledger));
        h.setUpdatedAt(LocalDateTime.now());
        if (ledger != null) {
            String summary = ledger.getString("summary");
            if (summary != null) h.setLastSummary(summary);
            JSONObject cons = ledger.getJSONObject("consistency");
            if (cons != null) {
                Object hallu = cons.get("hallucination_count");
                if (hallu instanceof Number) {
                    h.setLedgerHallucinationCount(((Number) hallu).intValue());
                }
            }
        }
        historyRepository.save(h);
    }

    private JSONObject assembleLedger(String pdfName, int[] pages) {
        JSONObject ledger = new JSONObject();
        ledger.put("pdf_name", pdfName);
        ledger.put("page_count", pages.length);
        ledger.put("ts_utc", LocalDateTime.now().toString());

        Map<Integer, String> pageTextNorm = new HashMap<>();
        Map<Integer, JSONObject> pageRespCache = new HashMap<>();
        for (int p : pages) {
            Path ocr = PathUtils.pageOcrJson(pdfName, p);
            if (!Files.exists(ocr)) continue;
            try {
                JSONObject pageResp = JSON.parseObject(new String(Files.readAllBytes(ocr), ENC));
                pageRespCache.put(p, pageResp);
                String text = pageResp.getString("text");
                pageTextNorm.put(p, normalizeForMatch(text));
            } catch (IOException ignored) {
            }
        }

        JSONObject fields = new JSONObject();
        JSONArray fieldChecks = new JSONArray();
        int fieldsTotal = 0;
        Map<String, Integer> perName = new LinkedHashMap<>();
        for (int p : pages) {
            JSONObject pageResp = pageRespCache.get(p);
            if (pageResp == null) continue;
            JSONArray arr = pageResp.getJSONArray("fields");
            if (arr == null) continue;
            for (int i = 0; i < arr.size(); i++) {
                JSONObject item = arr.getJSONObject(i);
                if (item == null) continue;
                String name = item.getString("name");
                String value = item.getString("value");
                if (name == null || name.isEmpty()) continue;
                JSONArray list = fields.getJSONArray(name);
                if (list == null) {
                    list = new JSONArray();
                    fields.put(name, list);
                }
                int idx = list.size();
                JSONObject entry = new JSONObject();
                entry.put("value", value);
                entry.put("page", p);
                list.add(entry);
                fieldsTotal++;
                perName.merge(name, 1, Integer::sum);

                String norm = pageTextNorm.getOrDefault(p, "");
                boolean found = matchesText(value, norm);
                JSONObject check = new JSONObject();
                check.put("path", name + "[" + idx + "]");
                check.put("name", name);
                check.put("value", value);
                check.put("page", p);
                check.put("found_in_ocr", found);
                fieldChecks.add(check);
            }
        }

        int hallu = 0;
        for (int i = 0; i < fieldChecks.size(); i++) {
            if (!fieldChecks.getJSONObject(i).getBooleanValue("found_in_ocr")) hallu++;
        }
        ledger.put("ledger", fields);

        JSONObject consistency = new JSONObject();
        consistency.put("field_count", fieldsTotal);
        consistency.put("hallucination_count", hallu);
        consistency.put("fields_per_name", perName);
        consistency.put("field_checks", fieldChecks);
        ledger.put("consistency", consistency);

        StringBuilder summary = new StringBuilder();
        summary.append("已生成 ").append(fieldsTotal).append(" 条记录 · ");
        summary.append(fields.size()).append(" 个字段");
        if (hallu > 0) summary.append(" · ⚠ ").append(hallu).append(" 处疑似幻觉");
        ledger.put("summary", summary.toString());
        return ledger;
    }

    private static String normalizeForMatch(String s) {
        if (s == null) return "";
        return s.replaceAll("\\s+", "").toLowerCase();
    }

    private static boolean matchesText(String value, String textNormed) {
        if (value == null || value.isEmpty() || textNormed == null || textNormed.isEmpty()) return false;
        String v = normalizeForMatch(value);
        if (v.isEmpty()) return false;
        if (textNormed.contains(v)) return true;
        String[] parts = v.split("[^\\p{L}\\p{N}]+");
        for (String part : parts) {
            if (part.length() >= 2 && textNormed.contains(part)) return true;
        }
        return false;
    }

    private boolean hasAnyFailed(String pdfName) {
        for (int p : listPagesOnDisk(pdfName)) {
            Path ocr = PathUtils.pageOcrJson(pdfName, p);
            if (!Files.exists(ocr)) continue;
            try {
                JSONObject j = JSON.parseObject(new String(Files.readAllBytes(ocr), ENC));
                if (hasNonEmptyError(j)) return true;
            } catch (Exception ignored) {
            }
        }
        return false;
    }

    private boolean hasNonEmptyError(JSONObject pageResp) {
        if (pageResp == null) return true;
        Object err = pageResp.get("error");
        if (err != null && !"null".equalsIgnoreCase(err.toString()) && !err.toString().isEmpty()) return true;
        Object cls = pageResp.get("classify");
        if (cls instanceof JSONObject) {
            Object e = ((JSONObject) cls).get("error");
            if (e != null && !"null".equalsIgnoreCase(e.toString()) && !e.toString().isEmpty()) return true;
        }
        return false;
    }

    private int[] listPagesOnDisk(String pdfName) {
        Path dir = PathUtils.libraryDir(pdfName);
        if (!Files.exists(dir)) return new int[0];
        try (Stream<Path> s = Files.list(dir)) {
            return s
                    .filter(p -> p.getFileName().toString().matches("page_\\d{4}\\.png"))
                    .map(p -> Integer.parseInt(p.getFileName().toString().substring(5, 9)))
                    .sorted()
                    .mapToInt(Integer::intValue)
                    .toArray();
        } catch (IOException e) {
            return new int[0];
        }
    }

    private int countOcrCached(String pdfName) {
        Path dir = PathUtils.ocrDir(pdfName);
        if (!Files.exists(dir)) return 0;
        try (Stream<Path> s = Files.list(dir)) {
            return (int) s.filter(p -> p.getFileName().toString().endsWith(".ocr.json") && isValidOcrCache(p)).count();
        } catch (IOException e) {
            return 0;
        }
    }

    private int[] readPngDimensions(Path image) {
        try (InputStream in = Files.newInputStream(image)) {
            BufferedImage bi = ImageIO.read(in);
            if (bi == null) return new int[]{0, 0};
            return new int[]{bi.getWidth(), bi.getHeight()};
        } catch (Exception e) {
            return new int[]{0, 0};
        }
    }

    private void writeFile(Path p, String content) throws IOException {
        Files.write(p, content.getBytes(StandardCharsets.UTF_8));
    }

    private void deleteFile(Path p) {
        try {
            Files.deleteIfExists(p);
        } catch (IOException ignored) {
        }
    }

    private void deleteDir(Path p) {
        if (!Files.exists(p)) return;
        try {
            Files.walkFileTree(p, new SimpleFileVisitor<Path>() {
                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                    Files.delete(file);
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult postVisitDirectory(Path dir, IOException exc) throws IOException {
                    Files.delete(dir);
                    return FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException e) {
            throw new BizException("清理失败: " + e.getMessage());
        }
    }

    @Data
    public static class UploadResult {
        private String pdfName;
        private String materialType = "rebar";
        private String originalName;
        private int pageCount;
        private Integer dpi;
        private String format;
        private String sourcePdf;
        private Boolean overwritten;
    }

    @Data
    public static class ImportResult {
        private int imported;
        private int skipped;
        private int errors;
        private String fatalError;
        private List<Item> results = new ArrayList<>();

        @Data
        public static class Item {
            private String name;
            private String status;       // ok | partial | skipped | error
            private String message;
            private int pages;
            private int imageCount;
            private int ocrCount;
            private boolean ledgerRecognized;
            private int ledgerHallucination;
        }
    }
}
