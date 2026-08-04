package xiaozhi.modules.model.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import com.sun.net.httpserver.HttpServer;

import xiaozhi.common.utils.Result;
import xiaozhi.modules.model.controller.ModelProbeController.ProbeRejectedException;
import xiaozhi.modules.model.dto.ModelProbeDTO;

/**
 * {@link ModelProbeController} 的 SSRF 防护与探测装配测试。
 *
 * <p>
 * 成功路径用 JDK 自带的 {@link HttpServer} 起一个 127.0.0.1 的 mock OVS / EdgeLLM，
 * 不连任何真实设备。
 * </p>
 */
class ModelProbeControllerTest {

    private ModelProbeController controller;
    private HttpServer server;
    private int port;
    private final Map<String, String> lastAuthHeader = new ConcurrentHashMap<>();
    private final AtomicInteger readyzHits = new AtomicInteger();
    private volatile int readyzNotReadyTimes = 0;

    @BeforeEach
    void setUp() throws IOException {
        controller = new ModelProbeController();
        server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
        port = server.getAddress().getPort();

        server.createContext("/readyz", ex -> {
            int hit = readyzHits.incrementAndGet();
            if (hit <= readyzNotReadyTimes) {
                respond(ex, 503, "{\"status\":\"not_ready\",\"reasons\":[\"backend_not_ready\"]}");
            } else {
                respond(ex, 200, "{\"status\":\"ready\"}");
            }
        });
        server.createContext("/asr/capabilities", ex -> {
            lastAuthHeader.put("asr", String.valueOf(ex.getRequestHeaders().getFirst("Authorization")));
            respond(ex, 200, "{\"backend\":\"funasr\",\"capabilities\":[\"streaming\",\"vad\"],"
                    + "\"sample_rate\":16000}");
        });
        server.createContext("/tts/capabilities", ex -> respond(ex, 200,
                "{\"backend\":\"sparktts\",\"model_id\":\"spark-tts-0.5b\","
                        + "\"capabilities\":[\"voice_clone\"],\"supports_voice_cloning\":true,"
                        + "\"sample_rate\":24000}"));
        server.createContext("/tts/speakers", ex -> respond(ex, 200,
                "{\"model_id\":\"spark-tts-0.5b\",\"default_speaker_id\":0,"
                        + "\"supports_voice_cloning\":true,\"speakers\":["
                        + "{\"id\":0,\"type\":\"preset\",\"label\":\"女声\",\"payload\":\"female\"},"
                        + "{\"id\":10000,\"type\":\"embedding\",\"label\":\"alice\","
                        + "\"speaker_embedding_b64\":\"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA...\","
                        + "\"meta\":{\"dim\":512}}]}"));
        server.createContext("/v1/models", ex -> respond(ex, 200,
                "{\"object\":\"list\",\"data\":[{\"id\":\"qwen3-4b\"},{\"id\":\"qwen3-8b\"}]}"));
        server.createContext("/secret", ex -> respond(ex, 200, "{\"leaked\":true}"));
        server.setExecutor(null);
        server.start();
    }

    @AfterEach
    void tearDown() {
        if (server != null) {
            server.stop(0);
        }
    }

    private static void respond(com.sun.net.httpserver.HttpExchange ex, int status, String body)
            throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private ModelProbeDTO dto(String probe, String endpoint, String apiKey) {
        ModelProbeDTO d = new ModelProbeDTO();
        d.setProbe(probe);
        d.setEndpoint(endpoint);
        d.setApiKey(apiKey);
        return d;
    }

    // ==================== SSRF 防护 1：只接受 host:port ====================

    @Test
    @DisplayName("防护1：带 scheme / path / query / fragment / userinfo 的输入一律拒绝")
    void rejectsUrlShapedInput() {
        for (String bad : List.of(
                "http://10.0.0.1:80",
                "https://10.0.0.1:443",
                "10.0.0.1:8621/readyz",
                "10.0.0.1:8621/../../etc/passwd",
                "10.0.0.1:8621?a=b",
                "10.0.0.1:8621#frag",
                "user@10.0.0.1:8621",
                "evil.com@10.0.0.1:8621",
                "10.0.0.1:8621\\x",
                "10.0.0.1 :8621")) {
            ProbeRejectedException e = assertThrows(ProbeRejectedException.class,
                    () -> ModelProbeController.parseAndValidateEndpoint(bad), "should reject: " + bad);
            assertEquals(400, e.getCode());
        }
    }

    @Test
    @DisplayName("防护1：空 / null 拒绝")
    void rejectsBlank() {
        assertThrows(ProbeRejectedException.class, () -> ModelProbeController.parseAndValidateEndpoint(null));
        assertThrows(ProbeRejectedException.class, () -> ModelProbeController.parseAndValidateEndpoint("   "));
    }

    // ==================== SSRF 防护 2：私有段白名单 ====================

