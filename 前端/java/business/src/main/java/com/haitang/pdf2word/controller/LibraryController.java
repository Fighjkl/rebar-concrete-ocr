package com.haitang.pdf2word.controller;

import com.alibaba.fastjson2.JSON;
import com.alibaba.fastjson2.JSONArray;
import com.alibaba.fastjson2.JSONObject;
import com.haitang.pdf2word.db.HistoryEntity;
import com.haitang.pdf2word.model.HistoryView;
import com.haitang.pdf2word.other.ApiResponse;
import com.haitang.pdf2word.exception.BizException;
import com.haitang.pdf2word.service.LibraryService;
import com.haitang.pdf2word.service.LedgerCompareService;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

@RestController
@RequestMapping("/api/library")
@RequiredArgsConstructor
public class LibraryController {

    private final LibraryService libraryService;
    private final LedgerCompareService ledgerCompareService;

    @GetMapping
    public Map<String, Object> list() {
        return ApiResponse.ok(libraryService.list());
    }

    @GetMapping("/ocr-models")
    public Map<String, Object> ocrModels() {
        return ApiResponse.ok(libraryService.ocrModels());
    }

    @PostMapping("/import-remote")
    public Map<String, Object> importRemote(javax.servlet.http.HttpServletRequest request,
                                            @org.springframework.web.bind.annotation.RequestParam(value = "names", required = false) java.util.List<String> namesParam,
                                            @RequestBody(required = false) String rawBody) {
        java.util.List<String> names = namesParam;
        if ((names == null || names.isEmpty()) && rawBody != null && !rawBody.isEmpty()) {
            try {
                JSONObject o = JSON.parseObject(rawBody);
                JSONArray arr = o == null ? null : o.getJSONArray("names");
                if (arr != null) {
                    names = new java.util.ArrayList<>();
                    for (int i = 0; i < arr.size(); i++) names.add(arr.getString(i));
                }
            } catch (Exception ignored) {
            }
        }
        return ApiResponse.ok(libraryService.importFromRemote(names));
    }

    @PostMapping(value = "/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Map<String, Object> upload(@RequestPart("file") MultipartFile file,
                                      @RequestParam(value = "name", required = false) String name,
                                      @RequestParam(value = "dpi", required = false) Integer dpi,
                                      @RequestParam(value = "image_format", required = false) String imageFormat,
                                      @RequestParam(value = "overwrite", required = false) Boolean overwrite,
                                      @RequestParam(value = "material_type", required = false) String materialType) {
        if (!"rebar".equals(materialType) && !"concrete".equals(materialType) && !"steel_beam".equals(materialType))
            return ApiResponse.error(422, "请先选择 PDF 材料类别");
        LibraryService.UploadResult r = libraryService.uploadPdf(file, dpi, imageFormat, overwrite, name, materialType);
        Map<String, Object> data = new HashMap<>();
        data.put("name", r.getPdfName());
        data.put("pdfName", r.getPdfName());
        data.put("originalName", r.getOriginalName());
        data.put("pageCount", r.getPageCount());
        data.put("dpi", r.getDpi());
        data.put("format", r.getFormat());
        data.put("sourcePdf", r.getSourcePdf());
        data.put("overwritten", r.getOverwritten());
        data.put("materialType", r.getMaterialType());
        return ApiResponse.ok(data);
    }

