package xiaozhi.modules.ovs.controller;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import cn.hutool.core.util.StrUtil;
import cn.hutool.http.HttpRequest;
import cn.hutool.http.HttpResponse;
import cn.hutool.json.JSONArray;
import cn.hutool.json.JSONObject;
import cn.hutool.json.JSONUtil;
import io.swagger.v3.oas.annotations.tags.Tag;
import xiaozhi.common.utils.Result;

@RestController
@RequestMapping("/ovs/tts")
@Tag(name = "OpenVoiceStream TTS")
public class OvsTtsController {

    @GetMapping("/speakers")
    @RequiresPermissions("sys:role:normal")
    public ResponseEntity<Result<List<Map<String, Object>>>> speakers(@RequestParam String baseUrl) {
        try {
            String url = StrUtil.removeSuffix(baseUrl, "/") + "/tts/capabilities";
            HttpResponse response = null;
            try {
                response = HttpRequest.get(url).timeout(5000).execute();
                if (!response.isOk()) {
                    return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                            .body(new Result<List<Map<String, Object>>>().error(503, "OVS unavailable"));
                }
                JSONObject body = JSONUtil.parseObj(response.body());
                JSONArray rawSpeakers = body.getJSONArray("speakers");
                List<Map<String, Object>> speakers = new ArrayList<>();
                if (rawSpeakers != null) {
                    for (Object item : rawSpeakers) {
                        if (!(item instanceof JSONObject)) {
                            continue;
                        }
                        JSONObject raw = (JSONObject) item;
                        Map<String, Object> entry = new HashMap<>();
                        entry.put("id", raw.get("id"));
                        entry.put("type", raw.getOrDefault("type", "preset"));
                        Object label = raw.get("label");
                        if (label == null) {
                            label = raw.get("name");
                        }
                        entry.put("label", label);
                        speakers.add(entry);
                    }
                }
                return ResponseEntity.ok(new Result<List<Map<String, Object>>>().ok(speakers));
            } finally {
                if (response != null) {
                    response.close();
                }
            }
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(new Result<List<Map<String, Object>>>().error(503, "OVS unavailable: " + e.getMessage()));
        }
    }
}
