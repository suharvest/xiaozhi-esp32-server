package xiaozhi.modules.model.controller;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.Inet4Address;
import java.net.Inet6Address;
import java.net.InetAddress;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import cn.hutool.core.util.StrUtil;
import cn.hutool.http.HttpRequest;
import cn.hutool.http.HttpResponse;
import cn.hutool.json.JSONArray;
import cn.hutool.json.JSONObject;
import cn.hutool.json.JSONUtil;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.extern.slf4j.Slf4j;
import xiaozhi.common.utils.Result;
import xiaozhi.common.utils.UpstreamStatus;
import xiaozhi.modules.model.dto.ModelProbeDTO;

/**
 * 本地语音 / LLM 服务能力探测。
 *
 * <p>
 * 使用场景：智控台里用户还没保存任何模型配置，只填了一个 {@code ip:port}，
 * 需要后端替他打几个只读端点，把 ASR backend / TTS model_id / 采样率 / 音色表 /
 * LLM 模型列表探出来回填表单。
 * </p>
 *
 * <h3>与 {@code OvsTtsController} 的区别</h3>
 * <p>
 * {@code OvsTtsController} 的 SSRF 防护靠「用已保存的 modelId 反查 base_url」——
 * 探测阶段模型还没保存，那套用不了。这里改成一组独立可测的输入校验，见
 * {@link #parseAndValidateEndpoint(String)}。
 * </p>
 *
 * <h3>响应约定</h3>
 * <p>
 * HTTP 层**始终返回 200**，成功与失败都靠 {@code Result.code} 区分
 * ：前端 {@code httpRequest.js} 会把非 200
 * 的响应吞进全局错误处理器弹 toast，探测失败需要在表单内联展示，不能走那条路。
 * 失败时 {@code code} 用上游状态码（401 / 503 …），{@code msg} 用上游的错误原因。
 * </p>
 */
@Slf4j
@RestController
@RequestMapping("/models")
@Tag(name = "本地服务能力探测")
public class ModelProbeController {

    // ==================== 探测注册表 ====================
    //
    // probe id -> 要打的路径。**路径全部是本类的 private static final 字面量**，
    // 请求体里没有任何字段能影响它们。用户只能选 probe id（枚举），不能传路径。

    private static final String PROBE_OVS_VOICE = "ovs_voice";
    private static final String PROBE_OVS_TTS_SPEAKERS = "ovs_tts_speakers";
    private static final String PROBE_EDGELLM_MODELS = "edgellm_models";

    private static final String PATH_READYZ = "/readyz";
    private static final String PATH_ASR_CAPABILITIES = "/asr/capabilities";
    private static final String PATH_TTS_CAPABILITIES = "/tts/capabilities";
    private static final String PATH_TTS_SPEAKERS = "/tts/speakers";
    private static final String PATH_V1_MODELS = "/v1/models";

    // ==================== 传输层硬限制 ====================

    /** 连接超时（防护 5：不给攻击者用长连接做端口扫描 / 慢速探测的机会）。 */
    static final int CONNECT_TIMEOUT_MS = 5000;
    /** 读取超时（防护 5）。 */
    static final int READ_TIMEOUT_MS = 5000;
    /** 响应体大小上限 1 MiB（防护 5：防止被喂超大响应打爆 manager-api 的堆）。 */
    static final int MAX_BODY_BYTES = 1024 * 1024;

    /** {@code /readyz} 冷启动轮询次数（LAZY_TTS=1 时 TTS 懒加载，能力端点先返 503）。 */
    static final int READYZ_MAX_ATTEMPTS = 3;
    /** {@code /readyz} 轮询间隔。 */
    static final long READYZ_RETRY_INTERVAL_MS = 2000L;

