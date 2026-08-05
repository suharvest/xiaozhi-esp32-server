# 本地语音服务接入设计 —— 填 `ip:port` 自动探测

目标：智控台里填一个 `ip:port`，点「检测」，自动回填 ASR / TTS / 声纹三个 provider 的
地址，音色与 LLM 模型变成下拉，连通性有明确反馈。

对接对象：OpenVoiceStream（`seeed-local-voice`）+ EdgeLLM（`edge-llm-chat-service`）。

**本轮不做**：模型切换（需要 `OVS_ADMIN_KEY`，见 §7）、声音克隆录入、多租户。

---

## 1. 为什么要改：现状的三个硬约束

1. **表单渲染层把一切塌缩成文本框。** `ai_model_provider.fields` 声明了 5 种 type，
   但 `ModelEditDialog.vue:308-318` 只映射 `dict`→JSON 文本域、`password`→密码框，
   其余一律 `text`。没有下拉、没有数字/开关控件、没有校验、没有联动，
   `default` 声明了也不读。
2. **唯一的探测先例绑死在角色页。** `OvsTtsController` + `roleConfig.vue` 的音色下拉
   是可用范式，但它靠「已保存的 modelId 反查 base_url」做 SSRF 防护 ——
   **填 IP 阶段模型还没保存，这套防护用不了**（见 §4）。
3. **配错不报错。** 三个 provider 构造函数都不阻塞、失败仅 WARN。
   用户看到的是「说话没反应 / 没声音 / 对话报错」，没有任何一处说「地址填错了」。

---

## 2. 数据模型：`fields` schema 扩展

现状每个 field 只有 `{key, type, label}`。扩展为：

```json
{
  "key": "speaker_id",
  "type": "remote-select",
  "label": "音色",
  "default": null,
  "required": false,
  "placeholder": "点击右侧按钮从设备拉取",
  "options": [{ "label": "…", "value": "…" }],
  "optionsFrom": {
    "probe": "ovs_tts_speakers",
    "dependsOn": "base_url",
    "labelKey": "label",
    "valueKey": "id"
  },
  "showWhen": { "field": "type", "equals": "openvoicestream_tts" }
}
```

新增 type：`select` / `remote-select` / `number` / `boolean` / `url`。

**向后兼容是硬要求**：上游已有几十个 provider 的 fields 里没有这些键，
缺省行为必须与现在完全一致（渲染成 text）。任何改动不得让存量 provider 变样。

---

## 3. 前端：抽 `DynamicField.vue`，上游文件只留一行

```
components/DynamicField.vue        ← 新文件，我们自有，永不冲突（~120 行）
  按 type 分发：
    text / password / json-textarea  保持现有行为（含敏感字段掩码钩子）
    number   → el-input-number
    boolean  → el-switch
    url      → el-input + 格式校验
    select   → el-select(options)
    remote-select → el-select + 刷新按钮，触发 probe
```

上游文件的净足迹（约 40~60 行）：

| 文件 | 改动 |
|---|---|
| `ModelEditDialog.vue` | 渲染块 `68-92`（25 行）→ 一行 `<dynamic-field>`；类型映射 `308-318` 停止塌缩，原样透传 |
| `AddModelDialog.vue` | 渲染块 `78-82`（5 行）→ 一行；类型映射 `160-164` 同上 |
| `ProviderDialog.vue` | 字段类型下拉补新选项（~3 行） |

⚠️ **唯一容易漏的地方**：`ModelEditDialog.vue` 的敏感字段掩码逻辑
（`isSensitiveField` / `handleInputFocus` / `handleInputBlur`）现在是嵌在那个 inline
`<el-input>` 上的。抽组件时必须通过 props/events 透出去。`AddModelDialog.vue` 没有
这套逻辑。

---

## 4. 后端：探测端点 + 新的 SSRF 防护

### 4.1 端点

```
POST /models/probe
{ "probe": "ovs_voice", "endpoint": "192.168.1.50:8621", "apiKey": "" }
```

