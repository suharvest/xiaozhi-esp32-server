# 套餐三改造：从单模块切到智控台

对象：`sensecraft-solutions/solutions/smart_warehouse` 的**套餐三 · 顶配版**。

目标：客户下发后**开机直接可用** —— 不需要 SSH 改 yaml、不需要手工挑模型。

---

## 1. 现状与目标的差距

套餐三现在部署的是**单模块**形态：compose 里只有 `xiaozhi-server` + `mcp-endpoint`，
没有智控台（无 MySQL / Redis / manager-api），配置来自
`assets/docker/configs/config-edge-computing.yaml` 的模板替换。

| | 现状（单模块） | 目标（智控台） |
|---|---|---|
| 配置来源 | `data/.config.yaml` 模板替换 | 数据库 + 网页 |
| ASR | `SherpaASR`（本地 sherpa-onnx，模型约 1GB） | `OpenVoiceStream`（设备上跑） |
| TTS | `RemoteTTSStream` → Jetson Kokoro | `OpenVoiceStream` |
| LLM | `JetsonLLM` → MLC LLM `qwen3:8b` | `EdgeLLM`（TensorRT） |
| 主机输入 | **一个** `jetson_ip`（LLM/TTS 共用） | **两个**：语音主机 `:8621` + LLM 主机 `:8000` |
| 网页配置 | 无 | 有（含地址探测、音色下拉） |

---

## 2. 要改的文件

### 2.1 `assets/docker/docker-compose-xiaozhi.yml`

补上智控台三件套，保留原有两个服务：

```
xiaozhi-esp32-server-web    manager-api + manager-web   18002:8002
xiaozhi-esp32-server-db     MySQL
xiaozhi-esp32-server-redis  Redis
xiaozhi-server              保留                        18000:8000 / 18003:8003
mcp-endpoint                保留                        18004:8004
```

⚠️ **镜像必须换成我们自建的**。上游 `web_latest` 不含 OVS/EdgeLLM 供应商、
仓库助手角色模板、`/models/probe` 探测端点这些 changeset。

`models/` 挂载**保留但瘦身**：`snakers4_silero-vad`（6.9M）必须留 —— VAD 在
xiaozhi-server 本地跑，OVS 不负责；`sherpa-onnx-sense-voice`（约 1GB）可以去掉，
识别已经搬到设备上。

### 2.2 `devices/xiaozhi_deploy.yaml`

- `jetson_ip` 拆成 `voice_ai_host`（OVS：ASR+TTS+声纹）和 `llm_host`（EdgeLLM）；
  `llm_host` 留空时回落到 `voice_ai_host`，单机部署不用填两遍
- 智控台模式下删除 `llm_base_url` / `llm_model_name` / `llm_api_key` /
  `tts_type` / `tts_base_url` / `tts_api_key` / `tts_voice` —— 这些改成在网页里配
- `services` / `pre_checks` 端口补 18002
- `post_deployment.message` 改成智控台地址

⚠️ **两处必须双写**：该文件的 `user_inputs` 在顶层（local 视图）和
`remote_overrides` 里各有一份完全重复的定义。

### 2.3 `assets/docker/configs/config-edge-computing.yaml`

智控台模式下大幅瘦身，模型配置全部搬进数据库：

```yaml
server:
  ip: 0.0.0.0
  port: 8000
  http_port: 8003
  websocket: ws://{{R1100_IP}}:18000/xiaozhi/v1/
read_config_from_api: true
manager-api:
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi
  secret: {{SERVER_SECRET}}
```

VAD / ASR / TTS / LLM / Intent / prompt 各段全部删除（DB 里已由 changeset 种好）。

---

## 3. 两个时序难题

### 3.1 `server.secret` 的先有鸡还是先有蛋

`config_from_api.yaml` 原文警告：「**每次从零部署，server.secret 都会变化**」。
它由 manager-api 首次启动时随机生成写入 `sys_params`
（`SysParamsServiceImpl.java:207-210`），而 xiaozhi-server 必须拿到它才能读配置。

**方案 A（已选）**：部署前自己生成，两边同源注入。

```yaml
actions:
  before:
    - name: xiaozhi-generate-server-secret
      run: |
        SECRET=$(cat /proc/sys/kernel/random/uuid)
        echo "SERVER_SECRET=$SECRET" >> .env
        mkdir -p data
        sed "s|{{SERVER_SECRET}}|$SECRET|" <配置模板> > data/.config.yaml
```

随机 UUID，无固定密钥的安全问题，且彻底绕开「启动后才知道」的死循环。

### 3.2 用户填的设备地址进不了数据库

智控台模式下模型地址在 DB 里，而 changeset 种的是 `127.0.0.1` 占位符。
用户在部署器里填的 `voice_ai_host` / `llm_host` 必须写进去。

**方案（已选）**：`actions.after` 调智控台 API，即手工验证过的那三次 PUT：

```
PUT /xiaozhi/models/ASR/openvoicestream/ASR_OpenVoiceStream
    config_json.ws_url   = ws://<voice_ai_host>:8621/asr/stream
PUT /xiaozhi/models/TTS/openvoicestream_tts/TTS_OpenVoiceStream
    config_json.base_url = http://<voice_ai_host>:8621
PUT /xiaozhi/models/LLM/openai/LLM_EdgeLLM
    config_json.base_url = http://<llm_host>:8000/v1
```