    @PostMapping(value = "/{pdfName}/resplit", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Map<String, Object> resplit(@PathVariable String pdfName,
                                       @RequestParam(value = "dpi", required = false) Integer dpi,
                                       @RequestParam(value = "image_format", required = false) String imageFormat) {
        return ApiResponse.ok(libraryService.resplitPdf(pdfName, dpi, imageFormat));
    }

    @PostMapping(value = "/{pdfName}/pages/{page}/rotate",
            consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Map<String, Object> rotate(@PathVariable String pdfName,
                                      @PathVariable int page,
                                      @RequestParam(value = "angle", required = false) Integer angle) {
        return ApiResponse.ok(libraryService.rotatePage(pdfName, page, angle));
    }

    @GetMapping(value = "/{pdfName}/pages/{page}/image")
    public ResponseEntity<Resource> image(@PathVariable String pdfName,
                                          @PathVariable int page) {
        Path file = libraryService.pageImagePath(pdfName, page);
        Resource resource = new FileSystemResource(file);
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_TYPE, MediaType.IMAGE_PNG_VALUE)
                .header(HttpHeaders.CACHE_CONTROL, "no-cache")
                .body(resource);
    }

    @GetMapping(value = "/{pdfName}/pages/{page}/ocr")
    public Map<String, Object> getPageOcr(@PathVariable String pdfName,
                                          @PathVariable int page) {
        JSONObject body = libraryService.getPageOcr(pdfName, page);
        return ApiResponse.ok(body);
    }

    @PostMapping(value = "/{pdfName}/pages/{page}/ocr",
            consumes = MediaType.APPLICATION_FORM_URLENCODED_VALUE)
    public Map<String, Object> runPageOcr(@PathVariable String pdfName,
                                          @PathVariable int page,
                                          @RequestParam(value = "prompt", required = false) String prompt,
                                          @RequestParam(value = "fields", required = false) String fields,
                                          @RequestParam(value = "classify_prompt", required = false) String classifyPrompt,
                                          @RequestParam(value = "classify_schema", required = false) String classifySchema) {
        JSONObject body = libraryService.runPageOcr(pdfName, page, prompt, fields, classifyPrompt, classifySchema);
        return ApiResponse.ok(body);
    }

    @PostMapping(value = "/{pdfName}/recognize",
            consumes = MediaType.APPLICATION_FORM_URLENCODED_VALUE)
    public Map<String, Object> recognize(@PathVariable String pdfName,
                                          @RequestParam(value = "prompt", required = false) String prompt,
                                          @RequestParam(value = "fields", required = false) String fields,
                                          @RequestParam(value = "classify_prompt", required = false) String classifyPrompt,
                                          @RequestParam(value = "classify_schema", required = false) String classifySchema,
                                          @RequestParam(value = "ocr_prompt", required = false) String ocrPrompt,
                                          @RequestParam(value = "model", required = false) String model,
                                          @RequestParam(value = "force_ocr_refresh", required = false) Boolean forceOcrRefresh) {
        boolean force = Boolean.TRUE.equals(forceOcrRefresh);
        JSONObject body = libraryService.recognize(pdfName, prompt, fields, classifyPrompt, classifySchema,
                ocrPrompt, model, force);
        return ApiResponse.ok(body);
    }

    @PostMapping("/{pdfName}/ledger/review")
    public Map<String, Object> reviewLedger(@PathVariable String pdfName, @RequestBody Map<String, Object> body) {
        return ApiResponse.ok(libraryService.confirmExportReview(pdfName,
                (String) body.get("revision"), (String) body.get("note")));
    }

    @PostMapping(value = "/{pdfName}/recognition", consumes = MediaType.APPLICATION_FORM_URLENCODED_VALUE)
    public Map<String, Object> startRecognition(@PathVariable String pdfName,
                                                @RequestParam(value = "prompt", required = false) String prompt,
                                                @RequestParam(value = "ocr_prompt", required = false) String ocrPrompt,
                                                @RequestParam(value = "model", required = false) String model,
                                                @RequestParam(value = "force_ocr_refresh", required = false) Boolean force) {
        return ApiResponse.ok(libraryService.startRecognition(pdfName, prompt, ocrPrompt, model, Boolean.TRUE.equals(force)));
    }

    @GetMapping("/{pdfName}/recognition")
    public Map<String, Object> recognition(@PathVariable String pdfName) {
        return ApiResponse.ok(libraryService.getRecognition(pdfName));
    }

    @GetMapping("/{pdfName}/ledger")
    public Map<String, Object> ledger(@PathVariable String pdfName) {
        return ApiResponse.ok(libraryService.getLedger(pdfName));
    }

    /** 单卷台账行视图：与总台账相同结构（columns + rows），供单卷页以总台账样式渲染。 */
    @GetMapping("/{pdfName}/ledger/rows")
    public Map<String, Object> ledgerRows(@PathVariable String pdfName) {
        return ApiResponse.ok(libraryService.getPdfLedgerRows(pdfName));
    }

    /** 导出单卷台账。format=xlsx|csv，缺省 xlsx。 */
    @GetMapping("/{pdfName}/export")
    public ResponseEntity<Resource> exportPdf(@PathVariable String pdfName,
                                              @RequestParam(value = "format", defaultValue = "xlsx") String format) {
        String fmt = format == null || format.isEmpty() ? "xlsx" : format.toLowerCase();
        byte[] body = libraryService.exportPdfLedger(pdfName, fmt);
        String filename = pdfName + "-ledger-" + System.currentTimeMillis() + "." + fmt;
        MediaType mt;
        if ("csv".equals(fmt)) {
            mt = MediaType.parseMediaType("text/csv");
        } else if ("xlsx".equals(fmt)) {
            mt = MediaType.parseMediaType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
        } else {
            mt = MediaType.APPLICATION_OCTET_STREAM;
        }
        org.springframework.core.io.ByteArrayResource resource = new org.springframework.core.io.ByteArrayResource(body);
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + filename + "\"")
                .header(HttpHeaders.CONTENT_TYPE, mt.toString())
                .contentLength(body.length)
                .body(resource);
    }

