package xiaozhi.modules.model.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import lombok.Data;

@Data
public class ModelBasicInfoDTO {
    private String id;
    private String modelName;

    @Schema(description = "model type, e.g. openvoicestream_tts")
    private String type;

    @Schema(description = "service base URL, only meaningful for remote providers like OpenVoiceStream")
    private String baseUrl;
}