走 API 而非直接改 DB：模型配置有缓存（`getModelByIdFromCache`），直接改库会让
运行中的服务读到旧值。

---

## 4. 部署器契约上的坑（写 action 时必须绕开）

1. **`remote_overrides.actions` 是整体替换，不是合并**
   （`sensecraft-solutions/spec/CONTRACT.md:169-175`）。套餐三默认走远程部署，
   写在顶层的 actions 会被静默替换掉，只有一条 `logger.warning` 兜底。
   → 我们的 action 必须在两处都写，或只写在 `remote_overrides` 里。
2. **`device_class` profile 的 actions 会被前置并按 `name` 去重**（`:181-185`）。
   我们的 action 名字要够独特，别跟 profile 撞（故上面用了 `xiaozhi-` 前缀）。
3. **`compose_dir` 对 `..` 路径的规范化**（`:162-167`）。当前 compose 路径无 `..`，
   暂不受影响；后续拆文件时注意。

### 已确认（引擎在 `app_collaboration/provisioning_station/`）

**Q1 — `run:` 正文直接支持 `{{变量}}`，不用绕 `env`。**
`deployers/action_executor.py:264` 即 `cmd = _substitute(action.run, context)`，
整段脚本执行前做替换。`env` 的 value（`:278`）和 `copy.src`/`copy.dest`
（`:346-347`）同样支持。

变量来源（`deployers/base.py:88-101` `_build_action_context`）：先填
`user_inputs[*].default`，再被 `connection` 覆盖。可用内置键：`host` / `port` /
`username` / `password` / `key_file`，加上用户实际填写的所有 `user_inputs` id。

⚠️ **`{{remote_path}}` 不可用** —— 未注入 action context。现有方案的做法是手写
全路径，如 `industrial_security_jetson/devices/deploy.yaml:185` 的
`/home/{{username}}/industrial-security-demo/...`。我们照此办理。

**Q2 — 远程部署时 `before` / `after_upload` / `after` 全部在边缘设备上执行。**
`deployers/docker_remote_deployer.py` 全文只构造 `SSHActionExecutor`
（`:688` before、`:752` after_upload、`:1149` after 共用同一个），最终走 paramiko
`exec_command`。本地部署则三个钩子都用 `LocalActionExecutor`
（`docker_deployer.py:316,317,326,474`）。

→ **`actions.after` 里 `curl http://localhost:18002/...` 的 localhost 就是边缘设备
本身，方案成立。**

### 引擎实现上的坑（写 action 时逐条绕开）

1. **未命中的 `{{xxx}}` 静默替换成空串**（`utils/template.py:22-25`：
   `if value is None: return ""`），不报错。变量名拼错会得到
   `curl http://:8621/...` 这种畸形命令。
   → 脚本开头必须自检：`[ -n "{{voice_ai_host}}" ] || { echo "..."; exit 1; }`
2. **`after` 在健康检查通过之后才跑**（`docker_remote_deployer.py:1145-1149`），
   任一 required service 未就绪则 `return False`，`after` 根本不执行。
   → 好处是不会在半残状态写配置；但**健康检查必须覆盖智控台**，否则 after 可能
   在 manager-api 尚未就绪时就调 API。
3. **SSH 下 `cwd` 被忽略**（`action_executor.py:281-283`）→ 远程脚本写绝对路径。
4. **多行 `run` 自动前置 `set -e`**（`:35-61`）→ curl 要 `|| true` 或
   `ignore_error: true`，否则一个非零退出整段失败。
5. **`sudo: true` 仅在 connection 带 password 时生效**（`:298`），key-only 登录时
   sudo 包装被静默跳过。
6. **`timeout` 默认 300s**（`models/device.py:475`），超时直接判失败。
7. **`actions.custom` 从未被任何 deployer 执行** —— 写了也白写。
8. **`remote_overrides.actions` 整体替换**（`services/solution_manager.py:373-379`）。
   套餐三默认走远程 → 我们的 actions **只写在 `remote_overrides` 里**，不要两头写。

---

## 5. 仍然需要手工的步骤

**步骤 6（联动智能体）保留手工**：MCP 接入点地址是**按 agent 生成**的
（token = AES(key, agentId)），而 agent 要用户自己建（需绑定具体设备）。
agent 不存在，地址就不存在，无法在部署时预先串好。

可优化方向（未做）：
1. 智控台加一键复制按钮，把「粘贴长 token」降级成「点两下」
2. 仓管系统直接调智控台 `/agent/mcp/address/{id}` 取地址，用户只需选 agent
   —— 但这要动仓管系统，是跨仓库的活

---

## 6. 交付验收项

- [ ] 全新库跑完迁移后，OVS/EdgeLLM 供应商与仓库助手模板的**中文不是 `?`**
      （本地 `mvn spring-boot:run` 执行迁移会把中文写成 `?`，容器内正常 —— 根因
      未定位，必须在交付镜像上复验）
- [ ] 智控台首页可访问，本地模型排在各列表第一位
- [ ] 音色下拉能从真实设备拉到音色
- [ ] 每个 agent 能生成各自的 MCP 接入点地址
- [ ] 真实对话跑通（ASR → LLM → TTS 全链路）
