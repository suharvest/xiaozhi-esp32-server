# 小智语音助手 - 部署指南

## 项目概述

基于 xiaozhi-esp32-server 的语音助手服务，部署在 Raspberry Pi (ARM64) 上，配合 Jetson 设备提供 GPU 加速的 LLM 和 TTS 服务。

### 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      ESP32 / 客户端                              │
│                    (WebSocket 连接)                              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Raspberry Pi (192.168.10.179)                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              xiaozhi-server 容器                          │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐     │  │
│  │  │   VAD   │  │   ASR   │  │   LLM   │  │   TTS   │     │  │
│  │  │ Silero  │  │ Sherpa  │  │ Remote  │  │ Remote  │     │  │
│  │  │ (本地)  │  │ (本地)  │  │ (远程)  │  │ (远程)  │     │  │
│  │  └─────────┘  └─────────┘  └────┬────┘  └────┬────┘     │  │
│  └─────────────────────────────────┼────────────┼──────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │           mcp-endpoint-server 容器                        │  │
│  │              (MCP 工具接入点)                              │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬────────────┬─────────────────┘
                                  │            │
                                  ▼            ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Jetson (192.168.10.35)                        │
│  ┌─────────────────────────┐  ┌─────────────────────────────┐  │
│  │   MLC-LLM Server        │  │   TTS Server (MeloTTS)      │  │
│  │   Qwen3:8b (GPU)        │  │   VITS 中文 (GPU)           │  │
│  │   Port: 8000            │  │   Port: 8000/tts            │  │
│  └─────────────────────────┘  └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 硬件配置

| 设备 | IP 地址 | 角色 | 规格 |
|------|---------|------|------|
| Raspberry Pi | 192.168.10.179 | 主服务器 | ARM64, 4GB RAM |
| Jetson | 192.168.10.35 | GPU 推理 | Orin/Xavier, GPU |

---

## Docker 镜像信息

### RPi 上的镜像

| 镜像名 | Tag | 大小 | 说明 |
|--------|-----|------|------|
| `xiaozhi-server` | `arm64` | 916MB | 主服务 |
| `mcp-endpoint-server` | `latest` | - | MCP 接入点 |

### 容器端口映射

| 容器 | 内部端口 | 外部端口 | 协议 | 用途 |
|------|----------|----------|------|------|
| xiaozhi-server | 8000 | 18000 | WebSocket | 语音交互 |
| xiaozhi-server | 8003 | 18003 | HTTP | OTA/Vision API |
| mcp-endpoint-server | 8004 | 18004 | WebSocket | MCP 工具 |

---

## 目录结构

```
~/xiaozhi-deploy/
├── data/
│   └── .config.yaml          # 主配置文件
├── models/
│   ├── sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/  # ASR 模型
│   ├── snakers4_silero-vad/   # VAD 模型
│   ├── vits-icefall-zh-aishell3/   # TTS 备用模型
│   └── vits-melo-tts-zh_en/   # TTS 本地模型 (备用)
└── tmp/                       # 临时文件
```

---

## 模块配置

### 当前启用的模块

| 模块 | 选择 | 运行位置 | 说明 |
|------|------|----------|------|
| VAD | SileroVAD | RPi 本地 | 语音活动检测 |
| ASR | SherpaASR | RPi 本地 | 语音识别 (SenseVoice) |
| LLM | MLC_Qwen3 | Jetson 远程 | 大语言模型 (Qwen3:8b) |
| TTS | RemoteTTS | Jetson 远程 | 语音合成 (MeloTTS) |
| Memory | nomem | - | 无记忆 |
| Intent | function_call | - | 函数调用模式 |

### 可切换选项

```yaml
# 在 .config.yaml 的 selected_module 中切换

# LLM 选项:
#   - MLC_Qwen3    (推荐, Jetson GPU)
#   - Ollama_Qwen3 (备用, Jetson Ollama)

# TTS 选项:
#   - RemoteTTS      (推荐, Jetson GPU, RTF~0.1)
#   - SherpaOnnxTTS  (备用, RPi CPU, RTF~0.8)
```

