"""服务端MCP管理器"""

import asyncio
import os
import json
from typing import Dict, Any, List

from mcp.types import LoggingMessageNotificationParams

from config.config_loader import get_project_dir
from config.logger import setup_logging
from .mcp_client import ServerMCPClient

TAG = __name__
logger = setup_logging()


class ServerMCPManager:
    """管理多个服务端MCP服务的集中管理器"""

    def __init__(self, conn) -> None:
        """初始化MCP管理器"""
        self.conn = conn
        self.config_path = get_project_dir() + "data/.mcp_server_settings.json"
        if not os.path.exists(self.config_path):
            self.config_path = ""
            logger.bind(tag=TAG).warning(
                f"请检查mcp服务配置文件：data/.mcp_server_settings.json"
            )
        self.clients: Dict[str, ServerMCPClient] = {}
        self.tools = []
        self._init_lock = asyncio.Lock()

    def load_config(self) -> Dict[str, Any]:
        """加载MCP服务配置"""
        if len(self.config_path) == 0:
            return {}

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("mcpServers", {})
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Error loading MCP config from {self.config_path}: {e}"
            )
            return {}

    async def _init_server(self, name: str, srv_config: Dict[str, Any]):
        """初始化单个MCP服务"""
        client = None
        try:
            # 初始化服务端MCP客户端
            logger.bind(tag=TAG).info(f"初始化服务端MCP客户端: {name}")
            client = ServerMCPClient(srv_config)
            # 设置超时时间10秒
            await asyncio.wait_for(client.initialize(logging_callback=self.logging_callback), timeout=10)

            # 使用锁保护共享状态的修改
            async with self._init_lock:
                self.clients[name] = client
                client_tools = client.get_available_tools()
                self.tools.extend(client_tools)

        except asyncio.TimeoutError:
            logger.bind(tag=TAG).error(
                f"Failed to initialize MCP server {name}: Timeout"
            )
            if client:
                await client.cleanup()
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Failed to initialize MCP server {name}: {e}"
            )
            if client:
                await client.cleanup()

    async def initialize_servers(self) -> None:
        """初始化所有MCP服务"""
        config = self.load_config()
        tasks = []
        for name, srv_config in config.items():
            if not srv_config.get("command") and not srv_config.get("url"):
                logger.bind(tag=TAG).warning(
                    f"Skipping server {name}: neither command nor url specified"
                )
                continue
            
            tasks.append(self._init_server(name, srv_config))
        
        if tasks:
            await asyncio.gather(*tasks)

        # 输出当前支持的服务端MCP工具列表
        if hasattr(self.conn, "func_handler") and self.conn.func_handler:
            # 刷新工具缓存以确保服务端MCP工具被正确加载
            if hasattr(self.conn.func_handler, "tool_manager"):
                self.conn.func_handler.tool_manager.refresh_tools()
            self.conn.func_handler.current_support_functions()

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有服务的工具function定义"""
        return self.tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        """检查是否是MCP工具"""
        for tool in self.tools:
            if (
                tool.get("function") is not None
                and tool["function"].get("name") == tool_name
            ):
                return True
        return False

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """执行工具调用，失败时会尝试重新连接"""
        logger.bind(tag=TAG).info(f"执行服务端MCP工具 {tool_name}，参数: {arguments}")

        max_retries = 3  # 最大重试次数
        retry_interval = 2  # 重试间隔(秒)

        # 找到对应的客户端
        client_name = None
        target_client = None
        for name, client in self.clients.items():
            if client.has_tool(tool_name):
                client_name = name
                target_client = client
                break

        if not target_client:
            raise ValueError(f"工具 {tool_name} 在任意MCP服务中未找到")

        # 人脸校验注入：若该工具声明了 meta.requires_face，先用设备摄像头采集一帧，
        # 注入 face_image_b64 再发往服务端。任一步失败都直接抛错（绝不把空图透传，
        # 否则服务端会按 no_image 拒绝、语义混乱）。
        if target_client.requires_face(tool_name):
            await self._inject_device_face(tool_name, arguments)

        # 带重试机制的工具调用
        for attempt in range(max_retries):
            try:
                return await target_client.call_tool(tool_name, arguments, progress_callback=self.progress_callback)
            except Exception as e:
                # 最后一次尝试失败时直接抛出异常
                if attempt == max_retries - 1:
                    raise

                logger.bind(tag=TAG).warning(
                    f"执行工具 {tool_name} 失败 (尝试 {attempt+1}/{max_retries}): {e}"
                )

                # 尝试重新连接
                logger.bind(tag=TAG).info(
                    f"重试前尝试重新连接 MCP 客户端 {client_name}"
                )
                try:
                    # 关闭旧的连接
                    await target_client.cleanup()

                    # 重新初始化客户端
                    config = self.load_config()
                    if client_name in config:
                        client = ServerMCPClient(config[client_name])
                        await client.initialize(logging_callback=self.logging_callback)
                        self.clients[client_name] = client
                        target_client = client
                        logger.bind(tag=TAG).info(
                            f"成功重新连接 MCP 客户端: {client_name}"
                        )
                    else:
                        logger.bind(tag=TAG).error(
                            f"Cannot reconnect MCP client {client_name}: config not found"
                        )
                except Exception as reconnect_error:
                    logger.bind(tag=TAG).error(
                        f"Failed to reconnect MCP client {client_name}: {reconnect_error}"
                    )

                # 等待一段时间再重试
                await asyncio.sleep(retry_interval)

    # 设备端人脸工具名（在固件 sscma_camera.cc 注册）。sanitize 后 '.' → '_'，
    # 与设备 MCP 客户端缓存的键一致。
    _FACE_EMBEDDING_TOOL = "self.face.capture_embedding"  # 拓扑 B：设备 NPU 算 embedding
    _FACE_CAPTURE_TOOL = "self.camera.capture_raw"        # 拓扑 A：设备只拍图

    async def _inject_device_face(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """采集人脸凭证并注入到工具参数。自动选择拓扑：

        - 拓扑 B（端侧推理，优先）：设备暴露 capture_embedding → 注入
          face_embedding_b64 + face_model_tag，warehouse 本地比对（不调外部 /infer）。
        - 拓扑 A（云端推理，回退）：设备只暴露 capture_raw → 注入 face_image_b64，
          warehouse 侧调 face_rec_api 算 embedding。

        失败一律抛 RuntimeError —— 上层作为工具错误返回给 LLM，由 LLM 口播
        "人脸校验失败"，绝不静默放行。
        """
        from core.utils.util import sanitize_tool_name
        from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

        conn = self.conn
        mcp_client = getattr(conn, "mcp_client", None)
        if not mcp_client:
            raise RuntimeError("人脸校验需要设备摄像头，但当前设备未启用 MCP")
        if not await mcp_client.is_ready():
            raise RuntimeError("设备摄像头未就绪，无法完成人脸校验")

        embed_tool = sanitize_tool_name(self._FACE_EMBEDDING_TOOL)
        capture_tool = sanitize_tool_name(self._FACE_CAPTURE_TOOL)

        if mcp_client.has_tool(embed_tool):
            await self._inject_embedding(tool_name, arguments, mcp_client, embed_tool)
        elif mcp_client.has_tool(capture_tool):
            await self._inject_image(tool_name, arguments, mcp_client, capture_tool)
        else:
            raise RuntimeError("当前设备不支持人脸采集（缺少 capture_embedding / capture_raw 工具）")

    async def _inject_embedding(self, tool_name, arguments, mcp_client, embed_tool) -> None:
        """拓扑 B：设备端算好 embedding，直接注入。"""
        from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

        logger.bind(tag=TAG).info(f"工具 {tool_name} 需要人脸，端侧采集 embedding")
        # 设备单次推理 ~820ms（含切模式冷启），给 10s 余量。
        raw = await call_mcp_tool(self.conn, mcp_client, embed_tool, "{}", timeout=10)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            raise RuntimeError(f"设备 embedding 返回格式异常: {e}")
        if not isinstance(data, dict) or not data.get("ok"):
            err = data.get("error") if isinstance(data, dict) else "unknown"
            raise RuntimeError(f"设备人脸采集失败: {err}")
        emb = data.get("embedding_b64")
        if not emb:
            raise RuntimeError("设备返回的 embedding 为空")
        arguments["face_embedding_b64"] = emb
        # model_tag 让设备/warehouse 配置约定；设备未带时由 warehouse 用租户配置兜底。
        if data.get("model_tag"):
            arguments["face_model_tag"] = data["model_tag"]
        logger.bind(tag=TAG).info(
            f"已注入 face_embedding_b64（{len(emb)} chars）到工具 {tool_name}"
        )

    async def _inject_image(self, tool_name, arguments, mcp_client, capture_tool) -> None:
        """拓扑 A：设备只拍图，warehouse 侧算 embedding。"""
        from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

        logger.bind(tag=TAG).info(f"工具 {tool_name} 需要人脸，端侧拍照（云端推理）")
        raw = await call_mcp_tool(self.conn, mcp_client, capture_tool, "{}", timeout=10)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            image_b64 = data.get("image_b64") if isinstance(data, dict) else None
        except (json.JSONDecodeError, AttributeError) as e:
            raise RuntimeError(f"设备人脸采集返回格式异常: {e}")
        if not image_b64:
            raise RuntimeError("设备返回的人脸图为空")
        arguments["face_image_b64"] = image_b64
        logger.bind(tag=TAG).info(
            f"已注入 face_image_b64（{len(image_b64)} chars）到工具 {tool_name}"
        )

    async def cleanup_all(self) -> None:
        """关闭所有 MCP客户端"""
        for name, client in list(self.clients.items()):
            try:
                if hasattr(client, "cleanup"):
                    await asyncio.wait_for(client.cleanup(), timeout=20)
                logger.bind(tag=TAG).info(f"服务端MCP客户端已关闭: {name}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.bind(tag=TAG).error(f"关闭服务端MCP客户端 {name} 时出错: {e}")
        self.clients.clear()

    # 可选回调方法

    async def logging_callback(self, params: LoggingMessageNotificationParams):
        logger.bind(tag=TAG).info(f"[Server Log - {params.level.upper()}] {params.data}")

    async def progress_callback(self, progress: float, total: float | None, message: str | None) -> None:
        logger.bind(tag=TAG).info(f"[Progress {progress}/{total}]: {message}")