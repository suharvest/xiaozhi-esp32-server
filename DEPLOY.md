# 树莓派 ARM64 Docker 部署指南

在 Raspberry Pi (ARM64) 上部署 xiaozhi-esp32-server + MCP 接入点。

## 架构概览

| 组件 | 方案 | 端口 |
|------|------|------|
| VAD | Silero VAD (ONNX + NumPy) | - |
| ASR | Sherpa-ONNX SenseVoice | - |
| LLM | OpenAI Compatible（用户自行配置） | - |
| TTS | Sherpa-ONNX VITS (中文) | - |
| 主服务 | xiaozhi-esp32-server | 8000 (WS) / 8003 (HTTP) |
| MCP 接入点 | mcp-endpoint-server | 8004 |

镜像去掉了 PyTorch/torchaudio/funasr 依赖，体积约 800MB。

## 前置要求

- Raspberry Pi 4/5 (ARM64, 4GB+ RAM)
- Raspberry Pi OS (64-bit)
- Docker 已安装
- 磁盘空间 >= 5GB

## 部署步骤

### 1. 构建主服务镜像

```bash
cd ~/xiaozhi-esp32-server
docker build -t xiaozhi-server:arm64 -f Dockerfile-server-arm64 .
```

### 2. 拉取 MCP 接入点镜像

```bash
docker pull ghcr.nju.edu.cn/xinnan-tech/mcp-endpoint-server:latest
```

### 3. 准备模型文件

```bash
mkdir -p ~/xiaozhi-deploy/models

# TTS 模型（vits-icefall-zh-aishell3，约 100MB）
cd ~/xiaozhi-deploy/models
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-icefall-zh-aishell3.tar.bz2
tar xjf vits-icefall-zh-aishell3.tar.bz2
rm vits-icefall-zh-aishell3.tar.bz2

# VAD 模型（已包含在镜像中）
# ASR 模型（sherpa-onnx 首次启动时自动下载，或手动准备）
```

ASR 模型手动下载（可选）：
```bash
cd ~/xiaozhi-deploy/models
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
tar xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
rm sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
```

### 4. 配置服务

```bash
mkdir -p ~/xiaozhi-deploy/data ~/xiaozhi-deploy/tmp
cp ~/xiaozhi-esp32-server/deploy/data/.config.yaml ~/xiaozhi-deploy/data/.config.yaml
cp ~/xiaozhi-esp32-server/deploy/docker-compose.yml ~/xiaozhi-deploy/docker-compose.yml
```

编辑 `~/xiaozhi-deploy/data/.config.yaml`，至少修改以下内容：

```yaml
# LLM 配置（必须修改）
LLM:
  LocalLLM:
    type: openai
    base_url: http://你的LLM服务地址:端口/v1
    model_name: 你的模型名称
    api_key: 你的API密钥

# MCP 接入点 token（如需使用）
mcp_endpoint: ws://mcp-endpoint:8004/mcp_endpoint/mcp/?token=你的token
```

### 5. 启动服务

```bash
cd ~/xiaozhi-deploy
docker compose up -d
```

### 6. 验证

```bash
# 查看日志
docker logs -f xiaozhi-server

# 测试 WebSocket 连接
# 用浏览器访问 http://192.168.10.179:8003/xiaozhi/ota/

# 测试 MCP 接入点
curl http://192.168.10.179:8004/health
```

## 目录结构

```
~/xiaozhi-deploy/
├── docker-compose.yml
├── data/
│   └── .config.yaml          # 服务配置
├── models/
│   ├── vits-icefall-zh-aishell3/   # TTS 模型
│   └── sherpa-onnx-sense-voice-*/  # ASR 模型
└── tmp/                       # 临时音频文件
```

## 配置说明

### TTS 配置项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| model_dir | models/vits-icefall-zh-aishell3 | 模型目录 |
| model_file | model.onnx | 模型文件名 |
| tokens_file | tokens.txt | 词表文件 |
| lexicon_file | lexicon.txt | 词典文件 |
| dict_dir | dict | 字典目录 |
| sid | 0 | 说话人 ID（aishell3 多说话人） |
| speed | 1.0 | 语速 |
| num_threads | 2 | 推理线程数 |

### VAD 配置项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| threshold | 0.5 | 语音检测上阈值 |
| threshold_low | 0.3 | 语音检测下阈值 |
| min_silence_duration_ms | 200 | 静默判定时长(ms) |

## 常见问题

### 镜像构建失败
- 检查网络连接（pip install 需要访问 PyPI）
- 确认磁盘空间足够（构建时需要约 2GB 临时空间）

### TTS 无声音输出
- 确认模型文件完整（dict/ 目录下应有多个文件）
- 检查 sid 参数是否在有效范围内

### ASR 识别不准
- 确认使用了 sense_voice 模型类型
- 检查音频采样率是否为 16000Hz

### MCP 接入点连接失败
- 确认 token 配置正确
- 检查容器网络（两个容器应在同一 docker network）

## 停止和清理

```bash
cd ~/xiaozhi-deploy
docker compose down        # 停止服务
docker compose down -v     # 停止并删除卷
docker rmi xiaozhi-server:arm64  # 删除镜像
```
