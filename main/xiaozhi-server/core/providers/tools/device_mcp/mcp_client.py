"""设备端MCP客户端定义"""

import asyncio
from concurrent.futures import Future
from core.utils.util import sanitize_tool_name
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class MCPClient:
    """设备端MCP客户端，用于管理MCP状态和工具"""

    def __init__(self, exclude_tools=None):
        # Device tool names never advertised to the LLM. See get_available_tools.
        self.exclude_tools = self._normalize_exclude(exclude_tools)
        self.tools = {}  # sanitized_name -> tool_data
        self.name_mapping = {}
        self.ready = False
        self.call_results = {}  # To store Futures for tool call responses
        self.next_id = 1
        self.lock = asyncio.Lock()
        self._cached_available_tools = None  # Cache for get_available_tools

    @staticmethod
    def _normalize_exclude(raw) -> set:
        """Accept a list or a comma-separated string; empty means keep all."""
        if not raw:
            return set()
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",")]
        return {x for x in raw if x}

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def get_available_tools(self) -> list:
        """Tool specs handed to the LLM on **every** request.

        The cost here is the advertisement, not the invocation: a chatty
        firmware can spend the whole context window before the user's
        sentence is considered. Measured on real hardware — 29 tools rendered
        to ~11 000 characters (~7 000 tokens) against an 8 192-token model,
        and roughly half were device housekeeping (screen brightness, volume,
        IP address) that a warehouse assistant can never usefully call.

        ``exclude_tools`` is empty by default, so existing deployments keep
        advertising everything.
        """
        # Check if the cache is valid
        if self._cached_available_tools is not None:
            return self._cached_available_tools

        # If cache is not valid, regenerate the list
        result = []
        for tool_name, tool_data in self.tools.items():
            if tool_name in self.exclude_tools:
                continue
            function_def = {
                "name": tool_name,
                "description": tool_data["description"],
                "parameters": {
                    "type": tool_data["inputSchema"].get("type", "object"),
                    "properties": tool_data["inputSchema"].get("properties", {}),
                    "required": tool_data["inputSchema"].get("required", []),
                },
            }
            result.append({"type": "function", "function": function_def})

        self._cached_available_tools = result  # Store the generated list in cache
        return result

    async def is_ready(self) -> bool:
        async with self.lock:
            return self.ready

    async def set_ready(self, status: bool):
        async with self.lock:
            self.ready = status

    async def add_tool(self, tool_data: dict):
        async with self.lock:
            sanitized_name = sanitize_tool_name(tool_data["name"])
            self.tools[sanitized_name] = tool_data
            self.name_mapping[sanitized_name] = tool_data["name"]
            self._cached_available_tools = (
                None  # Invalidate the cache when a tool is added
            )

    async def get_next_id(self) -> int:
        async with self.lock:
            current_id = self.next_id
            self.next_id += 1
            return current_id

    async def register_call_result_future(self, id: int, future: Future):
        async with self.lock:
            self.call_results[id] = future

    async def resolve_call_result(self, id: int, result: any):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_result(result)

    async def reject_call_result(self, id: int, exception: Exception):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_exception(exception)

    async def cleanup_call_result(self, id: int):
        async with self.lock:
            if id in self.call_results:
                self.call_results.pop(id)