    @PostMapping("/probe")
    @Operation(summary = "探测本地语音 / LLM 服务能力")
    @RequiresPermissions("sys:role:superAdmin") // 防护 6：仅超级管理员可发起对内网的出站请求
    public Result<Object> probe(@RequestBody ModelProbeDTO dto) {
        if (dto == null || StrUtil.isBlank(dto.getProbe())) {
            return new Result<Object>().error(400, "probe 不能为空");
        }
        String probe = dto.getProbe().trim();

        Endpoint endpoint;
        try {
            endpoint = parseAndValidateEndpoint(dto.getEndpoint());
        } catch (ProbeRejectedException e) {
            return new Result<Object>().error(e.getCode(), e.getMessage());
        }

        String apiKey = StrUtil.trimToNull(dto.getApiKey());
        try {
            return switch (probe) {
                case PROBE_OVS_VOICE -> probeOvsVoice(endpoint, apiKey);
                case PROBE_OVS_TTS_SPEAKERS -> probeOvsTtsSpeakers(endpoint, apiKey);
                case PROBE_EDGELLM_MODELS -> probeEdgeLlmModels(endpoint, apiKey);
                default -> new Result<Object>().error(400, "未知的探测项：" + probe);
            };
        } catch (ProbeRejectedException e) {
            return new Result<Object>().error(e.getCode(), e.getMessage());
        } catch (Exception e) {
            log.warn("探测 {} {} 失败：{}", probe, endpoint.display(), e.getMessage());
            return new Result<Object>().error(503, "探测失败：" + e.getMessage());
        }
    }

    // ==================== 各探测项实现 ====================

    /**
     * OVS 一体化探测：先 {@code /readyz}（最多 3 次 × 2s），
     * 再并发拉 {@code /asr/capabilities} + {@code /tts/capabilities} + {@code /tts/speakers}。
     *
     * <p>
     * 轮询是必须的：OVS 在 {@code LAZY_TTS=1} 时 TTS 懒加载，
     * 用户填完 IP 立刻点「检测」大概率撞上 503 冷启动窗口，不轮询就会拿到空音色表。
     * </p>
     */
    private Result<Object> probeOvsVoice(Endpoint ep, String apiKey) {
        boolean ready = false;
        String notReadyReason = null;
        for (int attempt = 1; attempt <= READYZ_MAX_ATTEMPTS; attempt++) {
            HttpOutcome readyz = fetch(ep, PATH_READYZ, apiKey);
            if (readyz.ok()) {
                ready = true;
                break;
            }
            notReadyReason = readyz.describe();
            // 连不上 / 鉴权失败重试没有意义，直接失败返回
            if (readyz.transportError() || readyz.status() == 401 || readyz.status() == 403) {
                return new Result<Object>().error(
                        readyz.transportError() ? 503 : UpstreamStatus.remap(readyz.status()),
                        readyz.describe());
            }
            if (attempt < READYZ_MAX_ATTEMPTS) {
                sleepQuietly(READYZ_RETRY_INTERVAL_MS);
            }
        }

        // 三个能力端点并发拉取（每个都各自带 5s 超时，整体最坏 ~5s 而不是 15s）
        CompletableFuture<HttpOutcome> asrF = CompletableFuture
                .supplyAsync(() -> fetch(ep, PATH_ASR_CAPABILITIES, apiKey));
        CompletableFuture<HttpOutcome> ttsF = CompletableFuture
                .supplyAsync(() -> fetch(ep, PATH_TTS_CAPABILITIES, apiKey));
        CompletableFuture<HttpOutcome> spkF = CompletableFuture
                .supplyAsync(() -> fetch(ep, PATH_TTS_SPEAKERS, apiKey));
        CompletableFuture.allOf(asrF, ttsF, spkF).join();
        HttpOutcome asr = asrF.join();
        HttpOutcome tts = ttsF.join();
        HttpOutcome spk = spkF.join();

        // 鉴权失败是整体失败：apiKey 填错时三个端点都会 401，没有部分可用的说法
        for (HttpOutcome o : List.of(asr, tts, spk)) {
            if (o.status() == 401 || o.status() == 403) {
                return new Result<Object>().error(UpstreamStatus.remap(o.status()), o.describe());
            }
        }
        // 三个全挂 = 这个地址根本不是 OVS
        if (!ready && !asr.ok() && !tts.ok() && !spk.ok()) {
            return new Result<Object>().error(503,
                    StrUtil.blankToDefault(notReadyReason, asr.describe()));
        }

        Map<String, Object> data = new LinkedHashMap<>();
        List<String> warnings = new ArrayList<>();
        data.put("ready", ready);
        if (!ready && notReadyReason != null) {
            warnings.add("/readyz 未就绪：" + notReadyReason);
        }

        // --- ASR ---
        data.put("asrBackend", null);
        data.put("asrCapabilities", new ArrayList<String>());
        data.put("sampleRate", null);
        if (asr.ok()) {
            JSONObject body = asr.json();
            if (body != null) {
                data.put("asrBackend", body.getStr("backend"));
                data.put("asrCapabilities", toStringList(body.getJSONArray("capabilities")));
                data.put("sampleRate", body.getInt("sample_rate"));
            }
        } else {
            warnings.add("/asr/capabilities 不可用：" + asr.describe());
        }

        // --- TTS ---
        data.put("ttsBackend", null);
        data.put("ttsModelId", null);
        data.put("ttsSampleRate", null);
        data.put("supportsVoiceCloning", false);
        if (tts.ok()) {
            JSONObject body = tts.json();
            if (body != null) {
                data.put("ttsBackend", body.getStr("backend"));
                data.put("ttsModelId", body.getStr("model_id"));
                data.put("ttsSampleRate", body.getInt("sample_rate"));
                data.put("supportsVoiceCloning", body.getBool("supports_voice_cloning", false));
            }
        } else {
            warnings.add("/tts/capabilities 不可用：" + tts.describe());
        }

        // --- 音色表 ---
        data.put("defaultSpeakerId", null);
        data.put("speakers", new ArrayList<>());
        if (spk.ok()) {
            JSONObject body = spk.json();
            if (body != null) {
                data.put("defaultSpeakerId", body.get("default_speaker_id"));
                data.put("speakers", sanitizeSpeakers(body.getJSONArray("speakers")));
                if (body.containsKey("supports_voice_cloning")) {
                    data.put("supportsVoiceCloning", body.getBool("supports_voice_cloning", false));
                }
                if (data.get("ttsModelId") == null) {
                    data.put("ttsModelId", body.getStr("model_id"));
                }
            }
        } else {
            warnings.add("/tts/speakers 不可用：" + spk.describe());
        }

        data.put("warnings", warnings);
        return new Result<Object>().ok(data);
    }

