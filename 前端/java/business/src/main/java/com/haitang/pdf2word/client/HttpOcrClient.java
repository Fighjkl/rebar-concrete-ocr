package com.haitang.pdf2word.client;

import com.alibaba.fastjson2.JSON;
import com.alibaba.fastjson2.JSONArray;
import com.alibaba.fastjson2.JSONObject;
import com.haitang.pdf2word.config.OcrProperties;
import com.haitang.pdf2word.exception.BizException;
import com.haitang.pdf2word.utils.StringUtils;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.*;
import org.springframework.stereotype.Component;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestTemplate;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.List;

@Slf4j
@Component
@RequiredArgsConstructor
public class HttpOcrClient {

    private final RestTemplate restTemplate;
    private final OcrProperties ocr;

    public List<PdfPageImage> pdfToImages(byte[] pdfBytes, String filename, String imageFormat, Integer dpi) {
        String url = ocr.getBaseUrl() + "/api/pdf/to_images";
        MultiValueMap<String, Object> form = new LinkedMultiValueMap<>();
        form.add("file", new ByteArrayResource(pdfBytes) {
            @Override
            public String getFilename() {
                return filename == null || filename.isEmpty() ? "upload.pdf" : filename;
            }
        });
        if (StringUtils.isNotBlank(imageFormat)) {
            form.add("image_format", imageFormat);
        }
        if (dpi != null && dpi > 0) {
            form.add("dpi", String.valueOf(dpi));
        }
        ResponseEntity<String> response = postWithRetry(url, form);
        JSONObject json = parseJson(response.getBody());
        int pageCount = json.getIntValue("page_count", 0);
        String defaultFormat = json.getString("format");
        JSONArray pagesArr = json.getJSONArray("pages");
        if (pagesArr == null || pagesArr.isEmpty()) {
            throw new BizException("PDF 页数据为空");
        }
        List<PdfPageImage> result = new ArrayList<>();
        for (int i = 0; i < pagesArr.size(); i++) {
            JSONObject item = pagesArr.getJSONObject(i);
            PdfPageImage page = new PdfPageImage();
            page.setPage(item.getIntValue("page", i + 1));
            page.setWidth(item.getIntValue("width", 0));
            page.setHeight(item.getIntValue("height", 0));
            String fmt = item.getString("format");
            page.setFormat(StringUtils.isBlank(fmt) ? defaultFormat : fmt);
            page.setImageBase64(item.getString("image_base64"));
            if (StringUtils.isBlank(page.getImageBase64())) {
                throw new BizException("第 " + page.getPage() + " 页图片为空");
            }
            result.add(page);
        }
        if (pageCount == 0) pageCount = result.size();
        if (result.size() != pageCount) {
            log.warn("PDF 返回页数与实际页数不一致: actual=" + result.size() + ", expected=" + pageCount);
        }
        return result;
    }

    public JSONObject ocrAll(byte[] imageBytes, String filename, int width, int height,
                             String prompt, String fields, String classifyPrompt, String classifySchema) {
        String url = ocr.getBaseUrl() + "/api/ocr/all";
        MultiValueMap<String, Object> form = new LinkedMultiValueMap<>();
        form.add("file", new ByteArrayResource(imageBytes) {
            @Override
            public String getFilename() {
                return filename == null || filename.isEmpty() ? "page.png" : filename;
            }
        });
        if (StringUtils.isNotBlank(prompt)) form.add("prompt", prompt);
        if (StringUtils.isNotBlank(fields)) form.add("fields", fields);
        if (StringUtils.isNotBlank(classifyPrompt)) form.add("classify_prompt", classifyPrompt);
        if (StringUtils.isNotBlank(classifySchema)) form.add("classify_schema", classifySchema);
        if (width > 0) form.add("image_width", String.valueOf(width));
        if (height > 0) form.add("image_height", String.valueOf(height));
        ResponseEntity<String> response = postWithRetry(url, form);
        return parseJson(response.getBody());
    }

    public JSONObject ocrModels() {
        ResponseEntity<String> response = restTemplate.exchange(ocr.getBaseUrl() + "/api/library/ocr-models",
                HttpMethod.GET, HttpEntity.EMPTY, String.class);
        return parseJson(response.getBody());
    }

    public JSONObject libraryList() {
        String url = ocr.getBaseUrl() + "/api/library";
        ResponseEntity<String> response = restTemplate.exchange(url, HttpMethod.GET, HttpEntity.EMPTY, String.class);
        return parseJson(response.getBody());
    }

