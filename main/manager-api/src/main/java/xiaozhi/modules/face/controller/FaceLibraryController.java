package xiaozhi.modules.face.controller;

import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import cn.hutool.core.util.StrUtil;
import cn.hutool.http.HttpRequest;
import cn.hutool.http.HttpResponse;
import cn.hutool.json.JSONUtil;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.extern.slf4j.Slf4j;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.sys.service.SysParamsService;

/**
 * 人脸库管理 —— 仓管系统 /api/face/* 的薄代理。
 *
 * 设计约束（与 {@code OvsTtsController} 同构）：
 * <ul>
 * <li>全部业务逻辑（embedding 计算、去重、张数上限、model_tag 一致性、租户隔离）
 * 都留在仓管系统，这里一行都不复制。</li>
 * <li>目标地址只从 sys param {@code server.face_warehouse} 读取，
 * <b>绝不接受前端传入的 URL / host / path</b>（SSRF 防护）。</li>
 * <li>API key 只在服务端拼进 {@code X-API-Key} 请求头，永不下发给前端。</li>
 * <li>仓管系统的 HTTP 状态码与错误信息原样回显在 {@code code} / {@code msg} 上，
 * 便于前端识别 {@code FACE_ENABLED=false} 时的 404。</li>
 * </ul>
 */
@Slf4j
@RestController
@RequestMapping("/face")
@Tag(name = "人脸库管理")
public class FaceLibraryController {

    /** 仓管系统人脸管理端点的固定前缀，永远是字面量，不接受外部拼接。 */
    private static final String FACE_PATH_PREFIX = "/api/face";

    private static final int TIMEOUT_MS = 15000;

    @Autowired
    private SysParamsService sysParamsService;

    // ==================== enrollments ====================