`probe` 是**后端注册表里的枚举**，不是用户可传的 URL 或路径：

| probe id | 打什么 | 返回 |
|---|---|---|
| `ovs_voice` | `/readyz` → `/asr/capabilities` + `/tts/capabilities` + `/tts/speakers` | `ready, asrBackend, ttsModelId, sampleRate, speakers[], defaultSpeakerId, supportsVoiceCloning` |
| `ovs_tts_speakers` | `/tts/speakers` | `speakers[], defaultSpeakerId` |
| `edgellm_models` | `/v1/models` | `models[]` |

### 4.2 SSRF 防护（本设计唯一有安全含量的部分）

现有的「modelId 反查白名单」在探测阶段不可用，换成：

1. **只接受 `host:port`，不接受完整 URL** —— 不收 scheme / path / query / userinfo /
   `@`。从源头砍掉绝大部分注入面。路径由后端注册表里的字面量拼。
2. **目标必须是私有地址段**：`10/8`、`172.16/12`、`192.168/16`、`127/8`、`::1`、
   `fd00::/8`，或运维显式配置的白名单域名。**拒绝公网地址** —— 本地语音服务本来
   就该在内网，这个限制不牺牲任何真实场景。
3. **DNS rebinding 防护**：域名解析后再校验一次，解析出的 IP 必须仍在私有段。
4. 端口范围 `1024-65535`；禁止跟随重定向；超时 5s；响应体大小上限。
5. 仅 `superAdmin` 可调。

---

## 5. 交互流程

```
语音服务  [ 192.168.1.50:8621 ]  [检测]
LLM 服务  [ 192.168.1.50:8000 ]  [检测]
```

点「检测」后：

1. 后端先打 `/readyz`，**最多轮询 3 次 × 2s** —— 覆盖 `LAZY_TTS=1` 时 TTS 懒加载
   导致能力端点返 503 的冷启动窗口。不轮询的话，用户填完立刻点大概率拿到空。
2. 再并发拉能力端点与音色表。
3. 前端回填：

| 目标 | 自动填出 |
|---|---|
| ASR provider | `ws_url = ws://192.168.1.50:8621/asr/stream`、`sample_rate` = 探测值（不猜） |
| TTS provider | `base_url = http://192.168.1.50:8621`、`speaker_id` 下拉（默认选中 `defaultSpeakerId`） |
| 声纹 | `embedding_url = http://192.168.1.50:8621/speaker/embedding` |
| LLM provider | `base_url = http://192.168.1.50:8000/v1`、`model_name` 下拉 |

4. 只读展示：当前 ASR backend / TTS `model_id` / 采样率。

**一个输入框喂三个 provider** —— ASR、TTS、声纹都在同一个 OVS 进程同一个端口
（容器内 `:8000`，宿主默认 `:8621`）。LLM 是独立容器，必须单独填。

---

## 6. 「测试连接」不能撒谎

探测通过 ≠ 能用。三条各自的真实性验证：

- **LLM**：必须打一次真实的 `POST /v1/chat/completions`（`max_tokens: 1`）。
  依据是 `edge-llm-chat-service/DOCKERFILE_PLAN.md:28` 自己写的：
  「warmup 必须调用真实 `/v1/chat/completions`，因为 `/v1/models` 不能发现当前这类
  MHA kernel/runtime 崩溃」。只查 metadata 是给假信心。
- **TTS**：合成一小段短文本，确认真出音，而不是只看 `/tts/capabilities` 说 ready。
- **ASR**：能力端点 + WS 握手（不必发音频）。注意**鉴权失败是 accept 之后 close 4401，
  不是 HTTP 401**，前端要单独处理这个分支。

---

## 7. 已知的能力天花板（写进文档，别让现场以为是 bug）

- **「这台机器装了哪些模型」探测不到。** OVS 同一时刻只有一个 profile 激活，
  候选清单在 `/admin/backend/loadable`，走 **admin 独立鉴权**（loopback 放行，
  远程必须配 `OVS_ADMIN_KEY`）。所以自动检测只能告诉你「当前跑的是哪个」，
  不能列出可切换项。若交付时需要「在页面上换模型」，必须一并配 admin key。
