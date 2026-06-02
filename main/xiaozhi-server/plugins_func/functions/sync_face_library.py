"""Voice-triggered face-library sync (warehouse -> device).

When the user says e.g. "同步人脸库", the LLM calls this tool. xiaozhi then
pulls the warehouse face library over HTTP and pushes each entry to the
device-local DB via the device MCP tool ``self.face.add``. The embeddings
never pass through the LLM — they flow entirely inside this plugin
(HTTP in, device MCP out). xiaozhi is only a conduit; the library lives
on the device (passive greeting) and in the warehouse (source of truth).

Config (data/.config.yaml):
    face_sync:
      warehouse_url: "http://localhost:2124/api/face/library"
      api_key: "<warehouse X-API-Key>"
"""
import json
from typing import TYPE_CHECKING

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

sync_face_library_function_desc = {
    "type": "function",
    "function": {
        "name": "sync_face_library",
        "description": (
            "把服务器（仓库系统）上的人脸库同步到本设备本地，用于人脸识别打招呼。"
            "当用户说'同步人脸库'、'更新人脸库'、'刷新认识的人'时调用。"
            "无需参数。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


@register_function("sync_face_library", sync_face_library_function_desc, ToolType.SYSTEM_CTL)
def sync_face_library(conn: "ConnectionHandler"):
    # 设备必须已启用 MCP（否则没有 self.face.add 可推）
    mcp_client = getattr(conn, "mcp_client", None)
    if not mcp_client:
        return ActionResponse(
            action=Action.RESPONSE, result="device mcp absent",
            response="当前设备不支持人脸库同步",
        )
    if not conn.loop or not conn.loop.is_running():
        return ActionResponse(
            action=Action.RESPONSE, result="loop not running",
            response="系统繁忙，请稍后再试",
        )

    task = conn.loop.create_task(_do_sync(conn))

    def _done(f):
        try:
            f.result()
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"人脸库同步失败: {e}")

    task.add_done_callback(_done)
    return ActionResponse(
        action=Action.RECORD, result="sync started", response="正在同步人脸库到设备"
    )


async def _do_sync(conn: "ConnectionHandler"):
    import aiohttp
    from core.utils.util import sanitize_tool_name
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

    cfg = conn.config.get("face_sync", {}) if isinstance(conn.config, dict) else {}
    url = cfg.get("warehouse_url")
    api_key = cfg.get("api_key", "")
    if not url:
        conn.logger.bind(tag=TAG).error("face_sync.warehouse_url 未配置")
        return

    # 1. 拉 warehouse 人脸库（embedding 全程在 xiaozhi 内部，不经 LLM）
    headers = {"X-API-Key": api_key} if api_key else {}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                conn.logger.bind(tag=TAG).error(f"拉取人脸库失败 HTTP {resp.status}")
                return
            faces = await resp.json()

    if not isinstance(faces, list) or not faces:
        conn.logger.bind(tag=TAG).info("warehouse 人脸库为空，无需同步")
        return

    # 2. 等设备 MCP ready
    if not await conn.mcp_client.is_ready():
        conn.logger.bind(tag=TAG).warning("设备 MCP 未就绪，跳过人脸库同步")
        return

    # 3. 逐个推送到设备本地库
    add_tool = sanitize_tool_name("self.face.add")
    if not conn.mcp_client.has_tool(add_tool):
        conn.logger.bind(tag=TAG).warning("设备缺少 self.face.add 工具，无法同步")
        return

    ok, fail = 0, 0
    for f in faces:
        name = f.get("name")
        emb = f.get("embedding_b64")
        if not name or not emb:
            continue
        try:
            args = json.dumps({"name": name, "embedding_b64": emb})
            raw = await call_mcp_tool(conn, conn.mcp_client, add_tool, args, timeout=10)
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict) and data.get("ok"):
                ok += 1
            else:
                fail += 1
                conn.logger.bind(tag=TAG).warning(f"下发 {name} 失败: {data}")
        except Exception as e:
            fail += 1
            conn.logger.bind(tag=TAG).error(f"下发 {name} 异常: {e}")

    conn.logger.bind(tag=TAG).info(f"人脸库同步完成: 成功 {ok}, 失败 {fail}")