    /** 检查远端 OCR 库是否已存在该卷（用于识别前自动同步历史卷）。 */
    public boolean remoteLibraryExists(String pdfName) {
        if (StringUtils.isBlank(pdfName)) return false;
        try {
            JSONObject list = libraryList();
            JSONArray pdfs = list == null ? null : list.getJSONArray("pdfs");
            if (pdfs == null) return false;
            for (int i = 0; i < pdfs.size(); i++) {
                JSONObject p = pdfs.getJSONObject(i);
                if (p != null && pdfName.equals(p.getString("name"))) return true;
            }
        } catch (Exception e) {
            log.warn("检查远端 OCR 库失败: {} -> {}", pdfName, e.getMessage());
        }
        return false;
    }

    public byte[] getLibraryImage(String pdfName, int page) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/pages/" + page + "/image";
        java.net.URI uri = java.net.URI.create(url);
        ResponseEntity<byte[]> r = restTemplate.exchange(uri, HttpMethod.GET, HttpEntity.EMPTY, byte[].class);
        return r.getBody();
    }

    public JSONObject getLibraryOcr(String pdfName, int page) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/pages/" + page + "/ocr";
        java.net.URI uri = java.net.URI.create(url);
        ResponseEntity<String> r = restTemplate.exchange(uri, HttpMethod.GET, HttpEntity.EMPTY, String.class);
        return parseJson(r.getBody());
    }

    public JSONObject getLibraryLedger(String pdfName) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/ledger";
        java.net.URI uri = java.net.URI.create(url);
        ResponseEntity<String> r = restTemplate.exchange(uri, HttpMethod.GET, HttpEntity.EMPTY, String.class);
        return parseJson(r.getBody());
    }

    /** 主动上传 PDF 到 OCR 服务的 library（name 与本地库一致，保证远端 recognize 可用）。 */
    public JSONObject uploadToLibrary(byte[] pdfBytes, String filename, String name,
                                      Integer dpi, String imageFormat, Boolean overwrite) {
        return uploadToLibrary(pdfBytes, filename, name, dpi, imageFormat, overwrite, "rebar");
    }

    public JSONObject uploadToLibrary(byte[] pdfBytes, String filename, String name,
                                      Integer dpi, String imageFormat, Boolean overwrite, String materialType) {
        String url = ocr.getBaseUrl() + "/api/library/upload";
        MultiValueMap<String, Object> form = new LinkedMultiValueMap<>();
        form.add("file", new ByteArrayResource(pdfBytes) {
            @Override
            public String getFilename() {
                return filename == null || filename.isEmpty() ? "upload.pdf" : filename;
            }
        });
        if (StringUtils.isNotBlank(name)) form.add("name", name);
        if (dpi != null && dpi > 0) form.add("dpi", String.valueOf(dpi));
        if (StringUtils.isNotBlank(imageFormat)) form.add("image_format", imageFormat);
        if (overwrite != null) form.add("overwrite", String.valueOf(overwrite));
        form.add("material_type", materialType);
        ResponseEntity<String> response = postWithRetry(url, form);
        return parseJson(response.getBody());
    }

    /** 远端单页 OCR：POST /api/library/{pdf}/pages/{page}/ocr，表单仅 prompt。 */
    public JSONObject runLibraryPageOcr(String pdfName, int page, String prompt) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/pages/" + page + "/ocr";
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        if (StringUtils.isNotBlank(prompt)) form.add("prompt", prompt);
        ResponseEntity<String> response = postFormWithRetry(url, form);
        return parseJson(response.getBody());
    }

    /** 远端整卷识别：POST /api/library/{pdf}/recognize。 */
    public JSONObject runLibraryRecognize(String pdfName, String prompt, String ocrPrompt,
                                          String model, boolean forceOcrRefresh) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/recognize";
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        if (StringUtils.isNotBlank(prompt)) form.add("prompt", prompt);
        if (StringUtils.isNotBlank(ocrPrompt)) form.add("ocr_prompt", ocrPrompt);
        if (StringUtils.isNotBlank(model)) form.add("model", model);
        if (forceOcrRefresh) form.add("force_ocr_refresh", "true");
        ResponseEntity<String> response = postFormWithRetry(url, form);
        return parseJson(response.getBody());
    }

    /** 提交快速返回的整卷任务；重试/刷新先查询状态，不重复启动耗时POST。 */
    public JSONObject startLibraryRecognition(String pdfName, String prompt, String ocrPrompt,
                                               String model, boolean forceOcrRefresh) {
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        if (StringUtils.isNotBlank(prompt)) form.add("prompt", prompt);
        if (StringUtils.isNotBlank(ocrPrompt)) form.add("ocr_prompt", ocrPrompt);
        if (StringUtils.isNotBlank(model)) form.add("model", model);
        if (forceOcrRefresh) form.add("force_ocr_refresh", "true");
        return parseJson(postFormWithRetry(ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/recognition", form).getBody());
    }

    public JSONObject getLibraryRecognition(String pdfName) {
        java.net.URI uri = java.net.URI.create(ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/recognition");
        return parseJson(restTemplate.exchange(uri, HttpMethod.GET, HttpEntity.EMPTY, String.class).getBody());
    }

    public JSONObject clearLibraryCache(String pdfName, String scope) {
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        form.add("scope", scope);
        return parseJson(postFormWithRetry(ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/cache", form).getBody());
    }

    /** 远端单页旋转：POST /api/library/{pdf}/pages/{page}/rotate，保持远端图片与本地一致。 */
    public JSONObject rotateLibraryPage(String pdfName, int page, int angle) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/pages/" + page + "/rotate";
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        form.add("angle", String.valueOf(angle));
        ResponseEntity<String> response = postFormWithRetry(url, form);
        return parseJson(response.getBody());
    }

    /** 远端重新切分：POST /api/library/{pdf}/resplit，保持远端图片与本地一致。 */
    public JSONObject resplitLibrary(String pdfName, Integer dpi, String imageFormat) {
        String url = ocr.getBaseUrl() + "/api/library/" + encode(pdfName) + "/resplit";
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        if (dpi != null && dpi > 0) form.add("dpi", String.valueOf(dpi));
        if (StringUtils.isNotBlank(imageFormat)) form.add("image_format", imageFormat);
        ResponseEntity<String> response = postFormWithRetry(url, form);
        return parseJson(response.getBody());
    }

    private ResponseEntity<String> postFormWithRetry(String url, MultiValueMap<String, String> form) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_FORM_URLENCODED);
        HttpEntity<MultiValueMap<String, String>> entity = new HttpEntity<>(form, headers);
        try {
            return restTemplate.exchange(java.net.URI.create(url), HttpMethod.POST, entity, String.class);
        } catch (Exception e) {
            // A timeout does not mean an OCR/rotate/cache operation was rejected.
            // The caller can recover through GET recognition without starting it again.
            throw new BizException("调用 OCR 失败: " + e.getMessage());
        }
    }
    private static String encode(String s) {
        try {
            return java.net.URLEncoder.encode(s, java.nio.charset.StandardCharsets.UTF_8.name()).replace("+", "%20");
        } catch (java.io.UnsupportedEncodingException e) {
            return s;
        }
    }

    private JSONObject parseJson(String body) {
        if (StringUtils.isBlank(body)) {
            throw new BizException("OCR 响应为空");
        }
        try {
            JSONObject json = JSON.parseObject(body);
            if (json == null) {
                throw new BizException("OCR 响应解析失败");
            }
            return json;
        } catch (BizException e) {
            throw e;
        } catch (Exception e) {
            throw new BizException("OCR 响应格式错误: " + body);
        }
    }

    private ResponseEntity<String> postWithRetry(String url, MultiValueMap<String, Object> form) {
        int max = Math.max(0, ocr.getRetry()) + 1;
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        HttpEntity<MultiValueMap<String, Object>> entity = new HttpEntity<>(form, headers);
        Exception last = null;
        for (int i = 0; i < max; i++) {
            try {
                return restTemplate.exchange(url, HttpMethod.POST, entity, String.class);
            } catch (Exception e) {
                last = e;
                if (e instanceof org.springframework.web.client.HttpClientErrorException) break;
                log.warn("调用 OCR 接口失败，重试 {} / {} -> {}", i + 1, max, e.getMessage());
                try {
                    Thread.sleep(500L * (i + 1));
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
        throw new BizException("调用 OCR 失败: " + (last == null ? "" : last.getMessage()));
    }

    public static byte[] decodeBase64(String s) {
        if (s == null || s.isEmpty()) return new byte[0];
        try {
            return Base64.getDecoder().decode(s);
        } catch (Exception ex) {
            throw new BizException("Base64 解码失败: " + ex.getMessage());
        }
    }

    public static List<String> imageExtensions() {
        return Collections.singletonList(".png");
    }

    @lombok.Data
    public static class PdfPageImage {
        private int page;
        private int width;
        private int height;
        private String format;
        private String imageBase64;
    }
}