---

## API 接口

### WebSocket 语音交互

```
ws://192.168.10.179:18000/xiaozhi/v1/
```

#### 连接流程

```python
import websockets
import json

async def connect():
    headers = {"device-id": "my-device-001"}
    async with websockets.connect(
        "ws://192.168.10.179:18000/xiaozhi/v1/",
        additional_headers=headers
    ) as ws:
        # 1. 发送 Hello
        await ws.send(json.dumps({
            "type": "hello",
            "device_id": "my-device-001",
            "device_name": "My Device",
            "device_mac": "my-device-001"
        }))

        # 2. 接收响应
        resp = await ws.recv()

        # 3. 发送文本消息
        await ws.send(json.dumps({
            "type": "listen",
            "state": "detect",
            "text": "你好"
        }))

        # 4. 接收音频流
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                # Opus 音频数据
                play_audio(msg)
            else:
                data = json.loads(msg)
                if data.get("type") == "tts" and data.get("state") == "stop":
                    break
```

### HTTP API

#### OTA 接口
```
GET http://192.168.10.179:18003/xiaozhi/ota/
```

#### 视觉分析接口
```
POST http://192.168.10.179:18003/mcp/vision/explain
```

---

## 性能指标

### 延迟测试结果 (Warm 状态)

| 查询类型 | 首音频延迟 | 完整响应 | 首段内容 |
|----------|-----------|----------|----------|
| 短句 "你好" | ~1.0s | ~4s | "你好呀" (3字) |
| 长句 "请介绍人工智能" | ~0.8s | ~18s | "人工智能就像是" (7字) |

### 各模块性能

| 模块 | 指标 | 数值 |
|------|------|------|
| ASR | RTF | 0.07 |
| ASR | 推理时间 | ~150ms |
| LLM | TTFT | ~200ms |
| LLM | 吞吐量 | ~17 tok/s |
| TTS (Remote) | RTF | 0.1-0.2 |
| TTS (Local) | RTF | 0.8 |

---

## 部署步骤

### 方式一：镜像导出导入 (推荐)

#### 1. 在源机器导出镜像

```bash
# 导出 xiaozhi-server 镜像
docker save xiaozhi-server:arm64 | gzip > xiaozhi-server-arm64.tar.gz

# 导出 mcp-endpoint-server 镜像
docker save ghcr.nju.edu.cn/xinnan-tech/mcp-endpoint-server:latest | gzip > mcp-endpoint-server.tar.gz
```

#### 2. 传输到目标机器

```bash
scp xiaozhi-server-arm64.tar.gz user@target-host:~/
scp mcp-endpoint-server.tar.gz user@target-host:~/
```

#### 3. 在目标机器导入镜像

```bash
# 导入镜像
gunzip -c xiaozhi-server-arm64.tar.gz | docker load
gunzip -c mcp-endpoint-server.tar.gz | docker load

# 验证
docker images
```

#### 4. 准备目录和配置

```bash
# 创建目录
mkdir -p ~/xiaozhi-deploy/{data,models,tmp}

# 复制模型文件 (从源机器)
scp -r source-host:~/xiaozhi-deploy/models/* ~/xiaozhi-deploy/models/

# 复制配置文件
scp source-host:~/xiaozhi-deploy/data/.config.yaml ~/xiaozhi-deploy/data/
```

#### 5. 修改配置

编辑 `~/xiaozhi-deploy/data/.config.yaml`，更新以下 IP 地址：

```yaml
server:
  websocket: ws://<新RPi IP>:18000/xiaozhi/v1/
  vision_explain: http://<新RPi IP>:18003/mcp/vision/explain

TTS:
  RemoteTTS:
    base_url: http://<Jetson IP>:8000

LLM:
  MLC_Qwen3:
    base_url: http://<Jetson IP>:8000/v1
```

#### 6. 启动容器

