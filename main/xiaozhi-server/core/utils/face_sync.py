"""Greeting-switch sync (warehouse -> device).

Single source of truth = warehouse. The device is a follower: on connect or
on voice trigger, xiaozhi pulls the warehouse face config (greeting_enabled)
and aligns the device-local state via the device MCP tool self.vision.mode.

Face *library* push is NOT done here: warehouse owns it via
`POST /api/mcp/connections/{c}/devices/{d}/push-faces`, which drives the device
directly and additionally does model_tag filtering, subject_id passthrough and
a 20-face cap. Keeping a second pusher here would create two sources of truth.

Robustness (must hold): warehouse being unreachable must NEVER degrade the
device. Every warehouse fetch is best-effort; on any failure we return early
and leave the device exactly as it was (its NVS-persisted library + greeting
switch keep working offline). Greeting/recognition is fully self-contained on
the device; warehouse is only consulted to *update* state, never to *run* it.

Config (data/.config.yaml):
    face_sync:
      warehouse_base: "http://localhost:2124/api"   # or warehouse_url for library
      api_key: "<warehouse X-API-Key>"
"""
import json
from typing import TYPE_CHECKING

from config.logger import setup_logging
from core.utils.util import sanitize_tool_name

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# Device unified its proactive-wake switches into self.vision.mode(0-3):
# 0=off, 1=object, 2=face recognition, 3=face DND. The warehouse only carries a
# boolean greeting switch, so we map greeting on -> mode 2, off -> mode 0.
_VISION_MODE = sanitize_tool_name("self.vision.mode")


def _endpoints(conn: "ConnectionHandler"):
    """Resolve the warehouse face-config URL + api_key from conn.config."""
    cfg = conn.config.get("face_sync", {}) if isinstance(conn.config, dict) else {}
    api_key = cfg.get("api_key", "")
    base = cfg.get("warehouse_base")
    if base:
        base = base.rstrip("/")
        return f"{base}/face/config", api_key
    # Back-compat: only warehouse_url (library) given; derive config sibling.
    lib = cfg.get("warehouse_url")
    if lib and lib.endswith("/face/library"):
        return lib[: -len("/library")] + "/config", api_key
    return None, api_key


async def sync_face_state(conn: "ConnectionHandler") -> dict:
    """Pull the warehouse greeting switch and align the device. Best-effort.

    Returns a small status dict (for logging / voice reply). Never raises;
    on any warehouse failure leaves the device untouched.
    """
    import aiohttp

    mcp_client = getattr(conn, "mcp_client", None)
    if not mcp_client:
        return {"ok": False, "reason": "no_device_mcp"}
    if not await mcp_client.is_ready():
        return {"ok": False, "reason": "device_mcp_not_ready"}

    config_url, api_key = _endpoints(conn)
    if not config_url:
        logger.bind(tag=TAG).error("face_sync.warehouse_base/url 未配置")
        return {"ok": False, "reason": "not_configured"}
    headers = {"X-API-Key": api_key} if api_key else {}
    timeout = aiohttp.ClientTimeout(total=15)

    greeting_enabled = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(config_url, headers=headers) as r:
                if r.status != 200:
                    logger.bind(tag=TAG).warning(f"拉取打招呼开关 HTTP {r.status}，保持设备现状")
                    return {"ok": False, "reason": f"config_http_{r.status}"}
                greeting_enabled = bool((await r.json()).get("greeting_enabled"))
    except Exception as e:
        # Warehouse unreachable — do NOT touch the device. It keeps running on
        # its NVS-persisted library + greeting switch.
        logger.bind(tag=TAG).warning(f"warehouse 不可达，保持设备现状: {e}")
        return {"ok": False, "reason": "warehouse_unreachable"}

    # align greeting switch (only if we successfully read it)
    aligned = None
    if greeting_enabled is not None and mcp_client.has_tool(_VISION_MODE):
        from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
        try:
            # greeting on -> face recognition (mode 2); off -> no proactive wake (mode 0)
            args = json.dumps({"mode": 2 if greeting_enabled else 0})
            await call_mcp_tool(conn, mcp_client, _VISION_MODE, args, timeout=10)
            aligned = greeting_enabled
        except Exception as e:
            logger.bind(tag=TAG).error(f"对齐打招呼开关异常: {e}")

    logger.bind(tag=TAG).info(f"打招呼开关同步: 打招呼={aligned}")
    return {"ok": True, "greeting_enabled": aligned}