    @Test
    @DisplayName("防护2：公网地址一律拒绝")
    void rejectsPublicAddresses() {
        for (String bad : List.of("8.8.8.8:80", "1.1.1.1:443", "172.32.0.1:80", "192.169.0.1:80",
                "11.0.0.1:80", "[2001:4860:4860::8888]:80")) {
            ProbeRejectedException e = assertThrows(ProbeRejectedException.class,
                    () -> ModelProbeController.parseAndValidateEndpoint(bad), "should reject: " + bad);
            assertEquals(400, e.getCode());
            assertTrue(e.getMessage().contains("只允许探测内网地址"), e.getMessage());
        }
    }

    @Test
    @DisplayName("防护2：私有段放行")
    void acceptsPrivateAddresses() {
        for (String good : List.of("10.0.0.1:8621", "172.16.0.1:8621", "172.31.255.254:8621",
                "192.168.1.50:8621", "127.0.0.1:8621", "169.254.1.1:8621", "[::1]:8621",
                "[fd00::1]:8621")) {
            assertNotNull(ModelProbeController.parseAndValidateEndpoint(good), good);
        }
    }

    @Test
    @DisplayName("防护2：边界 —— 172.15/172.32 不在 172.16/12 内")
    void privateRangeBoundaries() {
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("172.16.0.0")));
        assertFalse(ModelProbeController.isAllowedPrivateAddress(literal("172.15.255.255")));
        assertFalse(ModelProbeController.isAllowedPrivateAddress(literal("172.32.0.0")));
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("172.31.255.255")));
    }

    @Test
    @DisplayName("防护2：Tailscale/CGNAT 段（100.64/10）放行，公网仍拒绝")
    void acceptsTailscaleSharedAddressSpace() {
        // 边缘设备常常只能通过 Tailscale 触达，实测 orin-nx = 100.82.225.102。
        // 100.64.0.0/10 是 RFC 6598 保留段，公网不可路由，放行不等于开放公网。
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("100.82.225.102")));
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("100.64.0.0")));
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("100.127.255.255")));
        // 边界外仍是公网
        assertFalse(ModelProbeController.isAllowedPrivateAddress(literal("100.63.255.255")));
        assertFalse(ModelProbeController.isAllowedPrivateAddress(literal("100.128.0.0")));
        assertNotNull(ModelProbeController.parseAndValidateEndpoint("100.82.225.102:8621"));
    }

    @Test
    @DisplayName("防护2：云元数据端点即使落在链路本地段也必须拒绝")
    void rejectsCloudMetadataAddresses() {
        // 这几个地址都在 169.254/16 / fc00::/7 里，不单独拉黑就会被放行，
        // 而它们正是 SSRF 最经典的提权目标（读实例角色临时凭证）。
        for (String meta : List.of("169.254.169.254", "169.254.170.2", "fd00:ec2::254")) {
            assertTrue(ModelProbeController.isCloudMetadataAddress(literal(meta)), meta);
            assertFalse(ModelProbeController.isAllowedPrivateAddress(literal(meta)), meta);
        }
        // 同段内的邻居地址不受影响，别误伤正常的链路本地设备
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("169.254.169.253")));
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("169.254.170.3")));
        assertTrue(ModelProbeController.isAllowedPrivateAddress(literal("fd00:ec2::253")));

        assertThrows(ModelProbeController.ProbeRejectedException.class,
                () -> ModelProbeController.parseAndValidateEndpoint("169.254.169.254:80"));
    }

    private static InetAddress literal(String ip) {
        try {
            return InetAddress.getByName(ip);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    // ==================== SSRF 防护 3：DNS rebinding ====================

    @Test
    @DisplayName("防护3：域名解析后复校验，并 pin 住解析结果做实际连接")
    void resolvesAndPinsHostname() {
        // localhost 解析到 127.0.0.1 / ::1，都是私有段 → 放行，且 connectHost 是 IP 字面量
        ModelProbeController.Endpoint ep = ModelProbeController.parseAndValidateEndpoint("localhost:8621");
        assertEquals("localhost", ep.requestedHost());
        assertTrue(ep.connectHost().equals("127.0.0.1") || ep.connectHost().equals("0:0:0:0:0:0:0:1"),
                ep.connectHost());
        assertTrue(ep.resolvedFromName());
    }

    @Test
    @DisplayName("防护3：解析到公网 IP 的域名被拒")
    void rejectsHostnameResolvingToPublic() {
        ProbeRejectedException e = assertThrows(ProbeRejectedException.class,
                () -> ModelProbeController.parseAndValidateEndpoint("dns.google:443"));
        assertEquals(400, e.getCode());
    }

    // ==================== SSRF 防护 4：端口 ====================

    @Test
    @DisplayName("防护4：端口 0 / 越界 / 非数字 / 缺失 一律拒绝")
    void rejectsBadPorts() {
        for (String bad : List.of("127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:-1",
                "127.0.0.1:99999999", "127.0.0.1:abc", "127.0.0.1:", "127.0.0.1")) {
            ProbeRejectedException e = assertThrows(ProbeRejectedException.class,
                    () -> ModelProbeController.parseAndValidateEndpoint(bad), "should reject: " + bad);
            assertEquals(400, e.getCode());
        }
        assertEquals(1, ModelProbeController.parseAndValidateEndpoint("127.0.0.1:1").port());
        assertEquals(65535, ModelProbeController.parseAndValidateEndpoint("127.0.0.1:65535").port());
    }

    // ==================== 探测项枚举 ====================

    @Test
    @DisplayName("probe 必须是注册表里的枚举，任意值被拒")
    void rejectsUnknownProbe() {
        Result<Object> r = controller.probe(dto("../../etc/passwd", "127.0.0.1:" + port, null));
        assertEquals(400, r.getCode());
        assertTrue(r.getMsg().contains("未知的探测项"));
        assertEquals(List.of("ovs_voice", "ovs_tts_speakers", "edgellm_models"),
                ModelProbeController.supportedProbes());
    }

    // ==================== 成功路径（mock server） ====================

    @Test
    @DisplayName("ovs_voice：装配 ready / asrBackend / ttsModelId / sampleRate / speakers")
    @SuppressWarnings("unchecked")
    void probeOvsVoiceSuccess() {
        Result<Object> r = controller.probe(dto("ovs_voice", "127.0.0.1:" + port, "sk-test"));
        assertEquals(0, r.getCode(), r.getMsg());
        Map<String, Object> data = (Map<String, Object>) r.getData();

        assertEquals(Boolean.TRUE, data.get("ready"));
        assertEquals("funasr", data.get("asrBackend"));
        assertEquals(16000, data.get("sampleRate"));
        assertEquals("spark-tts-0.5b", data.get("ttsModelId"));
        assertEquals(Boolean.TRUE, data.get("supportsVoiceCloning"));
        assertEquals(0, data.get("defaultSpeakerId"));

        List<Map<String, Object>> speakers = (List<Map<String, Object>>) data.get("speakers");
        assertEquals(2, speakers.size());
        assertEquals("女声", speakers.get(0).get("label"));
        assertEquals("preset", speakers.get(0).get("type"));
        assertEquals("alice", speakers.get(1).get("label"));
        // 截断的 embedding / preset payload 绝不外泄
        for (Map<String, Object> s : speakers) {
            assertEquals(List.of("id", "label", "type"), List.copyOf(s.keySet()));
        }
        // apiKey 以 Authorization: Bearer 下发
        assertEquals("Bearer sk-test", lastAuthHeader.get("asr"));
    }

    @Test
    @DisplayName("ovs_voice：/readyz 冷启动 503 会重试，最多 3 次")
    @SuppressWarnings("unchecked")
    void probeOvsVoiceRetriesReadyz() {
        readyzNotReadyTimes = 2; // 前两次 503，第三次 200
        Result<Object> r = controller.probe(dto("ovs_voice", "127.0.0.1:" + port, null));
        assertEquals(0, r.getCode(), r.getMsg());
        Map<String, Object> data = (Map<String, Object>) r.getData();
        assertEquals(Boolean.TRUE, data.get("ready"));
        assertEquals(3, readyzHits.get());
    }

    @Test
    @DisplayName("ovs_tts_speakers：只回 defaultSpeakerId + 清洗过的 speakers")
    @SuppressWarnings("unchecked")
    void probeSpeakersSuccess() {
        Result<Object> r = controller.probe(dto("ovs_tts_speakers", "127.0.0.1:" + port, null));
        assertEquals(0, r.getCode(), r.getMsg());
        Map<String, Object> data = (Map<String, Object>) r.getData();
        assertEquals(List.of("defaultSpeakerId", "speakers"), List.copyOf(data.keySet()));
        assertEquals(2, ((List<Object>) data.get("speakers")).size());
    }

    @Test
    @DisplayName("edgellm_models：/v1/models 映射成 models[{id}]")
    @SuppressWarnings("unchecked")
    void probeEdgeLlmSuccess() {
        Result<Object> r = controller.probe(dto("edgellm_models", "127.0.0.1:" + port, null));
        assertEquals(0, r.getCode(), r.getMsg());
        Map<String, Object> data = (Map<String, Object>) r.getData();
        List<Map<String, Object>> models = (List<Map<String, Object>>) data.get("models");
        assertEquals(2, models.size());
        assertEquals("qwen3-4b", models.get(0).get("id"));
        assertEquals(List.of("id"), List.copyOf(models.get(0).keySet()));
    }

    // ==================== 失败路径 ====================

    @Test
    @DisplayName("连不上时 code=503 且 HTTP 层仍返回 Result（由 Spring 包成 200）")
    void unreachableGivesServiceUnavailable() {
        // 65534 上没有服务
        Result<Object> r = controller.probe(dto("edgellm_models", "127.0.0.1:65534", null));
        assertEquals(503, r.getCode());
        assertTrue(r.getMsg().contains("无法连接"), r.getMsg());
    }
}
