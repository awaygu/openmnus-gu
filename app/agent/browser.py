import json
from typing import TYPE_CHECKING, Optional

from pydantic import Field, model_validator

from app.agent.toolcall import ToolCallAgent
from app.logger import logger
from app.prompt.browser import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import Message, ToolChoice
from app.tool import BrowserUseTool, Terminate, ToolCollection
from app.tool.sandbox.sb_browser_tool import SandboxBrowserTool


# 如果 BrowserAgent 需要 BrowserContextHelper，避免循环导入
if TYPE_CHECKING:
    from app.agent.base import BaseAgent  # 或者定义 memory 的地方


class BrowserContextHelper:
    def __init__(self, agent: "BaseAgent"):
        self.agent = agent
        self._current_base64_image: Optional[str] = None

    async def get_browser_state(self) -> Optional[dict]:
        browser_tool = self.agent.available_tools.get_tool(BrowserUseTool().name)
        if not browser_tool:
            browser_tool = self.agent.available_tools.get_tool(
                SandboxBrowserTool().name
            )
        if not browser_tool or not hasattr(browser_tool, "get_current_state"):
            logger.warning("BrowserUseTool not found or doesn't have get_current_state")
            return None
        try:
            result = await browser_tool.get_current_state()
            if result.error:
                logger.debug(f"Browser state error: {result.error}")
                return None
            if hasattr(result, "base64_image") and result.base64_image:
                self._current_base64_image = result.base64_image
            else:
                self._current_base64_image = None
            return json.loads(result.output)
        except Exception as e:
            logger.debug(f"Failed to get browser state: {str(e)}")
            return None

    async def format_next_step_prompt(self) -> str:
        """获取浏览器状态并格式化浏览器提示词。"""
        browser_state = await self.get_browser_state()
        url_info, tabs_info, content_above_info, content_below_info = "", "", "", ""
        results_info = ""  # 或者如果需要，从 agent 获取

        if browser_state and not browser_state.get("error"):
            url_info = f"\n   URL: {browser_state.get('url', 'N/A')}\n   Title: {browser_state.get('title', 'N/A')}"
            tabs = browser_state.get("tabs", [])
            if tabs:
                tabs_info = f"\n   {len(tabs)} tab(s) available"
            pixels_above = browser_state.get("pixels_above", 0)
            pixels_below = browser_state.get("pixels_below", 0)
            if pixels_above > 0:
                content_above_info = f" ({pixels_above} pixels)"
            if pixels_below > 0:
                content_below_info = f" ({pixels_below} pixels)"

            # 调试信息：显示可交互元素数量
            interactive_elements = browser_state.get("interactive_elements", "")
            element_count = interactive_elements.count("[") if interactive_elements else 0
            logger.info(f"🔍 Browser state: {element_count} interactive elements detected")
            logger.debug(f"🔍 Browser URL: {browser_state.get('url', 'N/A')}")
            logger.debug(f"🔍 Browser Title: {browser_state.get('title', 'N/A')}")
            if interactive_elements:
                # 只显示前200个字符，避免日志过长
                preview = interactive_elements[:200] + "..." if len(interactive_elements) > 200 else interactive_elements
                logger.debug(f"🔍 Interactive elements preview: {preview}")

            # browser-use 返回的元素信息格式：[index]<type>text</type>
            # 包含索引、元素类型和文本描述，应该足够详细让 LLM 根据文本匹配选择元素
            # 因此不发送截图，只使用文本描述，节省成本和提升速度
            if self._current_base64_image:
                image_size_kb = len(self._current_base64_image) * 3 / 4 / 1024  # 估算图片大小（KB）
                logger.debug(f"📸 Browser screenshot captured: {image_size_kb:.2f} KB (base64) - but not sending to LLM")
                logger.debug(f"📝 Using element text descriptions instead of visual model")
                # 不发送截图，只使用文本元素描述
                self._current_base64_image = None  # 丢弃截图，不使用视觉模型
            else:
                logger.debug("📝 No screenshot - using element text descriptions only")

        # 构建完整的 prompt，包含元素列表
        prompt = NEXT_STEP_PROMPT.format(
            url_placeholder=url_info,
            tabs_placeholder=tabs_info,
            content_above_placeholder=content_above_info,
            content_below_placeholder=content_below_info,
            results_placeholder=results_info,
        )

        # 添加元素列表到 prompt（这是关键！）
        if browser_state and not browser_state.get("error"):
            interactive_elements = browser_state.get("interactive_elements", "")
            if interactive_elements:
                prompt += "\n\n[Current state starts here]\n"
                prompt += "Interactive Elements:\n"
                prompt += interactive_elements
                prompt += "\n"

        return prompt

    async def cleanup_browser(self):
        browser_tool = self.agent.available_tools.get_tool(BrowserUseTool().name)
        if browser_tool and hasattr(browser_tool, "cleanup"):
            await browser_tool.cleanup()


class BrowserAgent(ToolCallAgent):
    """
    使用 browser_use 库控制浏览器的浏览器 agent。

    此 agent 可以导航网页、与元素交互、填写表单、
    提取内容并执行其他基于浏览器的操作来完成任务。
    """

    name: str = "browser"
    description: str = "可以控制浏览器来完成任务的浏览器 agent"

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT

    max_observe: int = 10000
    max_steps: int = 20

    # 配置可用工具
    available_tools: ToolCollection = Field(
        default_factory=lambda: ToolCollection(BrowserUseTool(), Terminate())
    )

    # 使用 Auto 进行工具选择，允许工具使用和自由形式的响应
    tool_choices: ToolChoice = ToolChoice.AUTO
    special_tool_names: list[str] = Field(default_factory=lambda: [Terminate().name])

    browser_context_helper: Optional[BrowserContextHelper] = None

    @model_validator(mode="after")
    def initialize_helper(self) -> "BrowserAgent":
        self.browser_context_helper = BrowserContextHelper(self)
        return self

    async def think(self) -> bool:
        """处理当前状态并使用工具决定下一步行动，添加浏览器状态信息"""
        self.next_step_prompt = (
            await self.browser_context_helper.format_next_step_prompt()
        )
        return await super().think()

    async def cleanup(self):
        """通过调用父类清理方法来清理浏览器 agent 资源。"""
        await self.browser_context_helper.cleanup_browser()
