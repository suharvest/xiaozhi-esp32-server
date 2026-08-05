# 智慧仓储语音方案 —— 手工部署说明（不依赖 SenseCraft 部署工具）

本文面向**自行部署**的工程人员：不使用 SenseCraft App 的一键部署，直接用 `docker compose`
把整套跑起来。SenseCraft 的部署引擎所做的事，本文逐条展开成可直接执行的命令。

适用拓扑（双机）：

```
┌──────────────────────────────┐        ┌────────────────────────────────────┐
│  Jetson Orin NX              │        │  Raspberry Pi 5 + Hailo-8          │
│                              │        │                                    │
│  OpenVoiceStream  :8621      │◄───────┤  xiaozhi-server        :18000 (ws) │
│    ASR  (Qwen3/TensorRT)     │  局域网 │  智控台 manager-api    :18002      │
│    TTS  (Matcha)             │  /VPN  │  xiaozhi-server HTTP   :18003      │
│    声纹 embedding            │        │  MCP 接入点            :18004      │
│                              │        │                                    │
│  EdgeLLM          :8000      │◄───────┤  仓库管理系统          :2125       │
│    Qwen3.5-4B                │        │  人脸识别 (Hailo)      :8001       │
└──────────────────────────────┘        └────────────────────────────────────┘
                                                      ▲
                                                      │ WebSocket
                                        ┌─────────────┴──────────────┐
                                        │  小智 ESP32 / Watcher 设备  │
                                        └────────────────────────────┘
```

**为什么这么分**：语音和大模型吃 GPU，放 Orin NX；业务系统和人脸识别吃 Hailo NPU 与磁盘，
放 Pi。两台机器只通过 HTTP/WebSocket 通信，不共享文件系统。

---

## 0. 相关仓库与镜像

### 仓库