```bash
# 创建 Docker 网络
docker network create xiaozhi-net

# 启动 mcp-endpoint-server
docker run -d \
  --name mcp-endpoint-server \
  --network xiaozhi-net \
  -p 18004:8004 \
  --restart always \
  ghcr.nju.edu.cn/xinnan-tech/mcp-endpoint-server:latest

# 启动 xiaozhi-server
docker run -d \
  --name xiaozhi-server \
  --network xiaozhi-net \
  -p 18000:8000 \
  -p 18003:8003 \
  -v ~/xiaozhi-deploy/data:/opt/xiaozhi-esp32-server/data \
  -v ~/xiaozhi-deploy/models:/opt/xiaozhi-esp32-server/models \
  -v ~/xiaozhi-deploy/tmp:/opt/xiaozhi-esp32-server/tmp \
  --restart always \
  xiaozhi-server:arm64
```

#### 7. 验证部署

```bash
# 检查容器状态
docker ps

# 检查日志
docker logs -f xiaozhi-server

# 测试 WebSocket 连接
python3 -c "
import asyncio
import websockets
import json

async def test():
    async with websockets.connect('ws://localhost:18000/xiaozhi/v1/',
                                   additional_headers={'device-id': 'test'}) as ws:
        await ws.send(json.dumps({'type': 'hello', 'device_id': 'test',
                                  'device_name': 'Test', 'device_mac': 'test'}))
        print(await ws.recv())
        print('Connection OK!')

asyncio.run(test())
"
```

---

## Docker Compose 部署 (可选)

创建 `~/xiaozhi-deploy/docker-compose.yml`:

```yaml
version: '3.8'

services:
  xiaozhi-server:
    image: xiaozhi-server:arm64
    container_name: xiaozhi-server
    restart: always
    ports:
      - "18000:8000"
      - "18003:8003"
    volumes:
      - ./data:/opt/xiaozhi-esp32-server/data
      - ./models:/opt/xiaozhi-esp32-server/models
      - ./tmp:/opt/xiaozhi-esp32-server/tmp
    networks:
      - xiaozhi-net
    depends_on:
      - mcp-endpoint

  mcp-endpoint:
    image: ghcr.nju.edu.cn/xinnan-tech/mcp-endpoint-server:latest
    container_name: mcp-endpoint-server
    restart: always
    ports:
      - "18004:8004"
    networks:
      - xiaozhi-net

networks:
  xiaozhi-net:
    driver: bridge
```

启动:
```bash
cd ~/xiaozhi-deploy
docker compose up -d
```

---

## 常用运维命令

```bash
# 查看日志
docker logs -f xiaozhi-server --tail 100

# 重启服务
docker restart xiaozhi-server

# 进入容器
docker exec -it xiaozhi-server bash

# 更新配置后重启
docker restart xiaozhi-server

# 查看资源使用
docker stats xiaozhi-server mcp-endpoint-server

# 完整重建
docker compose down && docker compose up -d
```

---

## 故障排查

### 1. WebSocket 连接失败

```bash
# 检查端口监听
netstat -tlnp | grep 18000

# 检查容器网络
docker network inspect xiaozhi-net
```

### 2. LLM/TTS 无响应

```bash
# 测试 Jetson 连接
curl http://192.168.10.35:8000/v1/models

# 测试 TTS
curl -X POST http://192.168.10.35:8000/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "测试", "sid": 0}' \
  -o test.wav
```

### 3. 首音频延迟过高

检查配置中的 `first_sentence_max_chars` 值，降低可减少延迟：
- 当前值: 5
- 可调范围: 3-10

---

## 依赖服务

### Jetson 端需要运行

1. **MLC-LLM Server** (Port 8000)
   - 模型: Qwen3-8B-q4f16_1-MLC
   - 提供 OpenAI 兼容 API

2. **TTS Server** (Port 8000/tts)
   - 模型: MeloTTS VITS 中文
   - 提供 HTTP TTS API

---

## 版本信息

- xiaozhi-esp32-server: 0.8.11
- sherpa-onnx: 1.12.17
- Python: 3.10
- 架构: ARM64 (aarch64)

---

## 联系与支持

项目仓库: [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)