- **`OVS_API_KEYS` 一开，探测全瞎。** 所有能力端点 401，唯一免 key 的 `/health`
  不返回 `model_id` 也不返回 `speakers`。若客户要开鉴权，表单必须让用户连 key 一起填。
- **换 TTS 模型后音色 id 完全不可迁移**（Qwen3 是 `0/2301/2302`，Kokoro 是 `0-52`，
  CustomVoice 是四位数）。前端不得缓存音色列表，换模型必须重拉。

---

## 8. Tier 0：不修就白做（与本设计同批完成）

这几条不修的话，自动探测做出来也是坏的：

1. **`AgentDao.xml:21`** resultMap 列名写成 `tts_speaker_id`，而 SQL 别名是
   `ttsSpeakerId` → `AgentInfoVO.ttsSpeakerId` 恒为 null。该 resultMap 含
   `<collection>` 嵌套，MyBatis 默认 `PARTIAL` 关掉了 automapping，兜不了底。
   后果：音色选了存不上；且角色页保存任何字段都会把已存的 `tts_speaker_id` 抹成 NULL。
2. **`sid` 默认值 `0` 改成留空。** `_to_optional_int(0)` 返回 `0` 而非 `None`
   （`openvoicestream_tts.py:19-39`），而 payload 优先级是
   `speaker_embedding_b64 > speaker_id > sid` —— `sid=0` 会**永远压住** `speaker_id`。
3. **`seeed-providers.sql` 转成 liquibase changeset。** 它自述「NOT a liquibase
   changelog」且确实不在 `db.changelog-master.yaml` 里 → 客户装完，智控台里根本
   看不到 OVS/EdgeLLM 供应商，要人工进数据库执行。
4. **去掉硬编码的开发机地址。** `seeed-providers.sql:29,32,35` 三处写着
   `http://<设备IP>:8621`（Tailscale 段内地址）。
5. **provider fields 补 `speaker_id`。** 目前只暴露了 `sid`，`speaker_id` 只能靠
   agent 的 `tts_speaker_id` 注入，配置面上缺一块。

另：provider 代码内置默认端口是 `8000`（`openvoicestream.py:40`、
`openvoicestream_tts.py:57`），而 OVS 实际宿主端口是 `8621`。探测回填后不依赖默认值，
但默认值本身也该改对，免得留空时静默连错。

---

## 9. 实施顺序

| Phase | 内容 | 谁做 |
|---|---|---|
| 0 | Tier 0 五条 | 主线程（改动小、都在关键路径） |
| 1 | `fields` schema 扩展 + `DynamicField.vue` + 三个上游文件接线 | 执行体 |
| 2 | `/models/probe` 端点 + SSRF 防护 | 执行体实现，安全部分主线程审 |
| 3 | OVS/EdgeLLM 的 fields 定义接线 + 端到端联调 | 主线程 |
| 4（Tier 2） | 「测试连接」按钮、配置导出/导入 | 待定 |

---
---

# 第二部分：xiaozhi-server 侧加固设计

前半部分讲的是智控台怎么把配置**填对**。这半部分讲 xiaozhi-server 拿到配置之后
**能不能可靠地跑起来、跑挂了能不能被发现**。这是交付现场真正会翻车的地方。

## 10. 阻断级问题（不做，客户现场必翻车）

### 10.1 ASR / TTS 完全不支持 API Key —— 开鉴权即全废

实测 grep 结果：

| 文件 | `api_key` 出现次数 |
|---|---|
| `core/api/voiceprint_adapter_handler.py` | 7 |
| `core/providers/asr/openvoicestream.py` | **0** |
| `core/providers/tts/openvoicestream_tts.py` | **0** |