| 仓库 | 作用 | 说明 |
|---|---|---|
| [`suharvest/xiaozhi-esp32-server`](https://github.com/suharvest/xiaozhi-esp32-server) | 语音服务端 + 智控台 | 本仓库。上游是 `xinnan-tech/xiaozhi-esp32-server`，本 fork 增加了 OpenVoiceStream / EdgeLLM 供应商、地址自动探测、仓库助手角色模板等 |
| `warehouse_system` | 仓库管理系统 + MCP server | 提供出入库/库存/看板，并以 MCP 工具形式暴露给语音助手 |
| [`suharvest/openvoicestream`](https://github.com/suharvest/openvoicestream) | 本地语音栈（OVS） | ASR + TTS + 声纹，**同一进程同一端口**。自带安装器 |
| EdgeLLM | 本地大模型 | OpenAI 兼容接口，独立服务 |

### 镜像

均在 `sensecraft-missionpack.seeed.cn`。**该 registry 允许匿名拉取，不需要 `docker login`**
（已在无凭据设备上实测确认）。直接 `docker compose pull` 即可。

| 镜像 | 跑在 | 用途 |
|---|---|---|
| `solution/xiaozhi-server:arm64` | Pi | 语音服务端 |
| `solution/xiaozhi-manager:arm64` | Pi | 智控台（Web + API） |
| `solution/warehouse:latest` | Pi | 仓库管理系统 |
| `solution/face-rec-api:v1.1-hailo` | Pi | 人脸识别（Hailo 加速） |
| `mysql:latest` / `redis:8.0` | Pi | 智控台依赖 |
| `ghcr.nju.edu.cn/xinnan-tech/mcp-endpoint-server:latest` | Pi | MCP 接入点 |

> **Jetson 用 `face-rec-api:v1.1-jetson`**，与 Hailo 版是两个镜像，不能混用 ——
> 对应 `docker-compose.face-jetson.yml`。本文按 Hailo 走。

---

## 1. 前置条件

### Pi 5

- Docker ≥ 20，Docker Compose ≥ 2
- **可用磁盘 ≥ 6 GB**（实测整套占约 3.5 GB，留出余量给数据库与日志增长）
- Hailo-8 驱动已装，`/dev/hailo0` 存在：
  ```bash
  ls -l /dev/hailo0
  ```
  没有这个设备节点，人脸识别容器起不来。

### Orin NX

- NVIDIA Container Runtime 可用
- 可用磁盘 ≥ 15 GB（模型权重占大头）
- OpenVoiceStream 与 EdgeLLM 已部署并可访问（部署方式见各自仓库文档）

### 网络

Pi 必须能路由到 Orin NX 的 `:8621`（语音）和 `:8000`（LLM）。先验证：

```bash
# 在 Pi 上执行，<ORIN_IP> 换成实际地址
curl -s -o /dev/null -w '%{http_code}\n' http://<ORIN_IP>:8621/readyz
curl -s http://<ORIN_IP>:8000/v1/models | head -c 200
```

`/readyz` 返回 200 才继续。**注意**：OpenVoiceStream 在 `LAZY_TTS=1` 时 TTS 是懒加载的，
刚启动那几十秒能力端点会返 503，属正常，等一会儿再试。

---

## 1.5 Orin NX：语音栈

OVS 自带安装器，会自动识别机型并选择合适的 profile：

```bash
git clone https://github.com/suharvest/openvoicestream
cd openvoicestream
deploy/install.sh --target orin-nx --pull --verify
```

`--target orin-nx` 对应 v0.9.1 ASR（Qwen3 / TensorRT）+ Matcha TTS，profile 为
`jetson-edgellm-v091-matcha`。`--verify` 会在装完后自跑一次校验。

装完只有**一个容器**，宿主端口 **8621**（容器内 8000），ASR / TTS / 声纹都在它上面。

```bash
curl -s http://localhost:8621/readyz
curl -s http://localhost:8621/asr/capabilities
curl -s http://localhost:8621/tts/speakers | head -c 300
```

首次启动要下载模型权重，视网络可能需要较久；`OVS_AUTO_DOWNLOAD_ARTIFACTS=1` 会自动拉取，
默认走 `hf-mirror.com` 镜像。

> **TTS 是懒加载的**（`LAZY_TTS=1`）。刚启动时 `/tts/*` 会返 503，属正常，等模型加载完即可。
> 智控台的自动探测已经考虑了这一点（会轮询重试），但如果你手工 curl 撞上 503，等一会儿再试。

### EdgeLLM

对话大模型是**独立服务**，OpenAI 兼容接口，监听 **:8000**。部署方式见其自身文档。
本方案实测运行的是 `Qwen/Qwen3.5-4B`。验证：

```bash
curl -s http://localhost:8000/v1/models
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3.5-4B","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
```

> 只查 `/v1/models` 不足以判断可用 —— 它查的是元数据，发现不了推理运行时的崩溃。
> **必须打一次真实的 `/v1/chat/completions`**，返回 200 且有 `choices` 才算就绪。

---

## 2. Pi：仓库管理系统 + 人脸识别

这两个服务在同一份 compose 里。

```bash
mkdir -p ~/mcp_warehouse && cd ~/mcp_warehouse
# 放入 docker-compose.face-hailo.yml
docker compose -f docker-compose.face-hailo.yml -p mcp_warehouse up -d
```

包含两个服务：

| 服务 | 端口 | 备注 |
|---|---|---|
| `warehouse` | 2125 | Web 界面 + REST API |
| `face-rec` | 8001 | 映射 `/dev/hailo0` |

验证：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:2125
docker compose -p mcp_warehouse ps
```

浏览器打开 `http://<PI_IP>:2125` 完成仓库系统自身的初始化（建仓库、建用户、人脸录入等），
具体见 `warehouse_system` 仓库文档。

---

## 3. Pi：小智智控台 + 语音服务端

这是最容易出错的一段 —— SenseCraft 引擎在这里替你做了三段脚本，手工部署必须自己做。

### 3.1 目录与文件

```bash
mkdir -p ~/xiaozhi_voice/assets/docker/{configs,data}
cd ~/xiaozhi_voice/assets/docker
# 放入 docker-compose-xiaozhi-console.yml
# 放入 configs/config-console.yaml
```

> ⚠️ **compose 里的相对挂载路径是相对于 compose 文件所在目录解析的**，不是你执行
> `docker compose` 的目录。务必保持上面的目录结构，否则挂载会落在错误位置 ——
> 这是真机部署踩过的坑。

### 3.2 生成 `data/.config.yaml`

把 `configs/config-console.yaml` 复制为 `data/.config.yaml`，并替换其中的占位符：

```bash
cd ~/xiaozhi_voice/assets/docker
DEVICE_HOST=<PI_IP>          # 设备能路由到的 Pi 地址，不能写 127.0.0.1
sed -e "s|__DEVICE_HOST__|$DEVICE_HOST|g" \
    configs/config-console.yaml > data/.config.yaml
```

`__SERVER_SECRET__` **此刻先留着**，等智控台启动后在 3.4 回填。

### 3.3 启动

```bash
cd ~/xiaozhi_voice/assets/docker
docker compose -f docker-compose-xiaozhi-console.yml -p xiaozhi_voice pull
docker compose -f docker-compose-xiaozhi-console.yml -p xiaozhi_voice up -d
```

五个容器：

| 服务 | 宿主端口 | 说明 |
|---|---|---|
| `xiaozhi-esp32-server-web` | 18002 | 智控台 |
| `xiaozhi-esp32-server-db` | — | MySQL |
| `xiaozhi-esp32-server-redis` | — | Redis |
| `xiaozhi-server` | 18000 (ws) / 18003 (http) | 语音服务端 |
| `mcp-endpoint` | 18004 | MCP 接入点 |

首次启动智控台要跑 110+ 个 Liquibase changeset，**约 1～2 分钟**。等它就绪：

```bash
until curl -sf -o /dev/null http://localhost:18002/; do sleep 5; done; echo 就绪
```

### 3.4 回填 server.secret（**不做这步 xiaozhi-server 起不来**）

智控台启动时会自己生成 `server.secret` 写进数据库。`xiaozhi-server` 用它去智控台取配置，
两边不一致就会崩在「无效的服务器密钥」。

```bash
cd ~/xiaozhi_voice/assets/docker
DB=xiaozhi-esp32-server-db

SECRET=$(docker exec -i $DB mysql -uroot -p123456 -N -B \
  --default-character-set=utf8mb4 xiaozhi_esp32_server \
  -e "SELECT param_value FROM sys_params WHERE param_code='server.secret';" \
  2>/dev/null | tail -1 | tr -d '\r\n ')

echo "secret = $SECRET"      # 必须非空
sed -i "s|^  secret: .*|  secret: \"$SECRET\"|" data/.config.yaml
```

> 确认 `data/.config.yaml` 里已经不是 `__SERVER_SECRET__` 再往下走。

### 3.5 写入设备接入地址

设备（ESP32 / Watcher）拿到的地址必须是**它们能路由到的**，所以用 Pi 的实际 IP。

```bash
DEVICE_HOST=<PI_IP>
docker exec -i $DB mysql -uroot -p123456 --default-character-set=utf8mb4 \
  xiaozhi_esp32_server <<SQL
UPDATE sys_params SET param_value = 'ws://$DEVICE_HOST:18000/xiaozhi/v1/'
 WHERE param_code = 'server.websocket';
UPDATE sys_params SET param_value = 'http://$DEVICE_HOST:18002/xiaozhi/ota/'
 WHERE param_code = 'server.ota';
SQL
```

> **OTA 是 18002 不是 18003。** 智控台模式下 `xiaozhi-server` 并不注册 `/xiaozhi/ota/`
> 这个路由（只在脱离智控台单机跑时才注册），该地址由 manager-api 提供。写 18003 会 404。

### 3.6 配置 MCP 接入点

接入点的 key 是容器首次启动时随机生成的，只出现在它自己的日志里：

```bash
MCP_KEY=$(docker logs mcp-endpoint-server 2>&1 | grep -oE 'key=[a-f0-9]+' | head -1 | cut -d= -f2)
echo "key = $MCP_KEY"

docker exec -i $DB mysql -uroot -p123456 --default-character-set=utf8mb4 \
  xiaozhi_esp32_server <<SQL
UPDATE sys_params
   SET param_value = 'http://$DEVICE_HOST:18004/mcp_endpoint/health?key=$MCP_KEY'
 WHERE param_code = 'server.mcp_endpoint';
SQL
```

> 智控台对这个参数有硬校验：**不能含 `localhost` / `127.0.0.1`，且 URL 里必须含 key**，
> 否则保存时会被拒绝。

### 3.7 清缓存并重启

配置读进 Redis 后不会自动失效，必须清掉再让服务端重拉：

```bash
for PAT in 'model:data:*' 'server:config' 'sys:params'; do
  docker exec xiaozhi-esp32-server-redis redis-cli --scan --pattern "$PAT" \
    | xargs -r -n50 docker exec -i xiaozhi-esp32-server-redis redis-cli DEL
done
docker restart xiaozhi-server
```

> **这步别跳过。** 种子数据里模型地址默认是 `127.0.0.1`，缓存不清的话即使数据库
> 已经改成 Orin NX 的地址，服务端用的仍是旧值，而且**不会报任何错** —— 表现为语音
> 完全没反应但日志干净。

---

## 4. 智控台里的配置

浏览器打开 `http://<PI_IP>:18002`。

**默认账号 `admin`，密码 `Seeed@2026`。首次登录后请立即修改。**

### 4.1 填语音与 LLM 地址

进「模型配置」，本 fork 已经把三个本地供应商排在各自类别第一位：

| 类型 | 供应商 | 填什么 |
|---|---|---|
| ASR | OpenVoiceStream流式ASR(本地) | `ws://<ORIN_IP>:8621/asr/stream` |
| TTS | OpenVoiceStream流式TTS(本地) | `http://<ORIN_IP>:8621` |
| LLM | EdgeLLM (本地) | `http://<ORIN_IP>:8000/v1` |

**不必手工拼这些 URL** —— 表单里填 `<ORIN_IP>:8621` 后点「检测」，会自动探测并回填
地址、采样率、音色列表；LLM 侧同理会把模型列表拉成下拉。音色下拉展开时也会自动拉取。

探测失败时错误会内联显示在字段下方（不是弹窗），常见原因：

- 地址不通 → 检查 §1 的网络验证
- OVS 开了 `OVS_API_KEYS` → 表单里要连 API Key 一起填，否则所有能力端点 401
- 刚启动 → TTS 懒加载中，等几十秒重试

### 4.2 创建智能体

「智能体管理」→ 新建，角色模板选**「仓库智能助手」**（本 fork 预置，排在第一位，
六个模型全部已预填）。

### 4.3 挂上仓库系统的 MCP

每个智能体有**各自独立**的 MCP 接入点地址，在智能体配置页里查看复制，填到
`warehouse_system` 的 `mcp/config.yml` 里，然后启动它的 `mcp_pipe.py`。

具体见 `warehouse_system/mcp/README.md`。

---

## 5. 设备固件

ESP32（小智）与 Himax（视觉）固件的烧录方式见上游文档。烧录后需要**把设备绑定到
本地服务器**，否则它连的是公有云。

设备端配置的 OTA 地址填 §3.5 里写入的那个：`http://<PI_IP>:18002/xiaozhi/ota/`。

参考：<https://github.com/xinnan-tech/xiaozhi-esp32-server/blob/main/docs/firmware-setting.md>

---

## 6. 部署后验证清单

按顺序逐条过，任何一条不过就别往下走：

```bash
# 1. 五个容器都在，且 xiaozhi-server 不在反复重启
docker compose -p xiaozhi_voice ps
docker inspect --format '{{.RestartCount}}' xiaozhi-server        # 应为 0

# 2. 智控台可访问
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18002/xiaozhi/user/pub-config

# 3. OTA 路由在 18002 上活着
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18002/xiaozhi/ota/

# 4. secret 已回填（不该是占位符）
grep '^  secret:' ~/xiaozhi_voice/assets/docker/data/.config.yaml

# 5. 服务端确实连上了 Orin NX 的语音栈
docker logs xiaozhi-server 2>&1 | grep -iE 'OVS|ASR|TTS' | tail -20

# 6. 仓库系统与人脸识别
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:2125
docker compose -p mcp_warehouse ps
```

第 5 条应该能看到类似 `OVS ASR capabilities: backend=... capabilities=['streaming',...]`。
**日志里出现 `127.0.0.1:8621` 说明 §3.7 的缓存没清干净。**

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `xiaozhi-server` 反复重启，日志 `无效的服务器密钥` | §3.4 没做，或 `.config.yaml` 路径不对 | 确认改的是 `assets/docker/data/.config.yaml` |
| 语音完全无反应，日志却没有报错 | Redis 缓存没清，仍在用 `127.0.0.1` | 执行 §3.7 |
| 设备连不上，OTA 404 | `server.ota` 写成了 18003 | 改回 18002（§3.5） |
| 智控台保存 MCP 接入点被拒 | 地址含 `localhost`，或 URL 里没有 key | 见 §3.6 |
| 人脸识别容器起不来 | `/dev/hailo0` 不存在或没映射 | 装 Hailo 驱动；确认用的是 `-hailo` 镜像 |
| 探测语音服务报 401 | OVS 开了 `OVS_API_KEYS` | 表单里补填 API Key |
| 音色下拉是空的 | TTS 懒加载还没就绪 | 等待后点刷新按钮重拉 |
| 换了 TTS 模型后音色错乱 | 不同模型的音色 id 完全不通用 | 重新拉取音色列表并重选 |
| 容器显示 `unhealthy` 但功能正常 | 健康检查探的路由不对 | 确认用的是最新 compose |

---

## 8. 与 SenseCraft 一键部署的对应关系

如果你之后改用 SenseCraft App 部署，本文各节对应关系如下 —— 引擎做的就是这些事：

| 本文 | 引擎中的位置 |
|---|---|
| §3.1 目录结构 | 文件上传阶段 |
| §3.2 占位符替换 | `actions.before` |
| §3.3 启动 | compose pull / up 阶段 |
| §3.4 ~ §3.7 | `actions.after` |
| §4 之后 | 引导页的手工步骤 |
