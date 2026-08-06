from typing import Dict, List, Optional

from pydantic import Field, model_validator

from app.agent.browser import BrowserContextHelper
from app.agent.toolcall import ToolCallAgent
from app.config import config
from app.llm import LLM
from app.logger import logger
from app.prompt.manus import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import Message
from app.tool import Terminate, ToolCollection
from app.tool.ask_human import AskHuman
from app.tool.browser_use_tool import BrowserUseTool
from app.tool.mcp import MCPClients, MCPClientTool
from app.tool.python_execute import PythonExecute
from app.tool.str_replace_editor import StrReplaceEditor
from app.tool.web_search import WebSearch


class Manus(ToolCallAgent):
    """一个通用的多功能 agent，支持本地工具和 MCP 工具。"""

    name: str = "Manus"
    description: str = "一个多功能的 agent，可以使用多种工具（包括基于 MCP 的工具）解决各种任务"

    system_prompt: str = SYSTEM_PROMPT.format(directory=config.workspace_root)
    next_step_prompt: str = NEXT_STEP_PROMPT

    max_observe: int = 10000
    max_steps: int = 30

    # MCP 客户端，用于远程工具访问
    mcp_clients: MCPClients = Field(default_factory=MCPClients)

    # 添加通用工具到工具集合
    available_tools: ToolCollection = Field(
        default_factory=lambda: ToolCollection(
            PythonExecute(),
            BrowserUseTool(),
            WebSearch(),
            StrReplaceEditor(),
            AskHuman(),
            Terminate(),
        )
    )

    special_tool_names: list[str] = Field(default_factory=lambda: [Terminate().name])
    browser_context_helper: Optional[BrowserContextHelper] = None

    # 跟踪已连接的 MCP 服务器
    connected_servers: Dict[str, str] = Field(
        default_factory=dict
    )  # server_id -> url/command
    _initialized: bool = False

    @model_validator(mode="after")
    def initialize_helper(self) -> "Manus":
        """同步初始化基本组件。"""
        self.browser_context_helper = BrowserContextHelper(self)
        return self

    @classmethod
    async def create(cls, **kwargs) -> "Manus":
        """工厂方法，创建并正确初始化 Manus 实例。"""
        instance = cls(**kwargs)
        await instance.initialize_mcp_servers()
        instance._initialized = True
        return instance

    async def initialize_mcp_servers(self) -> None:
        """初始化与已配置的 MCP 服务器的连接。"""
        for server_id, server_config in config.mcp_config.servers.items():
            try:
                if server_config.type == "sse":
                    if server_config.url:
                        await self.connect_mcp_server(server_config.url, server_id)
                        logger.info(
                            f"Connected to MCP server {server_id} at {server_config.url}"
                        )
                elif server_config.type == "stdio":
                    if server_config.command:
                        await self.connect_mcp_server(
                            server_config.command,
                            server_id,
                            use_stdio=True,
                            stdio_args=server_config.args,
                        )
                        logger.info(
                            f"Connected to MCP server {server_id} using command {server_config.command}"
                        )
            except Exception as e:
                logger.error(f"Failed to connect to MCP server {server_id}: {e}")

    async def connect_mcp_server(
        self,
        server_url: str,
        server_id: str = "",
        use_stdio: bool = False,
        stdio_args: List[str] = None,
    ) -> None:
        """连接到 MCP 服务器并添加其工具。"""
        if use_stdio:
            await self.mcp_clients.connect_stdio(
                server_url, stdio_args or [], server_id
            )
            self.connected_servers[server_id or server_url] = server_url
        else:
            await self.mcp_clients.connect_sse(server_url, server_id)
            self.connected_servers[server_id or server_url] = server_url

        # 仅使用此服务器的新工具更新可用工具
        new_tools = [
            tool for tool in self.mcp_clients.tools if tool.server_id == server_id
        ]
        self.available_tools.add_tools(*new_tools)

    async def disconnect_mcp_server(self, server_id: str = "") -> None:
        """断开与 MCP 服务器的连接并移除其工具。"""
        await self.mcp_clients.disconnect(server_id)
        if server_id:
            self.connected_servers.pop(server_id, None)
        else:
            self.connected_servers.clear()

        # 重建可用工具列表，排除已断开连接的服务器工具
        base_tools = [
            tool
            for tool in self.available_tools.tools
            if not isinstance(tool, MCPClientTool)
        ]
        self.available_tools = ToolCollection(*base_tools)
        self.available_tools.add_tools(*self.mcp_clients.tools)

    async def cleanup(self):
        """清理 Manus agent 资源。"""
        if self.browser_context_helper:
            await self.browser_context_helper.cleanup_browser()
        # 仅在已初始化的情况下断开所有 MCP 服务器连接
        if self._initialized:
            await self.disconnect_mcp_server()
            self._initialized = False

    async def think(self) -> bool:
        """处理当前状态，并在适当的上下文中决定下一步行动。"""
        if not self._initialized:
            await self.initialize_mcp_servers()
            self._initialized = True

        original_prompt = self.next_step_prompt
        original_llm = self.llm

        # 检查是否有浏览器工具在消息历史中（已使用过）
        recent_messages = self.memory.messages[-5:] if self.memory.messages else []
        browser_in_use = any(
            tc.function.name == BrowserUseTool().name
            for msg in recent_messages
            if msg.tool_calls
            for tc in msg.tool_calls
        )

        # 检查消息中是否有浏览器截图（说明浏览器工具已使用，需要视觉理解）
        has_browser_screenshot = any(
            (isinstance(msg, Message) and msg.base64_image)
            or (isinstance(msg, dict) and msg.get("base64_image"))
            for msg in recent_messages
        )

        # 检查工具列表中是否包含浏览器工具（可能即将使用）
        browser_tool_available = BrowserUseTool().name in [
            tool.name for tool in self.available_tools.tools
        ]

        # 智能切换策略：
        # browser-use 库返回的元素信息格式：[index]<type>text</type>
        # 包含：索引、元素类型（button、input等）、文本描述（如"出发地"、"搜索"等）
        # 如果元素描述足够详细，qwen-max 应该能够根据文本匹配选择正确的元素
        # 因此：默认使用 qwen-max，只在元素描述不够清晰或需要视觉理解时才使用视觉模型

        # 如果浏览器工具可用，总是使用 browser_context_helper 来格式化 prompt
        # 这样 LLM 能看到浏览器状态和强调使用工具的提示
        if browser_tool_available:
            # 默认使用快速模型（qwen-max），不切换到视觉模型
            # browser-use 返回的元素文本描述应该足够详细，让 LLM 根据文本匹配选择元素
            logger.debug(f"🚀 Using default model for browser automation: {self.llm.model}")
            logger.debug(f"📝 Browser-use provides element descriptions in format: [index]<type>text</type>")

            # 总是使用 browser_context_helper 来格式化 prompt
            # 这样 LLM 能看到浏览器状态（即使浏览器还没打开，也会提示需要打开）
            self.next_step_prompt = (
                await self.browser_context_helper.format_next_step_prompt()
            )

        result = await super().think()

        # 注意：不要在这里重新调用 think()
        # 让第一次选择的工具正常执行，执行后会有截图
        # 下次 think() 时会自动检测到截图并切换到视觉模型

        # Restore original prompt (but keep vision model if browser screenshot is still present)
        self.next_step_prompt = original_prompt
        # 只有在没有浏览器截图时才恢复原模型（快速模型）
        if not has_browser_screenshot and original_llm != self.llm:
            # 检查是否还有浏览器工具调用
            current_browser_in_use = any(
                tc.function.name == BrowserUseTool().name
                for msg in self.memory.messages[-3:]
                if hasattr(msg, 'tool_calls') and msg.tool_calls
                for tc in msg.tool_calls
            )
            # 检查是否还有截图
            current_has_screenshot = any(
                (isinstance(msg, Message) and msg.base64_image)
                or (isinstance(msg, dict) and msg.get("base64_image"))
                for msg in self.memory.messages[-3:]
            )
            if not current_browser_in_use and not current_has_screenshot:
                logger.debug(f"🔄 Restoring original LLM: {original_llm.model}")
                self.llm = original_llm

        return result