声纹适配层做了鉴权，ASR/TTS 一个字都没有。后果：客户一旦设 `OVS_API_KEYS`，
**ASR 的 WS 会在 accept 之后被 close 4401、TTS 全部 401**，整套语音直接失效。
而我们的日志只会说「连接失败」，不会说「你没带 key」。

**设计**：

- ASR：`websockets.connect(..., additional_headers={"Authorization": f"Bearer {key}"})`。
  OVS 也接受 `?token=`（`main.py` ws 鉴权在 accept 前走 `check_ws`），
  但 header 更干净、不会落进日志里的 URL。
- TTS：`session.post(..., headers={"Authorization": f"Bearer {key}"})`。
- 配置项统一命名 `api_key`，与声纹的 `embedding_api_key` 保持同一套取值来源，
  避免同一台 OVS 要在三个地方填三次 key。

### 10.2 429 裸奔

`_post_with_503_retry`（`openvoicestream_tts.py:209-228`）函数名就写死了只认 503：
`if resp.status != 503: return resp` —— 429 被当成正常响应返回。

而 429 恰恰是 `/tts/stream` 多句死锁的**次生故障面**（slot 被占死后所有请求返 429，
OVS 还带了 `Retry-After` 头）。

**设计**：泛化成 `_post_with_retry`，同时处理 503（热重载）与 429（会话满）。
429 分支优先读 `Retry-After` 头，没有就退避。重试上限与 503 分开计数。

### 10.3 失败静默 —— 全程没有一处告诉用户「地址填错了」

三个 provider 的构造函数都不阻塞，只 fire-and-forget 探一次能力端点，失败仅 WARN
（`openvoicestream.py:70-78,110-111`；`openvoicestream_tts.py:96-102,258-259`）。
ASR 连接失败走 `_cleanup()` 后直接 `return`（`openvoicestream.py:270-278`），
**用户表现为说话完全没反应**，且每句重试一次、无退避、无重连。

**设计**：

1. **启动门控**（可配 `startup_check: true`）：服务起来时对每个已选中的 OVS provider
   打一次 `/readyz`，失败在日志里给**可操作**的信息（目标地址、HTTP 状态、
   最可能的原因），而不是一句 `probe failed`。
2. **运行时健康状态**：provider 内部维护 `last_probe_ok / last_error / consecutive_failures`。
3. **暴露出去**（见 §13）。

## 11. 健壮性

| 问题 | 现状 | 设计 |
|---|---|---|
| ASR 无重连/退避 | 连接失败即丢一句，下一句重来 | 单句内重试 2 次 + 指数退避；连续失败 N 次标记 unhealthy 并上报 |
| TTS 只有总超时 | `timeout: 30` 一个总超时（`:78,279`）。死锁时用户干等 30 秒 | 拆成**首字节超时**（建议 5s）+ 总超时。首字节迟迟不来就是后端卡住了，早失败早反馈 |
| 冷启动竞态 | 构造时 fire-and-forget 探 `/capabilities`，`LAZY_TTS=1` 时必然拿到 503 | 改用 `/readyz` 轮询门控，`/capabilities` 只用于记录信息 |

## 12. 配置面清理

- **默认端口错**：`openvoicestream.py:40` 与 `openvoicestream_tts.py:57` 内置默认都是
  `:8000`，OVS 实际宿主端口是 `:8621`。留空必然连不上 → 改成 8621。
- **`sid` 默认 `0` 要改成留空**：`_to_optional_int(0)` 返回 `0` 而非 `None`
  （`:19-39`），payload 优先级是 `speaker_embedding_b64 > speaker_id > sid`，
  所以 `sid=0` 会**永远压住** `speaker_id`。
- **`partial_results` 是死配置**：代码注释自认
  「the `partial_results` flag is reserved for future use」（`openvoicestream.py:208-210`），
  配了完全没作用。**要么实现，要么从 provider fields 里拿掉** ——
  给客户一个配了没反应的开关，是交付里最伤信任的细节。

## 13. 可观测性：`/api/health/providers`

现在没有**任何**地方能看出「OVS 连不上」。现场只能靠猜。