    /** 只拉音色表，用于 {@code remote-select} 字段的下拉刷新。 */
    private Result<Object> probeOvsTtsSpeakers(Endpoint ep, String apiKey) {
        HttpOutcome out = fetch(ep, PATH_TTS_SPEAKERS, apiKey);
        if (!out.ok()) {
            return new Result<Object>().error(
                    out.transportError() ? 503 : UpstreamStatus.remap(out.status()), out.describe());
        }
        JSONObject body = out.json();
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("defaultSpeakerId", body == null ? null : body.get("default_speaker_id"));
        data.put("speakers", body == null ? new ArrayList<>() : sanitizeSpeakers(body.getJSONArray("speakers")));
        return new Result<Object>().ok(data);
    }

    /** EdgeLLM（OpenAI 兼容）模型列表。注意这是弱校验，只说明 metadata 可读。 */
    private Result<Object> probeEdgeLlmModels(Endpoint ep, String apiKey) {
        HttpOutcome out = fetch(ep, PATH_V1_MODELS, apiKey);
        if (!out.ok()) {
            return new Result<Object>().error(
                    out.transportError() ? 503 : UpstreamStatus.remap(out.status()), out.describe());
        }
        List<Map<String, Object>> models = new ArrayList<>();
        JSONObject body = out.json();
        JSONArray raw = body == null ? null : body.getJSONArray("data");
        if (raw != null) {
            for (Object item : raw) {
                if (item instanceof JSONObject obj && obj.get("id") != null) {
                    Map<String, Object> entry = new LinkedHashMap<>();
                    entry.put("id", obj.getStr("id"));
                    models.add(entry);
                }
            }
        }
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("models", models);
        return new Result<Object>().ok(data);
    }

    // ==================== SSRF 防护 ====================

