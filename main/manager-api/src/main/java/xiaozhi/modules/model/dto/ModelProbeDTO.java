package xiaozhi.modules.model.dto;

import java.io.Serializable;

import io.swagger.v3.oas.annotations.media.Schema;
import lombok.Data;

/**
 * 本地语音 / LLM 服务探测请求。
 *
 * <p>
 * 三个字段都不是「地址」：{@code probe} 是后端注册表里的枚举（决定打哪些**字面量**路径），
 * {@code endpoint} 只能是 {@code host:port}（不接受 scheme / path / query / userinfo），
 * {@code apiKey} 只在服务端拼进 {@code Authorization: Bearer} 请求头。
 * </p>
 */
@Data
@Schema(description = "本地服务能力探测请求")
public class ModelProbeDTO implements Serializable {

    private static final long serialVersionUID = 1L;

    /** 探测项 ID，取值见 {@code ModelProbeController.ProbeType}。绝不是 URL 或路径。 */
    @Schema(description = "探测项：ovs_voice / ovs_tts_speakers / edgellm_models")
    private String probe;

    /** 目标地址，**只接受** {@code host:port}（如 {@code 192.168.1.50:8621}）。 */
    @Schema(description = "目标地址，形如 192.168.1.50:8621（不接受完整 URL）")
    private String endpoint;

    /** 可选 API Key，为空则不带鉴权头。 */
    @Schema(description = "可选 API Key，为空则不带鉴权头")
    private String apiKey;
}