**设计**：xiaozhi-server 的 aiohttp 服务（`core/http_server.py`，已经挂着
`/mcp/vision/explain`、`/api/speaker/*`、`/voiceprint/*`）新增：

```
GET /api/health/providers
→ {
    "asr": {"type":"openvoicestream","endpoint":"ws://…:8621/asr/stream",
            "ok":true,"backend":"paraformer","last_error":null,"checked_at":…},
    "tts": {"type":"openvoicestream_tts","endpoint":"http://…:8621",
            "ok":false,"last_error":"401 Unauthorized","checked_at":…},
    "llm": {...}, "voiceprint": {...}
  }
```

一份数据三处复用：现场排查（curl 一下就知道谁挂了）、智控台的「测试连接」按钮
（§6 那个真实性验证可以打这个端点）、以及交付验收脚本。

## 14. server 侧实施顺序

| # | 内容 | 依赖 |
|---|---|---|
| S1 | ASR/TTS 加 `api_key` 支持 | 无 |
| S2 | 429 + `Retry-After` 退避重试 | 无 |
| S3 | 默认端口 8621、`sid` 默认留空、`partial_results` 处置 | 无 |
| S4 | `/readyz` 启动门控 + provider 健康状态字段 | 无 |
| S5 | ASR 重连退避、TTS 首字节超时 | S4 |
| S6 | `GET /api/health/providers` | S4 |

S1~S3 是纯增量、风险低，先做。S4~S6 动到连接生命周期，要配合真机验证。

---
---

# 第三部分：配置校验 —— 让「配错了」当场可见

## 15. 现状：唯一的校验只检查中文「你」

`VLLMProvider.__init__`（`core/providers/vllm/openai.py:37`）里唯一的检查是
`check_model_key("VLLM", self.api_key)`，而它的全部实现是（`core/utils/util.py:133-136`）：

```python
def check_model_key(modelType, modelKey):
    if "你" in modelKey:
        return f"配置错误: {modelType} 的 API key 未设置,当前值为: {modelKey}"
    return None
```

只为识别占位符 `你的api_key`。填 `abc` 通过、填空串通过、填过期的真 key 通过、
`base_url` 指向不存在的地址也通过。而 `openai.OpenAI(...)` 构造不发任何请求
—— **provider 实例化成功 ≠ 能用**。

ASR/TTS/LLM/VLLM 四个 provider 是同一个模式：构造不阻塞、失败只 WARN。
**全系统没有任何一处会主动说「这个配置是错的」。**

## 16. 每类 provider 的「强校验」定义

弱校验（只探能力端点 / `/v1/models`）一律不接受 —— 它给的是**假阳性**，
而假阳性的代价是客户在现场付的。每类都必须做一次真实往返：

| 类型 | 强校验动作 | 为什么弱校验不够 |
|---|---|---|
| **LLM** | `POST /v1/chat/completions`，`max_tokens: 1` | `edge-llm-chat-service/DOCKERFILE_PLAN.md:28` 自述：`/v1/models` 发现不了 MHA kernel/runtime 崩溃 |
| **VLLM** | 发一张几十字节的纯色小图 + 「描述这张图」，`max_tokens: 1` | 很多 OpenAI 兼容端点**接受 `image_url` 字段但底层模型没有视觉能力**，只查 `/v1/models` 必得假阳性 |
| **TTS** | 合成一段极短文本，确认真出 PCM | `/tts/capabilities` 说 ready 只代表 backend 装载了，不代表能出音 |
| **ASR** | 能力端点 + WS 握手（不必发音频） | 鉴权失败是 accept 后 close 4401，HTTP 层探不出来 |
| **声纹** | `POST /speaker/embedding` 传一小段静音 PCM | 模型未装时该端点返 503，只有真调才知道 |

代价是每次校验一次真实推理（云端几分钱，本地几百毫秒）。值得。

## 17. 交互：保存即校验，失败不阻断

不要做成「有个测试按钮，等你想起来点」—— 没人会点。