    /** 校验通过的目标：{@code connectHost} 是真正用来连接的字面量（域名场景下是已解析并校验过的 IP）。 */
    record Endpoint(String connectHost, String requestedHost, int port) {
        /** 拼进 URL 的 host 片段，IPv6 要加方括号。 */
        String urlHost() {
            return connectHost.contains(":") ? "[" + connectHost + "]" : connectHost;
        }

        String display() {
            return requestedHost + ":" + port;
        }

        boolean resolvedFromName() {
            return !connectHost.equals(requestedHost);
        }
    }

    /** 校验失败：带上要回显给前端的 code。 */
    static class ProbeRejectedException extends RuntimeException {
        private static final long serialVersionUID = 1L;
        private final int code;

        ProbeRejectedException(int code, String msg) {
            super(msg);
            this.code = code;
        }

        int getCode() {
            return code;
        }
    }

    /**
     * SSRF 防护总入口 —— 把用户输入收敛成一个「私有网段里的 host:port」。
     *
     * <p>
     * 逐条防的是什么：
     * </p>
     * <ol>
     * <li><b>只接受 {@code host:port}，不接受完整 URL</b>。拒绝 scheme（{@code http://}、
     * {@code file://}、{@code gopher://}）、path、query、fragment、{@code @}（userinfo
     * 可以把 {@code evil.com@10.0.0.1} 这类骗过朴素的解析器）、空白与控制字符。
     * 从源头砍掉绝大部分注入面 —— 目标 URL 由本方法的返回值 + 类内字面量路径拼成，
     * 用户不可能影响路径部分。</li>
     * <li><b>目标必须落在私有地址段</b>：10/8、172.16/12、192.168/16、127/8、169.254/16、
     * ::1、fc00::/7。挡掉「拿 manager-api 当跳板打公网」以及云上 169.254.169.254
     * 元数据服务（169.254/16 虽是私有段，这里单独在 {@link #isAllowedPrivateAddress}
     * 里说明：它属于允许段，但云元数据端点需要靠部署侧网络策略兜底）。
     * 本地语音服务本来就在内网，拒绝公网不牺牲任何真实场景。</li>
     * <li><b>DNS rebinding 防护</b>：host 是域名时，解析出的**每一个** IP 都必须仍在私有段；
     * 并且把解析结果 pin 住（返回 IP 字面量做实际连接），避免「校验时解析到内网 IP、
     * 连接时再解析到公网 IP」的 TOCTOU。</li>
     * <li><b>端口范围 1..65535</b>，拒绝 0、负数、越界与非数字（{@code 8621abc}）。</li>
     * </ol>
     *
     * @param raw 前端传入的 endpoint 原文
     * @return 校验通过的目标
     * @throws ProbeRejectedException 任何一条不满足
     */
    static Endpoint parseAndValidateEndpoint(String raw) {
        if (StrUtil.isBlank(raw)) {
            throw new ProbeRejectedException(400, "endpoint 不能为空");
        }
        String value = raw.trim();

        // --- 防护 1：只接受 host:port，任何 URL 成分一律拒绝 ---
        for (char c : value.toCharArray()) {
            if (c <= 0x20 || c == 0x7F) {
                throw new ProbeRejectedException(400, "endpoint 不能包含空白或控制字符");
            }
        }
        if (value.contains("://") || value.contains("/") || value.contains("\\")) {
            throw new ProbeRejectedException(400,
                    "endpoint 只接受 host:port 形式，不要带协议或路径（如 192.168.1.50:8621）");
        }
        if (value.indexOf('?') >= 0 || value.indexOf('#') >= 0) {
            throw new ProbeRejectedException(400, "endpoint 不能包含查询参数或锚点");
        }
        if (value.indexOf('@') >= 0) {
            throw new ProbeRejectedException(400, "endpoint 不能包含用户信息（@）");
        }

        // --- 拆 host / port（含 IPv6 的 [::1]:8621 形式）---
        String host;
        String portText;
        if (value.startsWith("[")) {
            int close = value.indexOf(']');
            if (close < 0) {
                throw new ProbeRejectedException(400, "IPv6 地址缺少右方括号");
            }
            host = value.substring(1, close);
            String rest = value.substring(close + 1);
            if (!rest.startsWith(":")) {
                throw new ProbeRejectedException(400, "endpoint 必须带端口，形如 [::1]:8621");
            }
            portText = rest.substring(1);
        } else {
            int colon = value.lastIndexOf(':');
            if (colon < 0) {
                throw new ProbeRejectedException(400, "endpoint 必须带端口，形如 192.168.1.50:8621");
            }
            host = value.substring(0, colon);
            portText = value.substring(colon + 1);
            // 裸 IPv6（多个冒号）必须写成 [..]:port，否则无法区分地址与端口
            if (host.indexOf(':') >= 0) {
                throw new ProbeRejectedException(400, "IPv6 地址请写成 [::1]:8621 形式");
            }
        }
        if (StrUtil.isBlank(host)) {
            throw new ProbeRejectedException(400, "endpoint 缺少主机名");
        }

        // --- 防护 4：端口范围 ---
        int port;
        try {
            port = Integer.parseInt(portText);
        } catch (NumberFormatException e) {
            throw new ProbeRejectedException(400, "端口必须是数字：" + portText);
        }
        if (port < 1 || port > 65535) {
            throw new ProbeRejectedException(400, "端口超出范围（1-65535）：" + port);
        }

        // --- 防护 2 / 3：地址必须落在私有段；域名要解析后复校验并 pin 住结果 ---
        InetAddress[] resolved;
        try {
            resolved = InetAddress.getAllByName(host);
        } catch (UnknownHostException e) {
            throw new ProbeRejectedException(400, "无法解析主机名：" + host);
        }
        if (resolved.length == 0) {
            throw new ProbeRejectedException(400, "无法解析主机名：" + host);
        }
        for (InetAddress addr : resolved) {
            if (!isAllowedPrivateAddress(addr)) {
                throw new ProbeRejectedException(400,
                        "只允许探测内网地址（10/8、172.16/12、192.168/16、127/8、169.254/16、::1、fc00::/7），"
                                + "拒绝：" + addr.getHostAddress());
            }
        }
        // pin 住解析结果：后续 HTTP 请求直接连这个 IP 字面量，DNS 再变也影响不到
        return new Endpoint(resolved[0].getHostAddress(), host, port);
    }