    @GetMapping("/enrollments")
    @Operation(summary = "人脸录入列表")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> listEnrollments(
            @RequestParam(required = false) Integer subjectId,
            @RequestParam(required = false) Integer tenantId) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "subject_id", subjectId);
        putIfNotNull(query, "tenant_id", tenantId);
        return proxy("GET", "/enrollments", query, null);
    }

    @PostMapping("/enrollments")
    @Operation(summary = "新增人脸录入")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> createEnrollment(
            @RequestBody Map<String, Object> body,
            @RequestParam(required = false) Integer tenantId) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "tenant_id", tenantId);
        return proxy("POST", "/enrollments", query, body);
    }

    @DeleteMapping("/enrollments/{enrollmentId}")
    @Operation(summary = "删除人脸录入")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> deleteEnrollment(
            @PathVariable Long enrollmentId,
            @RequestParam(required = false) Integer tenantId) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "tenant_id", tenantId);
        return proxy("DELETE", "/enrollments/" + enrollmentId, query, null);
    }

    // ==================== subjects ====================

    @GetMapping("/subjects")
    @Operation(summary = "人员档案列表")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> listSubjects(
            @RequestParam(required = false) Integer tenantId,
            @RequestParam(required = false) Boolean includeInactive) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "tenant_id", tenantId);
        if (includeInactive != null) {
            query.put("include_inactive", includeInactive ? "true" : "false");
        }
        return proxy("GET", "/subjects", query, null);
    }

    @PostMapping("/subjects")
    @Operation(summary = "新增人员档案")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> createSubject(
            @RequestBody Map<String, Object> body,
            @RequestParam(required = false) Integer tenantId) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "tenant_id", tenantId);
        return proxy("POST", "/subjects", query, body);
    }

    @PutMapping("/subjects/{subjectId}")
    @Operation(summary = "修改人员档案")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> updateSubject(
            @PathVariable Long subjectId,
            @RequestBody Map<String, Object> body,
            @RequestParam(required = false) Integer tenantId) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "tenant_id", tenantId);
        return proxy("PUT", "/subjects/" + subjectId, query, body);
    }

    @DeleteMapping("/subjects/{subjectId}")
    @Operation(summary = "删除人员档案")
    @RequiresPermissions("sys:role:superAdmin")
    public Result<Object> deleteSubject(
            @PathVariable Long subjectId,
            @RequestParam(required = false) Integer tenantId) {
        Map<String, Object> query = new LinkedHashMap<>();
        putIfNotNull(query, "tenant_id", tenantId);
        return proxy("DELETE", "/subjects/" + subjectId, query, null);
    }

    // ==================== 内部实现 ====================

    private static void putIfNotNull(Map<String, Object> map, String key, Object value) {
        if (value != null) {
            map.put(key, value);
        }
    }

    /**
     * 统一代理。
     *
     * @param method   HTTP 方法，字面量，不来自请求
     * @param subPath  {@code /api/face} 之后的路径，只由本类的字面量 + 数字 ID 拼成
     * @param query    白名单查询参数
     * @param jsonBody 请求体（原样透传给仓管系统）
     */
    private Result<Object> proxy(String method, String subPath,
            Map<String, Object> query, Map<String, Object> jsonBody) {
        String raw = sysParamsService.getValue(Constant.SERVER_FACE_WAREHOUSE, true);
        if (StrUtil.isBlank(raw) || "null".equalsIgnoreCase(raw.trim())) {
            return new Result<Object>().error(503, "未配置仓管系统人脸库地址（server.face_warehouse）");
        }

        URI uri;
        try {
            uri = new URI(raw.trim());
        } catch (URISyntaxException e) {
            log.error("server.face_warehouse 格式不正确：{}", raw);
            return new Result<Object>().error(500, "server.face_warehouse 地址格式不正确");
        }

        String apiKey = extractKey(uri.getQuery());
        String url = buildUrl(uri, subPath, query);
        if (url == null) {
            return new Result<Object>().error(500, "server.face_warehouse 地址缺少 host");
        }

        HttpResponse response = null;
        try {
            HttpRequest request = switch (method) {
                case "POST" -> HttpRequest.post(url);
                case "PUT" -> HttpRequest.put(url);
                case "DELETE" -> HttpRequest.delete(url);
                default -> HttpRequest.get(url);
            };
            request.timeout(TIMEOUT_MS);
            if (StrUtil.isNotBlank(apiKey)) {
                request.header("X-API-Key", apiKey);
            }
            if (jsonBody != null) {
                request.header("Content-Type", "application/json;charset=UTF-8");
                request.body(JSONUtil.toJsonStr(jsonBody), "application/json;charset=UTF-8");
            }
            response = request.execute();

            int status = response.getStatus();
            String body = response.body();
            if (status >= 200 && status < 300) {
                return new Result<Object>().ok(parseBody(body));
            }
            // 原样回显仓管系统的错误码与错误信息（前端据此识别 FACE_ENABLED=false 的 404）
            return new Result<Object>().error(status, extractDetail(body, status));
        } catch (Exception e) {
            log.error("调用仓管系统人脸接口失败 {} {}：{}", method, subPath, e.getMessage());
            return new Result<Object>().error(503, "仓管系统不可达：" + e.getMessage());
        } finally {
            if (response != null) {
                response.close();
            }
        }
    }

    /**
     * SSRF 防护核心：目标地址 100% 由 sys param 决定。
     * scheme / host / port / 上下文路径全部取自 {@code server.face_warehouse}，
     * 路径段是本类字面量，只有数字 ID 和白名单查询参数来自请求，且经过 URL 编码。
     */
    private String buildUrl(URI uri, String subPath, Map<String, Object> query) {
        String scheme = uri.getScheme();
        String host = uri.getHost();
        if (StrUtil.isBlank(host)) {
            return null;
        }
        if (StrUtil.isBlank(scheme)) {
            scheme = "http";
        }
        int port = uri.getPort();
        String root = port == -1
                ? "%s://%s".formatted(scheme, host)
                : "%s://%s:%s".formatted(scheme, host, port);

        // sys param 里的路径形如 /api（或反代子路径 /wh/api），去掉末尾的 /api
        // 之后作为上下文前缀，再拼固定的 /api/face。
        String context = StrUtil.nullToEmpty(uri.getPath());
        context = StrUtil.removeSuffix(context, "/");
        context = StrUtil.removeSuffix(context, "/api");
        if ("/api".equals(context) || "api".equals(context)) {
            context = "";
        }

        StringBuilder sb = new StringBuilder(root).append(context)
                .append(FACE_PATH_PREFIX).append(subPath);
        if (query != null && !query.isEmpty()) {
            sb.append('?');
            boolean first = true;
            for (Map.Entry<String, Object> e : query.entrySet()) {
                if (!first) {
                    sb.append('&');
                }
                first = false;
                sb.append(e.getKey()).append('=')
                        .append(java.net.URLEncoder.encode(String.valueOf(e.getValue()),
                                StandardCharsets.UTF_8));
            }
        }
        return sb.toString();
    }

    /** 从 {@code ?key=xxx} 里取出 API key，只在服务端使用。 */
    private String extractKey(String rawQuery) {
        if (StrUtil.isBlank(rawQuery)) {
            return null;
        }
        for (String pair : rawQuery.split("&")) {
            int idx = pair.indexOf('=');
            if (idx > 0 && "key".equals(pair.substring(0, idx))) {
                return pair.substring(idx + 1);
            }
        }
        return null;
    }

    private Object parseBody(String body) {
        if (StrUtil.isBlank(body)) {
            return null;
        }
        try {
            return JSONUtil.parse(body);
        } catch (Exception e) {
            return body;
        }
    }

    /** FastAPI 的错误体是 {"detail": "..."}；取不到就回落到原始 body。 */
    private String extractDetail(String body, int status) {
        if (StrUtil.isBlank(body)) {
            return "仓管系统返回 " + status;
        }
        try {
            Object detail = JSONUtil.parseObj(body).get("detail");
            if (detail != null) {
                return detail instanceof CharSequence ? detail.toString() : JSONUtil.toJsonStr(detail);
            }
        } catch (Exception ignored) {
            // 非 JSON 响应，直接回显原文
        }
        return StrUtil.maxLength(body, 500);
    }
}