```
[保存] ──► 后台跑一次强校验
            ├─ 通过：正常保存，列表里显示 ✅
            └─ 失败：**仍然保存**，但列表该行标 ⚠️，
                     悬停显示具体原因（401 / 连不上 / 模型不支持视觉 / 503 未就绪）
```

**失败不阻断保存**是刻意的：客户常常先填配置、服务还没起来，硬拦会让人抓狂。
但 ⚠️ 会一直挂着直到校验通过，于是「哪个配置是坏的」变成一眼可见，
而不是等设备不响应了再回头猜。

数据来源与 §13 的 `GET /api/health/providers` 是同一份：
智控台读它渲染 ⚠️，现场 curl 它排查。校验结果带时间戳，避免把一次陈旧的失败
一直显示成当前状态。

## 18. 待定：不用视觉时应否停止广播 vision 能力

`send_mcp_initialize_message`（`core/providers/tools/device_mcp/mcp_handler.py:251-274`）
**无条件**把 vision 能力下发给设备：

```python
vision_url = get_vision_url(conn.config)   # 全程不检查 VLLM 是否配置
...
"capabilities": {..., "vision": vision}
```

后果：未配 VLLM 时能力照样广播 → 设备注册摄像头工具 → LLM 看得见就会调 →
拍照 → `POST /mcp/vision/explain` → `vision_handler.py:114` 抛
「您还未设置默认的视觉分析模块」。**故障点在最后一公里**，前面每一步都以为自己是对的。

建议：`selected_module.VLLM` 为空时不下发 `vision` 能力（几行）。方向上与仓管系统
那条「透明 gate」教训一致 —— **不该有的能力就别暴露给 LLM，否则它一定会去用**。

⏳ 等确认本方案是否需要视觉问答后再实施。人脸链路不走 VLLM（仓管后端直连设备
拉图/拉身份），若只做刷脸出入库，视觉能力可直接关闭。

---
---

# 第四部分：回填规则

## 19. 探测结果落到表单：按「候选个数 + 有无服务端默认」分三种

回填是自动的（不要多一步「确认」点击），但**绝不允许在多候选且没有默认值时替用户猜**。
猜错的代价是：配置看起来填好了、保存也成功、直到设备实际跑起来才发现用错了模型，
而那时没人会想到回头怀疑这个自动填的值。

| 情况 | 处理 | 例子 |
|---|---|---|
| **恰好 1 个候选** | 直接填入，字段仍可改 | 某台机器只装了一个 LLM |
| **多个候选 + 服务端给了默认** | 填入默认值，字段渲染成下拉，用户可切换 | `/tts/speakers` 返回 `default_speaker_id`，按它选中 |
| **多个候选 + 没有默认** | **不填**。字段留空 + 渲染成下拉 + 自动展开，横幅里计入「待选择 N 项」 | `/v1/models` 返回 3 个模型，协议里没有「默认模型」概念 |
| **0 个候选** | 不填，字段旁内联提示「服务端未返回可用项」 | TTS 后端未就绪 / 模型没装 |

## 20. 覆盖已有值：先比对，再覆盖，可撤销

1. **探测前对整个 `configJson` 做一次快照。**
2. 逐字段决定：
   - 用户当前值**在候选列表里** → **保持不动**。用户显然是有意填的，别用「默认值」把他的选择冲掉。
   - 用户当前值不在候选里（或为空）→ 按 §19 的规则填。
3. 顶部横幅汇总本次回填：
   ```
   已根据探测结果填入 5 项，覆盖 2 项，待选择 1 项    [撤销回填]
   ```
4. **[撤销回填]** 一键还原到步骤 1 的快照。有这个按钮，自动覆盖就不吓人了 ——
   代价只有一个快照对象，却消除了「它把我刚填的东西冲了」这类不信任感。

横幅是**内联**的，不是 toast：toast 会自己消失，而用户需要在填完整张表之前一直看得到
「哪些是机器填的、还有哪一项要我选」。