    @DeleteMapping("/{pdfName}/cache")
    public Map<String, Object> clearCache(@PathVariable String pdfName,
                                          @RequestParam(value = "scope", defaultValue = "ledger") String scope) {
        libraryService.clearCache(pdfName, scope);
        return ApiResponse.ok("ok");
    }

    @DeleteMapping("/{pdfName}")
    public Map<String, Object> delete(@PathVariable String pdfName) {
        libraryService.delete(pdfName);
        return ApiResponse.ok("ok");
    }

    @DeleteMapping("/all")
    public Map<String, Object> clearAll() {
        libraryService.clearAll();
        return ApiResponse.ok("ok");
    }

    /** 导入人工识别 Excel 并与当前卷识别台账比对。 */
    @PostMapping(value = "/{pdfName}/ledger/compare", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Map<String, Object> compareLedger(@PathVariable String pdfName,
                                             @RequestPart("file") MultipartFile file) {
        return ApiResponse.ok(ledgerCompareService.compare(pdfName, file));
    }

    @GetMapping("/{pdfName}")
    public Map<String, Object> get(@PathVariable String pdfName) {
        HistoryEntity h = libraryService.getHistoryRow(pdfName);
        Map<String, Object> data = new HashMap<>();
        if (h == null) {
            data.put("pdfName", pdfName);
            data.put("history", null);
        } else {
            data.put("pdfName", pdfName);
            data.put("history", toView(h));
        }
        return ApiResponse.ok(data);
    }

    /**
     * 修改总台账单元格（新版 records 结构）：
     * body: { "record_index": 0, "field": "材料名称", "value": "...新值..." }
     */
    @PatchMapping(value = "/{pdfName}/ledger/field", consumes = MediaType.APPLICATION_JSON_VALUE)
    public Map<String, Object> patchLedgerField(@PathVariable String pdfName,
                                                @RequestBody Map<String, Object> body) {
        Object recRaw = body.get("record_index");
        Object fieldRaw = body.get("field");
        Object valueRaw = body.get("value");
        if (!(recRaw instanceof Number)) throw new BizException(400, "缺少字段 record_index（数字）");
        if (!(fieldRaw instanceof String) || ((String) fieldRaw).isEmpty()) throw new BizException(400, "缺少字段 field");
        String value = valueRaw == null ? "" : valueRaw.toString();
        libraryService.updateMasterField(pdfName, ((Number) recRaw).intValue(), (String) fieldRaw, value);
        return ApiResponse.ok("ok");
    }

        @lombok.Data
    public static class ImportRequest {
        private java.util.List<String> names;
    }

        // ============== 总台账（跨 PDF） ==============

    private HistoryView toView(HistoryEntity h) {
        HistoryView v = new HistoryView();
        v.setId(h.getId());
        v.setPdfName(h.getPdfName());
        v.setOriginalName(h.getOriginalName());
        v.setPageCount(h.getPageCount());
        v.setOcrCachedCount(h.getOcrCachedCount());
        v.setLedgerRecognized(h.getLedgerRecognized());
        v.setLedgerHallucinationCount(h.getLedgerHallucinationCount());
        v.setLastStatus(h.getLastStatus());
        v.setLastSummary(h.getLastSummary());
        v.setLastError(h.getLastError());
        v.setCreatedAt(h.getCreatedAt());
        v.setUpdatedAt(h.getUpdatedAt());
        return v;
    }
}