    /**
     * 防护 2 的判定：地址是否落在允许的私有段。
     *
     * <p>
     * IPv4：10/8、172.16/12、192.168/16、127/8、169.254/16。<br>
     * IPv6：::1（回环）、fc00::/7（ULA）、fe80::/10（链路本地，等价于 IPv4 的 169.254/16）。
     * </p>
     *
     * <p>
     * <b>云元数据端点在代码层显式拉黑</b>，不依赖部署侧网络策略。169.254.169.254
     * （AWS/GCP/Azure/OpenStack IMDS）、169.254.170.2（ECS 任务元数据）、
     * fd00:ec2::254（AWS IPv6 IMDS）在语义上属于链路本地段，但它们是 SSRF 最经典的
     * 提权目标 —— 一旦 manager-api 跑在公有云上，放行等于把临时凭证送出去。
     * 拉黑的代价是零：没有人会把本地语音服务部署在元数据地址上。
     * 「靠部署侧网络策略兜底」不是可接受的方案，交付给客户的产品不能假设对方配了网络策略。
     * </p>
     */
    /**
     * 云厂商实例元数据端点的显式黑名单。这些地址落在链路本地段内，若不单独拉黑就会被
     * {@link #isAllowedPrivateAddress} 放行，而它们正是 SSRF 攻击最常见的提权目标
     * （读取实例角色的临时凭证）。
     *
     * <ul>
     * <li>169.254.169.254 —— AWS / GCP / Azure / OpenStack / 阿里云 IMDS</li>
     * <li>169.254.170.2 —— AWS ECS 任务元数据</li>
     * <li>fd00:ec2::254 —— AWS IPv6 IMDS</li>
     * </ul>
     */
    static boolean isCloudMetadataAddress(InetAddress addr) {
        byte[] b = addr.getAddress();
        if (addr instanceof Inet4Address) {
            int b0 = b[0] & 0xFF, b1 = b[1] & 0xFF, b2 = b[2] & 0xFF, b3 = b[3] & 0xFF;
            if (b0 == 169 && b1 == 254 && b2 == 169 && b3 == 254) {
                return true;
            }
            return b0 == 169 && b1 == 254 && b2 == 170 && b3 == 2;
        }
        // fd00:ec2::254
        byte[] awsV6 = { (byte) 0xFD, 0x00, 0x0E, (byte) 0xC2, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0x02, 0x54 };
        return java.util.Arrays.equals(b, awsV6);
    }

