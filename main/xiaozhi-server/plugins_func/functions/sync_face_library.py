"""Voice-triggered face-library + greeting-switch sync (warehouse -> device).

User says "同步人脸库" -> LLM calls this (no args) -> xiaozhi pulls the
warehouse config + library and aligns the device. All logic lives in
core.utils.face_sync (also used by the on-connect auto-sync). Embeddings
never pass through the LLM; warehouse-unreachable leaves the device untouched.
"""
from typing import TYPE_CHECKING

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from core.utils.face_sync import sync_face_state

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

sync_face_library_function_desc = {
    "type": "function",
    "function": {
        "name": "sync_face_library",
        "description": (
            "把服务器（仓库系统）上的人脸库和打招呼开关同步到本设备。"
            "当用户说'同步人脸库'、'更新人脸库'、'刷新认识的人'时调用。无需参数。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


@register_function("sync_face_library", sync_face_library_function_desc, ToolType.SYSTEM_CTL)
def sync_face_library(conn: "ConnectionHandler"):
    if not getattr(conn, "mcp_client", None):
        return ActionResponse(Action.RESPONSE, "device mcp absent", "当前设备不支持人脸库同步")
    if not conn.loop or not conn.loop.is_running():
        return ActionResponse(Action.RESPONSE, "loop not running", "系统繁忙，请稍后再试")

    task = conn.loop.create_task(sync_face_state(conn))

    def _done(f):
        try:
            f.result()
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"人脸库同步失败: {e}")

    task.add_done_callback(_done)
    return ActionResponse(Action.RECORD, "sync started", "正在同步人脸库到设备")