    static boolean isAllowedPrivateAddress(InetAddress addr) {
        if (addr == null) {
            return false;
        }
        if (isCloudMetadataAddress(addr)) {
            return false;
        }
        if (addr instanceof Inet4Address) {
            byte[] b = addr.getAddress();
            int b0 = b[0] & 0xFF;
            int b1 = b[1] & 0xFF;
            if (b0 == 10) {
                return true; // 10.0.0.0/8
            }
            if (b0 == 172 && b1 >= 16 && b1 <= 31) {
                return true; // 172.16.0.0/12
            }
            if (b0 == 192 && b1 == 168) {
                return true; // 192.168.0.0/16
            }
            if (b0 == 127) {
                return true; // 127.0.0.0/8
            }
            return b0 == 169 && b1 == 254; // 169.254.0.0/16
        }
        if (addr instanceof Inet6Address) {
            if (addr.isLoopbackAddress()) {
                return true; // ::1
            }
            byte[] b = addr.getAddress();
            if ((b[0] & 0xFE) == 0xFC) {
                return true; // fc00::/7 (ULA)
            }
            return addr.isLinkLocalAddress(); // fe80::/10
        }
        return false;
    }

    // ==================== HTTP ====================

    /** 一次探测请求的结果：要么拿到状态码 + body，要么是传输层错误。 */
    record HttpOutcome(int status, String body, String transportErrorMessage) {

        static HttpOutcome transport(String message) {
            return new HttpOutcome(0, null, message);
        }

        boolean transportError() {
            return transportErrorMessage != null;
        }

        boolean ok() {
            return !transportError() && status >= 200 && status < 300;
        }

        JSONObject json() {
            if (StrUtil.isBlank(body)) {
                return null;
            }
            try {
                return JSONUtil.parseObj(body);
            } catch (Exception e) {
                return null;
            }
        }

        /** 给前端看的失败原因：优先用上游 JSON 里的 error / detail / message。 */
        String describe() {
            if (transportError()) {
                return "无法连接：" + transportErrorMessage;
            }
            if (StrUtil.isNotBlank(body)) {
                try {
                    JSONObject obj = JSONUtil.parseObj(body);
                    for (String key : new String[] { "error", "detail", "message", "msg" }) {
                        Object v = obj.get(key);
                        if (v != null) {
                            String text = v instanceof CharSequence ? v.toString() : JSONUtil.toJsonStr(v);
                            return "HTTP " + status + "：" + StrUtil.maxLength(text, 300);
                        }
                    }
                } catch (Exception ignored) {
                    // 非 JSON 响应，回落到原文
                }
                return "HTTP " + status + "：" + StrUtil.maxLength(body.trim(), 300);
            }
            return "HTTP " + status;
        }
    }

    /**
     * 发一次只读 GET。
     *
     * <p>
     * 目标 URL = {@code http://} + 已校验的 host:port + <b>类内字面量</b> path，
     * 请求体里没有任何东西能影响 path。
     * </p>
     *
     * <p>
     * 防护 5：{@code setFollowRedirects(false)} —— 不跟随重定向。否则内网服务返回
     * {@code 302 http://169.254.169.254/...} 就能把前面所有的地址校验绕过去。
     * 3xx 一律当失败处理。连接与读取超时各 5s，响应体读取封顶 1 MiB。
     * </p>
     */
    HttpOutcome fetch(Endpoint ep, String path, String apiKey) {
        String url = "http://" + ep.urlHost() + ":" + ep.port() + path;
        HttpResponse response = null;
        try {
            HttpRequest request = HttpRequest.get(url)
                    .setFollowRedirects(false)
                    .setMaxRedirectCount(0)
                    .setConnectionTimeout(CONNECT_TIMEOUT_MS)
                    .setReadTimeout(READ_TIMEOUT_MS);
            if (ep.resolvedFromName()) {
                // 域名场景下连的是 pin 住的 IP，补一个 Host 头让虚拟主机仍能路由。
                // （JDK 的受限请求头策略可能忽略它，本地语音服务不依赖 Host 路由，可接受）
                request.header("Host", ep.requestedHost() + ":" + ep.port());
            }
            if (StrUtil.isNotBlank(apiKey)) {
                // OVS / EdgeLLM 的鉴权头都是 Authorization: Bearer <key>；apiKey 为空则不带
                request.header("Authorization", "Bearer " + apiKey);
            }
            // execute(true) = 异步模式，body 不预读，交给下面的限长读取
            response = request.execute(true);
            int status = response.getStatus();
            if (status >= 300 && status < 400) {
                return new HttpOutcome(status, "{\"error\":\"上游返回重定向，已按安全策略拒绝跟随\"}", null);
            }
            return new HttpOutcome(status, readLimited(response), null);
        } catch (ProbeRejectedException e) {
            throw e;
        } catch (Exception e) {
            String msg = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
            log.debug("探测 {} 失败：{}", url, msg);
            return HttpOutcome.transport(msg);
        } finally {
            if (response != null) {
                response.close();
            }
        }
    }

    /** 防护 5：响应体封顶 {@link #MAX_BODY_BYTES}，超限直接截断并报错，绝不无限吃进内存。 */
    private static String readLimited(HttpResponse response) {
        try (InputStream in = response.bodyStream()) {
            if (in == null) {
                return "";
            }
            ByteArrayOutputStream buffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            int n;
            while ((n = in.read(chunk)) > 0) {
                if (buffer.size() + n > MAX_BODY_BYTES) {
                    throw new ProbeRejectedException(502, "上游响应体超过 1MB 上限，已中止读取");
                }
                buffer.write(chunk, 0, n);
            }
            return buffer.toString(StandardCharsets.UTF_8);
        } catch (ProbeRejectedException e) {
            throw e;
        } catch (Exception e) {
            return "";
        }
    }

    // ==================== 响应整形 ====================

    /**
     * 只透传 {@code id / label / type} 三个字段。
     *
     * <p>
     * OVS 的 {@code /tts/speakers} 会带上 {@code speaker_embedding_b64}，而且是**截断值**
     * （前 40 字符 + "..."，见 {@code server/core/tts_speakers.py:376}）——
     * 对前端既无用又误导，一律丢弃。{@code payload}（preset 的内部标识）同理不外泄。
     * </p>
     */
    private static List<Map<String, Object>> sanitizeSpeakers(JSONArray raw) {
        List<Map<String, Object>> speakers = new ArrayList<>();
        if (raw == null) {
            return speakers;
        }
        for (Object item : raw) {
            if (!(item instanceof JSONObject obj)) {
                continue;
            }
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("id", obj.get("id"));
            Object label = obj.get("label");
            if (label == null || StrUtil.isBlank(label.toString())) {
                label = obj.get("name");
            }
            if (label == null || StrUtil.isBlank(label.toString())) {
                label = "Speaker " + obj.get("id");
            }
            entry.put("label", label.toString());
            entry.put("type", obj.getStr("type", "preset"));
            speakers.add(entry);
        }
        return speakers;
    }

    private static List<String> toStringList(JSONArray array) {
        List<String> list = new ArrayList<>();
        if (array != null) {
            for (Object item : array) {
                if (item != null) {
                    list.add(item.toString());
                }
            }
        }
        return list;
    }

    private static void sleepQuietly(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** 仅供单元测试断言注册表内容用。 */
    static List<String> supportedProbes() {
        return Arrays.asList(PROBE_OVS_VOICE, PROBE_OVS_TTS_SPEAKERS, PROBE_EDGELLM_MODELS);
    }
}
