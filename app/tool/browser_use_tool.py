import asyncio
import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Generic, Optional, TypeVar

from browser_use import Browser as BrowserUseBrowser
from browser_use import BrowserConfig
from browser_use.browser.context import BrowserContext, BrowserContextConfig
from browser_use.dom.service import DomService
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from app.config import config
from app.llm import LLM
from app.logger import logger
from app.tool.base import BaseTool, ToolResult
from app.tool.web_search import WebSearch


# 真实桌面浏览器 UA。browser-use 默认 UA 易被反爬识别，这里显式设置。
# 若 config.browser_config.new_context_config.user_agent 已配置则优先用配置值。
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 拦截页特征：携程风控返回的 whaleguard 拦截页
_BLOCKED_MARKERS = ("whaleguard",)


def _norm_text(s: str) -> str:
    """规范化文字以便宽松匹配：移除所有空白（含全角空格）、统一小写。"""
    if not s:
        return ""
    return s.replace("　", " ").replace("　", " ").strip().lower().replace(" ", "")


# 按钮文字常见词与用户描述的等价映射（规范化后比较）
_BUTTON_KEYWORD_GROUPS = [
    {"搜索", "搜", "search", "查询", "查", "search"},
    {"提交", "submit", "确认", "confirm"},
    {"登录", "login", "登陆"},
]


# 中文停用词，提取 goal 关键词时过滤
_STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "都", "一", "一个",
    "上", "下", "中", "里", "到", "去", "来", "把", "被", "让", "给", "为",
    "请", "帮", "帮我", "帮忙", "查询", "查", "找", "搜索", "搜",
    "信息", "内容", "列表", "详情", "数据", "结果", "页面", "网页", "网站",
    "一下", "并", "且", "然后", "接着", "最后", "需要", "想要", "希望", "能",
    "这个", "那个", "这些", "那些", "什么", "怎么", "如何", "哪些",
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on",
}


def _extract_keywords(goal: str) -> list:
    """从 goal 提取关键词：英文按空格/标点分词，中文按 2-3 字滑窗取候选词。"""
    if not goal:
        return []
    kws = set()
    # 英文词
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,}", goal):
        wl = w.lower()
        if wl not in _STOPWORDS and len(wl) >= 2:
            kws.add(wl)
    # 中文：去掉标点和空格后的连续片段，按 2/3 字滑窗
    cn_segs = re.split(r"[\s,，。、；;:：！!？?·\(\)（）\[\]【】\"'""''<>《》/\\|]+", goal)
    for seg in cn_segs:
        seg = re.sub(r"[^一-龥]", "", seg)
        if not seg:
            continue
        if len(seg) <= 3:
            if seg not in _STOPWORDS:
                kws.add(seg)
        else:
            for n in (2, 3):
                for i in range(len(seg) - n + 1):
                    gram = seg[i : i + n]
                    if gram not in _STOPWORDS:
                        kws.add(gram)
    return list(kws)


def _extract_relevant(content: str, goal: str, max_len: int) -> str:
    """智能截断：当 content 超过 max_len 时，优先保留含 goal 关键词的段落。

    按行切分，对每段按"含关键词数 + 航班/价格特征"打分，取高分段拼接至 max_len，
    并按原顺序排列便于 LLM 理解上下文。任何异常回退到 content[:max_len]。
    """
    try:
        if not content or len(content) <= max_len:
            return content or ""
        kws = _extract_keywords(goal)
        # 航班/列表类特征正则
        flight_re = re.compile(r"[A-Z]{2}\d{2,4}|¥|￥|\d{1,2}:\d{2}")
        # 移除 markdown 图片中的 base64 数据（占空间且对提取无意义），保留 alt 文本
        content_clean = re.sub(
            r"!\[([^\]]*)\]\(data:image/[^)]+\)", r"[图:\1]", content
        )
        lines = content_clean.split("\n")
        scored = []  # (score, original_index, line)
        for i, line in enumerate(lines):
            line_s = line.strip()
            if not line_s:
                continue
            score = 0
            low = line_s.lower()
            for kw in kws:
                if kw in low:
                    score += 2
                elif kw in line_s:
                    score += 2
            if flight_re.search(line_s):
                score += 1
            # 否定/占位提示降权，避免"暂无航班"等占位符盖过真实数据
            if any(neg in line_s for neg in ("暂无", "未找到", "无符合", "加载中", "loading", "未直接显示")):
                score = max(score - 5, 0)
            if score > 0:
                scored.append((score, i, line_s))
        if not scored:
            return content_clean[:max_len]
        # 按分数降序选段，直到达到 max_len
        scored.sort(key=lambda x: (-x[0], x[1]))
        chosen_idx = set()
        total = 0
        for score, i, line_s in scored:
            if total + len(line_s) + 1 > max_len:
                break
            chosen_idx.add(i)
            total += len(line_s) + 1
        if not chosen_idx:
            return content_clean[:max_len]
        # 按原顺序拼接选中段落
        parts = [lines[i].strip() for i in sorted(chosen_idx)]
        result = "\n".join(parts)
        # 若仍有余量，补前 max_len 的剩余字符（去重避免重复）
        if len(result) < max_len:
            remaining = max_len - len(result)
            tail = content_clean[: remaining + 200]
            result = (result + "\n...\n" + tail)[:max_len]
        return result
    except Exception:
        return content[:max_len]


_BROWSER_DESCRIPTION = """\
浏览器自动化工具，提供简洁的操作接口：

## 核心操作（推荐使用）
* click: 点击元素 - 参数 element_description 描述要点击的元素
  示例: click(element_description="搜索按钮")
  示例: click(element_description="1月30日")
  示例: click(element_description="确认")

* type: 输入文本 - 参数 element_description 描述输入框，text 为要输入的文本
  示例: type(element_description="出发城市", text="上海")
  示例: type(element_description="搜索框", text="机票")

## 辅助操作
* go_to_url: 导航到 URL
* scroll_down/scroll_up: 滚动页面
* send_keys: 发送按键（Enter、Escape 等）
* wait: 等待秒数
* go_back: 返回上一页
* extract_content: 提取页面内容

## 工作原理
click 和 type 内部自动选择最佳策略：
1. 优先通过 JavaScript 分析 HTML 定位元素
2. 如果失败，自动使用视觉模型识别
3. 自动处理下拉选项和页面变化
"""

Context = TypeVar("Context")


class BrowserUseTool(BaseTool, Generic[Context]):
    name: str = "browser_use"
    description: str = _BROWSER_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "go_to_url",
                    "click",
                    "type",
                    "scroll_down",
                    "scroll_up",
                    "send_keys",
                    "go_back",
                    "wait",
                    "extract_content",
                    "switch_tab",
                    "open_tab",
                    "close_tab",
                ],
                "description": "要执行的浏览器操作。推荐使用 click（点击元素）和 type（输入文本）",
            },
            "url": {
                "type": "string",
                "description": "用于 'go_to_url' 或 'open_tab' 操作的 URL",
            },
            "element_description": {
                "type": "string",
                "description": "用于 'click' 或 'type' 的元素描述（如：'搜索按钮'、'出发城市'、'1月30日'）",
            },
            "text": {
                "type": "string",
                "description": "用于 'type' 操作的输入文本",
            },
            "scroll_amount": {
                "type": "integer",
                "description": "用于 'scroll_down' 或 'scroll_up' 操作的滚动像素数",
            },
            "tab_id": {
                "type": "integer",
                "description": "用于 'switch_tab' 操作的标签页 ID",
            },
            "keys": {
                "type": "string",
                "description": "用于 'send_keys' 操作要发送的按键（如 Enter、Escape）",
            },
            "seconds": {
                "type": "integer",
                "description": "用于 'wait' 操作要等待的秒数",
            },
            "goal": {
                "type": "string",
                "description": "用于 'extract_content' 操作的提取目标",
            },
        },
        "required": ["action"],
        "dependencies": {
            "go_to_url": ["url"],
            "click": ["element_description"],
            "type": ["element_description", "text"],
            "scroll_down": ["scroll_amount"],
            "scroll_up": ["scroll_amount"],
            "send_keys": ["keys"],
            "go_back": [],
            "wait": ["seconds"],
            "extract_content": ["goal"],
            "switch_tab": ["tab_id"],
            "open_tab": ["url"],
            "close_tab": [],
        },
    }

    lock: asyncio.Lock = Field(default_factory=asyncio.Lock)
    browser: Optional[BrowserUseBrowser] = Field(default=None, exclude=True)
    context: Optional[BrowserContext] = Field(default=None, exclude=True)
    dom_service: Optional[DomService] = Field(default=None, exclude=True)
    web_search_tool: WebSearch = Field(default_factory=WebSearch, exclude=True)

    # Context for generic functionality
    tool_context: Optional[Context] = Field(default=None, exclude=True)

    llm: Optional[LLM] = Field(default_factory=LLM)

    @field_validator("parameters", mode="before")
    def validate_parameters(cls, v: dict, info: ValidationInfo) -> dict:
        if not v:
            raise ValueError("Parameters cannot be empty")
        return v

    # 注意：日期选择器元素提取已移除，现在使用 vision_click 视觉模式处理
    # 所有动态元素（日期选择器、弹窗等）都通过 GUI-Plus 视觉模型来识别和点击

    async def _ensure_browser_initialized(self) -> BrowserContext:
        """确保浏览器和上下文已初始化。"""
        if self.browser is None:
            browser_config_kwargs = {"headless": False, "disable_security": True}

            if config.browser_config:
                from browser_use.browser.browser import ProxySettings

                # 处理代理设置。
                if config.browser_config.proxy and config.browser_config.proxy.server:
                    browser_config_kwargs["proxy"] = ProxySettings(
                        server=config.browser_config.proxy.server,
                        username=config.browser_config.proxy.username,
                        password=config.browser_config.proxy.password,
                    )

                browser_attrs = [
                    "headless",
                    "disable_security",
                    "extra_chromium_args",
                    "chrome_instance_path",
                    "wss_url",
                    "cdp_url",
                ]

                for attr in browser_attrs:
                    value = getattr(config.browser_config, attr, None)
                    if value is not None:
                        if not isinstance(value, list) or value:
                            browser_config_kwargs[attr] = value

            self.browser = BrowserUseBrowser(BrowserConfig(**browser_config_kwargs))

        if self.context is None:
            context_config = BrowserContextConfig()

            # 如果配置中有上下文配置，则使用它。
            if (
                config.browser_config
                and hasattr(config.browser_config, "new_context_config")
                and config.browser_config.new_context_config
            ):
                context_config = config.browser_config.new_context_config

            # 设置真实桌面 UA，避免被反爬识别。配置已显式提供则尊重配置。
            if not getattr(context_config, "user_agent", None):
                context_config.user_agent = _DEFAULT_USER_AGENT

            self.context = await self.browser.new_context(context_config)
            self.dom_service = DomService(await self.context.get_current_page())

        return self.context

    async def execute(
        self,
        action: str,
        url: Optional[str] = None,
        index: Optional[int] = None,
        text: Optional[str] = None,
        scroll_amount: Optional[int] = None,
        tab_id: Optional[int] = None,
        query: Optional[str] = None,
        goal: Optional[str] = None,
        keys: Optional[str] = None,
        seconds: Optional[int] = None,
        vision_instruction: Optional[str] = None,
        element_description: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """
        执行指定的浏览器操作。

        Args:
            action: 要执行的浏览器操作
            url: 用于导航或新标签页的 URL
            index: 用于点击或输入操作的元素索引
            text: 用于输入操作或搜索查询的文本
            scroll_amount: 用于滚动操作的滚动像素数
            tab_id: 用于 switch_tab 操作的标签页 ID
            query: 用于 Google 搜索的搜索查询
            goal: 用于内容提取的提取目标
            keys: 用于键盘操作要发送的按键
            seconds: 要等待的秒数
            vision_instruction: 用于 vision_click 操作的视觉指令
            element_description: 用于 smart_click/smart_input 的元素描述
            **kwargs: 其他参数

        Returns:
            包含操作输出或错误的 ToolResult
        """
        async with self.lock:
            try:
                context = await self._ensure_browser_initialized()

                # 从配置中获取最大内容长度
                max_content_length = getattr(
                    config.browser_config, "max_content_length", 2000
                )

                # 导航操作
                if action == "go_to_url":
                    if not url:
                        return ToolResult(
                            error="URL is required for 'go_to_url' action"
                        )

                    # 检测并修正携程机票 URL 中的过期日期
                    original_url = url
                    if "flights.ctrip.com" in url and "date=" in url:
                        date_match = re.search(r'date=(\d{4})-(\d{2})-(\d{2})', url)
                        if date_match:
                            try:
                                url_date = datetime.strptime(date_match.group(0)[5:], '%Y-%m-%d').date()
                                today = datetime.now().date()
                                if url_date < today:
                                    # 日期在过去，自动修正为当前年份
                                    corrected_date = url_date.replace(year=today.year)
                                    # 如果修正后仍在过去，使用明年
                                    if corrected_date < today:
                                        corrected_date = corrected_date.replace(year=today.year + 1)
                                    url = url.replace(date_match.group(0), f"date={corrected_date.strftime('%Y-%m-%d')}")
                                    logger.warning(f"[browser] 自动修正过期日期: {date_match.group(0)[5:]} -> {corrected_date.strftime('%Y-%m-%d')}")
                            except ValueError:
                                pass  # 日期解析失败，保持原 URL

                    page = await context.get_current_page()
                    await page.goto(url, wait_until="domcontentloaded")
                    # SPA 异步数据需要更充分的等待；networkidle 失败也不阻断
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # 检测反爬拦截页（如携程 whaleguard）
                    content = await page.content()
                    if any(m in content.lower() for m in _BLOCKED_MARKERS):
                        logger.warning(f"[browser] 页面被风控拦截(whaleguard): {url}")
                        return ToolResult(
                            error=(
                                f"页面被反爬风控拦截(whaleguard)，无法加载真实内容: {url}。"
                                "请勿使用 itinerary 直链。改用首页入口进入："
                                "先 go_to_url 到 https://www.ctrip.com/（携程首页，可正常加载），"
                                "或直接用去哪儿网 https://flight.qunar.com/，"
                                "再通过 click/type 操作选择出发地、目的地和日期。"
                            )
                        )

                    if url != original_url:
                        return ToolResult(output=f"Navigated to {url} (日期已自动修正)")
                    return ToolResult(output=f"Navigated to {url}")

                elif action == "go_back":
                    await context.go_back()
                    return ToolResult(output="Navigated back")

                elif action == "refresh":
                    await context.refresh_page()
                    return ToolResult(output="Refreshed current page")

                elif action == "web_search":
                    if not query:
                        return ToolResult(
                            error="Query is required for 'web_search' action"
                        )
                    # 执行网页搜索并直接返回结果，无需浏览器导航
                    search_response = await self.web_search_tool.execute(
                        query=query, fetch_content=True, num_results=1
                    )
                    # 导航到第一个搜索结果
                    first_search_result = search_response.results[0]
                    url_to_navigate = first_search_result.url

                    page = await context.get_current_page()
                    await page.goto(url_to_navigate)
                    await page.wait_for_load_state()

                    return search_response

                # 元素交互操作
                elif action == "click_element":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'click_element' action"
                        )

                    page = await context.get_current_page()

                    # 检查是否是日期选择器中的日期元素
                    if hasattr(self, '_date_picker_element_map') and index in self._date_picker_element_map:
                        # 这是日期选择器中的日期元素，使用坐标或 JavaScript 点击
                        date_info = self._date_picker_element_map[index]
                        rect = date_info.get('rect', {})
                        date_text = date_info.get('text', '')
                        logger.info(f"📅 点击日期选择器中的日期元素 (index {index}, 日期: {date_text})")

                        if rect and rect.get('width', 0) > 0 and rect.get('height', 0) > 0:
                            # 使用坐标点击
                            x = rect.get('x', 0) + rect.get('width', 0) / 2
                            y = rect.get('y', 0) + rect.get('height', 0) / 2
                            await page.mouse.click(x, y)
                            await asyncio.sleep(0.5)
                            return ToolResult(output=f"Clicked date picker element at index {index} (date: {date_text})")
                        else:
                            # 使用 JavaScript 点击
                            click_js = f"""
                            (function() {{
                                const calendarModal = document.querySelector('.calendar-modal, .date-picker-wrapper');
                                if (!calendarModal) return false;

                                const dateElements = calendarModal.querySelectorAll('.date-day:not(.date-disabled):not(.disabled)');
                                const targetDate = '{date_text}';

                                for (let el of dateElements) {{
                                    const dateSpan = el.querySelector('.date-d, [class*="date-d"]');
                                    const dateText = dateSpan ? dateSpan.textContent.trim() : el.textContent.trim();
                                    if (dateText === targetDate) {{
                                        el.click();
                                        return true;
                                    }}
                                }}
                                return false;
                            }})();
                            """
                            clicked = await page.evaluate(click_js)
                            if clicked:
                                await asyncio.sleep(0.5)
                                return ToolResult(output=f"Clicked date picker element at index {index} (date: {date_text})")
                            else:
                                return ToolResult(error=f"Failed to click date picker element at index {index}")

                    # 获取点击前的状态（用于调试日期选择器）
                    try:
                        state_before = await context.get_state()
                        if state_before.element_tree:
                            elements_before = state_before.element_tree.clickable_elements_to_string()
                            # 检查是否点击的是日期相关的元素
                            if elements_before:
                                element_lines = elements_before.split("\n")
                                if index < len(element_lines):
                                    element_line = element_lines[index]
                                    if any(keyword in element_line.lower() for keyword in ["日期", "date", "出发", "departure", "calendar", "日历"]):
                                        logger.info(f"📅 检测到点击日期相关元素 (index {index}): {element_line[:100]}")
                    except Exception as e:
                        logger.debug(f"获取点击前状态失败: {e}")

                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    download_path = await context._click_element_node(element)

                    # 等待页面稳定
                    page = await context.get_current_page()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except:
                        pass

                    # 如果是日期相关元素，等待一下让日期选择器完全加载
                    try:
                        await asyncio.sleep(1)  # 等待日期选择器动画完成
                        state_after = await context.get_state()
                        if state_after.element_tree:
                            elements_after = state_after.element_tree.clickable_elements_to_string()
                            # 检查元素数量变化（日期选择器打开后元素数量可能会变化）
                            element_count_before = elements_before.count("[") if 'elements_before' in locals() else 0
                            element_count_after = elements_after.count("[") if elements_after else 0

                            if abs(element_count_after - element_count_before) > 10:
                                logger.info(f"📅 点击后元素数量变化: {element_count_before} -> {element_count_after}，可能是日期选择器打开")
                                # 额外等待并重新获取状态
                                await asyncio.sleep(1)
                                state_after = await context.get_state()

                                # 保存日期选择器打开后的 HTML
                                html_content = await page.content()
                                debug_dir = Path("debug_html")
                                debug_dir.mkdir(exist_ok=True)
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                filename = f"{timestamp}_date_picker_opened.html"
                                filepath = debug_dir / filename
                                with open(filepath, "w", encoding="utf-8") as f:
                                    f.write(html_content)
                                logger.info(f"💾 已保存日期选择器打开后的 HTML 到: {filepath}")

                                # 保存元素信息
                                if elements_after:
                                    elements_file = debug_dir / f"{timestamp}_date_picker_elements.txt"
                                    with open(elements_file, "w", encoding="utf-8") as f:
                                        f.write(f"URL: {state_after.url}\n")
                                        f.write(f"Title: {state_after.title}\n")
                                        f.write(f"Element Count Before: {element_count_before}\n")
                                        f.write(f"Element Count After: {element_count_after}\n")
                                        f.write(f"\n=== All Interactive Elements After Click ===\n")
                                        f.write(elements_after)
                                    logger.info(f"💾 已保存日期选择器元素信息到: {elements_file}")
                    except Exception as e:
                        logger.debug(f"检查日期选择器状态失败: {e}")

                    output = f"Clicked element at index {index}"
                    if download_path:
                        output += f" - Downloaded file to {download_path}"
                    return ToolResult(output=output)

                elif action == "input_text":
                    if index is None or not text:
                        return ToolResult(
                            error="Index and text are required for 'input_text' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    await context._input_text_element_node(element, text)
                    return ToolResult(
                        output=f"Input '{text}' into element at index {index}"
                    )

                elif action == "scroll_down" or action == "scroll_up":
                    direction = 1 if action == "scroll_down" else -1
                    amount = (
                        scroll_amount
                        if scroll_amount is not None
                        else context.config.browser_window_size["height"]
                    )
                    await context.execute_javascript(
                        f"window.scrollBy(0, {direction * amount});"
                    )
                    return ToolResult(
                        output=f"Scrolled {'down' if direction > 0 else 'up'} by {amount} pixels"
                    )

                elif action == "scroll_to_text":
                    if not text:
                        return ToolResult(
                            error="Text is required for 'scroll_to_text' action"
                        )
                    page = await context.get_current_page()
                    try:
                        locator = page.get_by_text(text, exact=False)
                        await locator.scroll_into_view_if_needed()
                        return ToolResult(output=f"Scrolled to text: '{text}'")
                    except Exception as e:
                        return ToolResult(error=f"Failed to scroll to text: {str(e)}")

                elif action == "send_keys":
                    if not keys:
                        return ToolResult(
                            error="Keys are required for 'send_keys' action"
                        )
                    page = await context.get_current_page()
                    await page.keyboard.press(keys)
                    return ToolResult(output=f"Sent keys: {keys}")

                elif action == "get_dropdown_options":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'get_dropdown_options' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    options = await page.evaluate(
                        """
                        (xpath) => {
                            const select = document.evaluate(xpath, document, null,
                                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (!select) return null;
                            return Array.from(select.options).map(opt => ({
                                text: opt.text,
                                value: opt.value,
                                index: opt.index
                            }));
                        }
                    """,
                        element.xpath,
                    )
                    return ToolResult(output=f"Dropdown options: {options}")

                elif action == "select_dropdown_option":
                    if index is None or not text:
                        return ToolResult(
                            error="Index and text are required for 'select_dropdown_option' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    await page.select_option(element.xpath, label=text)
                    return ToolResult(
                        output=f"Selected option '{text}' from dropdown at index {index}"
                    )

                # 内容提取操作
                elif action == "extract_content":
                    if not goal:
                        return ToolResult(
                            error="Goal is required for 'extract_content' action"
                        )

                    page = await context.get_current_page()
                    import markdownify

                    # 等待页面内容稳定：若检测到加载占位符（暂无/loading），再等一会儿重试，
                    # 避免在航班等异步内容渲染前提取到占位文案。最多等 ~6 秒。
                    try:
                        for _ in range(6):
                            html = await page.content()
                            if not any(
                                m in html for m in ("暂无符合", "加载中", "loading", "Loading")
                            ):
                                break
                            await asyncio.sleep(1.0)
                    except Exception:
                        pass

                    content = markdownify.markdownify(await page.content())

                    # 智能截断：优先保留含 goal 关键词的段落，避免航班数据被前N字符截断丢弃
                    page_content = _extract_relevant(content, goal, max_content_length)

                    prompt = f"""\
Your task is to extract the content of the page. You will be given a page and a goal, and you should extract all relevant information around this goal from the page. If the goal is vague, summarize the page. Respond in json format.
Extraction goal: {goal}

Page content:
{page_content}
"""
                    messages = [{"role": "system", "content": prompt}]

                    # 定义提取函数模式
                    extraction_function = {
                        "type": "function",
                        "function": {
                            "name": "extract_content",
                            "description": "Extract specific information from a webpage based on a goal",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "extracted_content": {
                                        "type": "object",
                                        "description": "The content extracted from the page according to the goal",
                                        "properties": {
                                            "text": {
                                                "type": "string",
                                                "description": "Text content extracted from the page",
                                            },
                                            "metadata": {
                                                "type": "object",
                                                "description": "Additional metadata about the extracted content",
                                                "properties": {
                                                    "source": {
                                                        "type": "string",
                                                        "description": "Source of the extracted content",
                                                    }
                                                },
                                            },
                                        },
                                    }
                                },
                                "required": ["extracted_content"],
                            },
                        },
                    }

                    # 使用 LLM 通过必需的函数调用来提取内容
                    response = await self.llm.ask_tool(
                        messages,
                        tools=[extraction_function],
                        tool_choice="required",
                    )

                    if response and response.tool_calls:
                        args = json.loads(response.tool_calls[0].function.arguments)
                        extracted_content = args.get("extracted_content", {})
                        return ToolResult(
                            output=f"Extracted from page:\n{extracted_content}\n"
                        )

                    return ToolResult(output="No content was extracted from the page.")

                # 标签页管理操作
                elif action == "switch_tab":
                    if tab_id is None:
                        return ToolResult(
                            error="Tab ID is required for 'switch_tab' action"
                        )
                    await context.switch_to_tab(tab_id)
                    page = await context.get_current_page()
                    await page.wait_for_load_state()
                    return ToolResult(output=f"Switched to tab {tab_id}")

                elif action == "open_tab":
                    if not url:
                        return ToolResult(error="URL is required for 'open_tab' action")
                    await context.create_new_tab(url)
                    return ToolResult(output=f"Opened new tab with {url}")

                elif action == "close_tab":
                    await context.close_current_tab()
                    return ToolResult(output="Closed current tab")

                # 实用操作
                elif action == "wait":
                    seconds_to_wait = seconds if seconds is not None else 3
                    await asyncio.sleep(seconds_to_wait)
                    return ToolResult(output=f"Waited for {seconds_to_wait} seconds")

                # 简化的 click 操作：智能路由（JavaScript -> 视觉模型）
                elif action == "click":
                    if not element_description:
                        return ToolResult(
                            error="element_description is required for 'click' action"
                        )
                    return await self._click(context, element_description)

                # 简化的 type 操作：智能路由（JavaScript -> 视觉模型）
                elif action == "type":
                    if not element_description:
                        return ToolResult(
                            error="element_description is required for 'type' action"
                        )
                    if not text:
                        return ToolResult(
                            error="text is required for 'type' action"
                        )
                    return await self._type(context, element_description, text)

                else:
                    return ToolResult(error=f"Unknown action: {action}")

            except Exception as e:
                return ToolResult(error=f"Browser action '{action}' failed: {str(e)}")

    async def _execute_vision_action(
        self, context: BrowserContext, vision_instruction: str, action_hint: str = "click"
    ) -> ToolResult:
        """
        使用 GUI-Plus 视觉模型执行浏览器操作。

        工作流程：
        1. 截取当前页面的截图
        2. 将截图发送给 GUI-Plus 模型，附带用户指令
        3. 解析模型返回的 JSON（包含 action 和参数）
        4. 执行相应的操作（点击、输入、滚动等）

        Args:
            context: 浏览器上下文
            vision_instruction: 用户的视觉指令（如"点击搜索按钮"、"在出发地输入框输入上海"）
            action_hint: 操作提示（"click" 或 "type"），帮助模型理解意图

        Returns:
            包含操作结果的 ToolResult
        """
        try:
            from openai import AsyncOpenAI
            import os

            page = await context.get_current_page()
            await page.bring_to_front()
            await page.wait_for_load_state()

            # 1. 截取当前页面截图（使用 viewport 截图，不是 full_page）
            screenshot_bytes = await page.screenshot(
                type="png",
                full_page=False,  # 只截取可见区域，确保坐标对齐
            )
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            image_data_url = f"data:image/png;base64,{screenshot_base64}"

            logger.info(f"[GUI-Plus] Taking screenshot for vision_{action_hint}...")

            # 保存截图用于调试
            debug_dir = Path("debug_html")
            debug_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = debug_dir / f"{timestamp}_vision_{action_hint}.png"
            with open(screenshot_path, "wb") as f:
                f.write(screenshot_bytes)
            logger.info(f"[GUI-Plus] Screenshot saved: {screenshot_path}")

            # 2. 构建 GUI-Plus 的 system prompt
            gui_plus_system_prompt = """## 1. 核心角色 (Core Role)
你是一个顶级的AI视觉操作代理。你的任务是分析电脑屏幕截图，理解用户的指令，然后将任务分解为单一、精确的GUI原子操作。

## 2. [CRITICAL] 坐标精确性要求
- **仔细观察截图**：返回的坐标必须是目标元素的**实际中心位置**
- **不要使用固定坐标**：每次都要根据截图中的实际元素位置来确定坐标
- **输入框识别**：对于输入框，坐标应该是输入框内部的中心位置，通常在文字区域内
- **验证坐标**：确保坐标点落在目标元素的边界内

## 3. [CRITICAL] JSON Schema & 绝对规则
你的输出**必须**是一个严格符合以下规则的JSON对象。**任何偏差都将导致失败**。
- **[R1] 严格的JSON**: 你的回复**必须**是且**只能是**一个JSON对象。禁止在JSON代码块前后添加任何文本、注释或解释。
- **[R2] 精确的Action值**: `action`字段的值**必须**是下列之一：`CLICK`, `TYPE`, `SCROLL`, `KEY_PRESS`, `FINISH`, `FAIL`。
- **[R3] 严格的Parameters结构**: `parameters`对象的结构**必须**与所选Action定义的模板**完全一致**。

## 4. 工具集 (Available Actions)

### CLICK
- **功能**: 单击屏幕上的元素。
- **Parameters模板**:
{"x": <integer>, "y": <integer>, "description": "<string: 描述你点击的是什么>"}

### TYPE
- **功能**: 先点击输入框，然后输入文本。必须提供输入框中心的坐标。
- **重要**: x和y坐标必须是输入框内部文字区域的中心位置
- **Parameters模板**:
{"x": <integer>, "y": <integer>, "text": "<string>", "needs_enter": <boolean>, "description": "<string: 描述输入框>"}

### SCROLL
- **功能**: 滚动窗口。
- **Parameters模板**:
{"direction": "<'up' or 'down'>", "amount": "<'small', 'medium', or 'large'>"}

### KEY_PRESS
- **功能**: 按下功能键。
- **Parameters模板**:
{"key": "<string: e.g., 'enter', 'esc', 'alt+f4'>"}

### FINISH
- **功能**: 任务成功完成。
- **Parameters模板**:
{"message": "<string: 总结任务完成情况>"}

### FAIL
- **功能**: 任务无法完成。
- **Parameters模板**:
{"reason": "<string: 清晰解释失败原因>"}

## 5. 重要提醒
- 坐标必须根据截图中元素的**实际位置**来确定，不要使用固定值
- 输入框的坐标应该是输入框**内部中心**的位置
- 按钮的坐标应该是按钮**中心**的位置
- 仔细观察截图，找到目标元素的边界，然后计算中心坐标
"""

            # 3. 调用 GUI-Plus 模型
            # 使用配置中的 API key 和 base_url
            api_key = os.getenv("DASHSCOPE_API_KEY") or config.llm.get("default", {}).get("api_key", "")
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

            client = AsyncOpenAI(api_key=api_key, base_url=base_url)

            messages = [
                {"role": "system", "content": gui_plus_system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {"type": "text", "text": vision_instruction},
                    ],
                },
            ]

            logger.info(f"[GUI-Plus] Calling model with instruction: {vision_instruction}")

            completion = await client.chat.completions.create(
                model="gui-plus",
                messages=messages,
            )

            response_content = completion.choices[0].message.content
            logger.info(f"[GUI-Plus] Model response: {response_content}")

            # 4. 解析 JSON 响应
            # 预处理：修复 {"x": 139, 675} 这种格式 -> {"x": 139, "y": 675}
            fixed_response = re.sub(
                r'"x":\s*(\d+),\s*(\d+)\s*[,}]',
                lambda m: f'"x": {m.group(1)}, "y": {m.group(2)}' + (',' if m.group(0).endswith(',') else '}'),
                response_content
            )
            # 修复 {"x": [139, 675]} 这种格式 -> {"x": 139, "y": 675}
            fixed_response = re.sub(
                r'"x":\s*\[(\d+),\s*(\d+)\]',
                r'"x": \1, "y": \2',
                fixed_response
            )
            if fixed_response != response_content:
                logger.warning(f"[GUI-Plus] Fixed JSON format: {fixed_response[:200]}")

            # 尝试提取 JSON（处理可能的 markdown 代码块和不完整的 JSON）
            json_match = re.search(r'\{[\s\S]*\}', fixed_response)
            if not json_match:
                # 尝试修复不完整的 JSON（添加缺失的 }）
                incomplete_match = re.search(r'\{[\s\S]*', fixed_response)
                if incomplete_match:
                    incomplete_json = incomplete_match.group()
                    # 计算缺失的闭合括号数量
                    open_braces = incomplete_json.count('{')
                    close_braces = incomplete_json.count('}')
                    missing_braces = open_braces - close_braces
                    if missing_braces > 0:
                        fixed_json = incomplete_json + '}' * missing_braces
                        try:
                            result = json.loads(fixed_json)
                            logger.warning(f"[GUI-Plus] Fixed incomplete JSON response")
                        except json.JSONDecodeError:
                            return ToolResult(error=f"无法从模型响应中解析 JSON: {fixed_response}")
                    else:
                        return ToolResult(error=f"无法从模型响应中解析 JSON: {fixed_response}")
                else:
                    return ToolResult(error=f"无法从模型响应中解析 JSON: {fixed_response}")
            else:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError as e:
                    # 尝试修复不完整的 JSON
                    json_str = json_match.group()
                    open_braces = json_str.count('{')
                    close_braces = json_str.count('}')
                    if open_braces > close_braces:
                        fixed_json = json_str + '}' * (open_braces - close_braces)
                        try:
                            result = json.loads(fixed_json)
                            logger.warning(f"[GUI-Plus] Fixed incomplete JSON response")
                        except json.JSONDecodeError:
                            return ToolResult(error=f"JSON 解析失败: {e}, 原始响应: {fixed_response}")
                    else:
                        return ToolResult(error=f"JSON 解析失败: {e}, 原始响应: {fixed_response}")

            # 修复 GUI-Plus 不规范的响应格式
            action_type = result.get("action", "").strip().upper()
            params = result.get("parameters", {})
            thought = result.get("thought", "")

            # 如果没有 action 但有坐标，推断 action 类型
            if not action_type:
                if result.get("x") is not None or params.get("x") is not None:
                    if result.get("text") or params.get("text"):
                        action_type = "TYPE"
                        if not params:
                            params = result
                    else:
                        action_type = "CLICK"
                        if not params:
                            params = result
                    logger.warning(f"[GUI-Plus] Inferred action type: {action_type}")

            # 如果 params 直接包含 x 但 y 在外面或格式错误，尝试修复
            if isinstance(params, dict):
                # 检查 params 中是否有裸的数字（如 {"x": 139, 675} 中的 675）
                # 这种情况下 JSON 解析会失败，所以需要在 JSON 解析前处理
                pass

            logger.info(f"[GUI-Plus] Decision: action={action_type}, thought={thought}")

            # 如果期望的是 type 操作，但模型只返回了 CLICK，需要先点击再输入
            if action_hint == "type" and action_type == "CLICK":
                # 从 vision_instruction 中提取引号内的文本（如 输入'上海' 中的 上海）
                text_match = re.search(r"(?:输入|填入|写入)['\"]([^'\"]+)['\"]", vision_instruction)
                if text_match:
                    text_to_type = text_match.group(1)
                    x = params.get("x")
                    y = params.get("y")
                    if x is not None and y is not None:
                        # 处理坐标格式
                        if isinstance(x, list) and len(x) >= 2:
                            x, y = x[0], x[1]
                        elif isinstance(x, list):
                            x = x[0]
                        if isinstance(y, list):
                            y = y[0]
                        try:
                            x = int(x)
                            y = int(y)
                        except (ValueError, TypeError):
                            pass
                        logger.info(f"[GUI-Plus] Click+Type: ({x}, {y}) then type '{text_to_type}'")
                        await page.mouse.click(x, y)
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Control+a")  # 全选
                        await asyncio.sleep(0.1)
                        await page.keyboard.type(text_to_type)
                        await asyncio.sleep(0.3)
                        return ToolResult(
                            output=f"[vision] 成功在 ({x}, {y}) 点击并输入: {text_to_type}\n思考过程: {thought}"
                        )

            # 5. 执行操作
            if action_type == "CLICK":
                x = params.get("x")
                y = params.get("y")
                description = params.get("description", "")

                # 处理坐标格式错误的情况
                # 情况1: {"x": [590, 206]} - x 是一个包含两个值的列表
                if isinstance(x, list) and len(x) >= 2:
                    x, y = x[0], x[1]
                elif isinstance(x, list) and len(x) == 1:
                    x = x[0]

                # 情况2: y 也可能是列表
                if isinstance(y, list) and len(y) >= 1:
                    y = y[0]

                if x is None or y is None:
                    return ToolResult(error=f"CLICK 操作缺少坐标: {params}")

                # 确保坐标是数值
                try:
                    x = int(x)
                    y = int(y)
                except (ValueError, TypeError):
                    return ToolResult(error=f"CLICK 坐标格式错误: x={x}, y={y}")

                logger.info(f"[GUI-Plus] CLICK at ({x}, {y}): {description}")

                # 调试：在截图上标记点击位置
                try:
                    from PIL import Image, ImageDraw
                    debug_screenshot = Image.open(screenshot_path)
                    draw = ImageDraw.Draw(debug_screenshot)
                    # 画一个红色十字标记点击位置
                    draw.ellipse([x-10, y-10, x+10, y+10], outline="red", width=3)
                    draw.line([x-15, y, x+15, y], fill="red", width=2)
                    draw.line([x, y-15, x, y+15], fill="red", width=2)
                    debug_path = screenshot_path.with_name(f"{screenshot_path.stem}_clicked.png")
                    debug_screenshot.save(debug_path)
                    logger.info(f"[GUI-Plus] Debug screenshot with click marker saved: {debug_path}")
                except Exception as debug_err:
                    logger.debug(f"[GUI-Plus] Failed to save debug screenshot: {debug_err}")

                await page.mouse.click(x, y)
                await asyncio.sleep(0.5)  # 等待点击生效

                return ToolResult(
                    output=f"[vision] 成功点击 ({x}, {y}): {description}\n思考过程: {thought}"
                )

            elif action_type == "TYPE":
                text_to_type = params.get("text", "")
                needs_enter = params.get("needs_enter", False)
                description = params.get("description", "输入框")

                if not text_to_type:
                    return ToolResult(error="TYPE 操作缺少文本")

                # 获取坐标（必须有坐标才能正确点击输入框）
                x = params.get("x")
                y = params.get("y")

                # 处理 x 或 y 是列表的情况
                if isinstance(x, list) and len(x) >= 2:
                    x, y = x[0], x[1]
                elif isinstance(x, list) and len(x) == 1:
                    x = x[0]
                if isinstance(y, list) and len(y) >= 1:
                    y = y[0]

                if x is not None and y is not None:
                    try:
                        x = int(x)
                        y = int(y)
                    except (ValueError, TypeError):
                        return ToolResult(error=f"TYPE 坐标格式错误: x={x}, y={y}")

                    logger.info(f"[GUI-Plus] TYPE: click ({x}, {y}) then type '{text_to_type}'")

                    # 调试：在截图上标记输入位置
                    try:
                        from PIL import Image, ImageDraw
                        debug_screenshot = Image.open(screenshot_path)
                        draw = ImageDraw.Draw(debug_screenshot)
                        # 画一个绿色方框标记输入位置
                        draw.rectangle([x-15, y-10, x+15, y+10], outline="green", width=3)
                        draw.text((x+20, y-5), text_to_type, fill="green")
                        debug_path = screenshot_path.with_name(f"{screenshot_path.stem}_typed.png")
                        debug_screenshot.save(debug_path)
                        logger.info(f"[GUI-Plus] Debug screenshot with type marker saved: {debug_path}")
                    except Exception as debug_err:
                        logger.debug(f"[GUI-Plus] Failed to save debug screenshot: {debug_err}")

                    # 先点击输入框
                    await page.mouse.click(x, y)
                    await asyncio.sleep(0.3)
                    # 全选并删除现有内容
                    await page.keyboard.press("Control+a")
                    await asyncio.sleep(0.1)
                else:
                    logger.warning(f"[GUI-Plus] TYPE: no coordinates, typing at current focus")

                # 输入文本
                await page.keyboard.type(text_to_type)
                if needs_enter:
                    await page.keyboard.press("Enter")

                await asyncio.sleep(0.3)  # 等待输入生效

                return ToolResult(
                    output=f"[vision] 成功在 ({x}, {y}) {description} 输入: {text_to_type}\n思考过程: {thought}"
                )

            elif action_type == "SCROLL":
                direction = params.get("direction", "down")
                amount = params.get("amount", "medium")

                scroll_amounts = {"small": 100, "medium": 300, "large": 600}
                pixels = scroll_amounts.get(amount, 300)
                if direction == "up":
                    pixels = -pixels

                await page.mouse.wheel(0, pixels)
                return ToolResult(output=f"[vision] 成功滚动 {direction} {amount}")

            elif action_type == "KEY_PRESS":
                key = params.get("key", "")
                if key:
                    await page.keyboard.press(key)
                    return ToolResult(output=f"[vision] 成功按下按键: {key}")
                return ToolResult(error="KEY_PRESS 操作缺少按键")

            elif action_type == "FINISH":
                message = params.get("message", "任务完成")
                return ToolResult(output=f"[vision] 任务完成: {message}")

            elif action_type == "FAIL":
                reason = params.get("reason", "未知原因")
                return ToolResult(error=f"[vision] 任务失败: {reason}")

            else:
                return ToolResult(error=f"未知的操作类型: {action_type}")

        except Exception as e:
            logger.error(f"[GUI-Plus] Execution failed: {e}")
            import traceback
            traceback.print_exc()
            return ToolResult(error=f"[vision] 执行失败: {str(e)}")

    async def _execute_smart_click(
        self, context: BrowserContext, element_description: str
    ) -> ToolResult:
        """
        智能点击：通过分析页面 HTML 找到匹配的元素并点击。
        结合 LLM 理解和 JavaScript 执行，比纯视觉坐标更精确。
        """
        try:
            page = await context.get_current_page()
            await page.bring_to_front()
            await page.wait_for_load_state()

            # 1. 获取页面的可交互元素信息（只获取视窗内可见的）
            viewport_height = await page.evaluate("window.innerHeight")
            viewport_width = await page.evaluate("window.innerWidth")

            elements_info = await page.evaluate("""
                (viewportInfo) => {
                    const elements = [];
                    const { height: vh, width: vw } = viewportInfo;

                    // 收集所有可点击元素（只在视窗内的）
                    const clickables = document.querySelectorAll('button, a, [onclick], [role="button"], input[type="submit"], input[type="button"], [class*="btn"], [class*="button"], [class*="search"]');
                    clickables.forEach((el, idx) => {
                        const rect = el.getBoundingClientRect();
                        // 只收集在视窗内的元素
                        if (rect.width > 0 && rect.height > 0 &&
                            rect.y >= 0 && rect.y < vh &&
                            rect.x >= 0 && rect.x < vw) {
                            elements.push({
                                index: idx,
                                tag: el.tagName.toLowerCase(),
                                text: (el.innerText || el.value || el.placeholder || '').trim().substring(0, 100),
                                className: el.className,
                                id: el.id,
                                type: el.type || '',
                                ariaLabel: el.getAttribute('aria-label') || '',
                                title: el.title || '',
                                href: el.href || '',
                                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                            });
                        }
                    });
                    // 收集所有 div/span 可能是按钮的元素（日期、日历等）
                    const divButtons = document.querySelectorAll('div[class*="date"], div[class*="day"], span[class*="date"], span[class*="day"], td[class*="date"], td[class*="day"]');
                    divButtons.forEach((el, idx) => {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 &&
                            rect.y >= 0 && rect.y < vh &&
                            rect.x >= 0 && rect.x < vw) {
                            elements.push({
                                index: clickables.length + idx,
                                tag: el.tagName.toLowerCase(),
                                text: (el.innerText || '').trim().substring(0, 50),
                                className: el.className,
                                id: el.id,
                                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                            });
                        }
                    });
                    return elements.slice(0, 100); // 限制数量
                }
            """, {"height": viewport_height, "width": viewport_width})

            if not elements_info:
                return ToolResult(error="[smart] 未找到可点击元素")

            # 2. 使用 LLM 找到最匹配的元素
            elements_text = "\n".join([
                f"[{e['index']}] <{e['tag']}> text='{e['text']}' class='{e.get('className', '')[:50]}' id='{e.get('id', '')}' aria='{e.get('ariaLabel', '')}'"
                for e in elements_info
            ])

            prompt = f"""根据用户描述找到最匹配的元素。

用户描述: {element_description}

页面元素:
{elements_text}

请返回最匹配的元素索引（只返回数字，如: 5）。如果没有找到匹配的元素，返回 -1。"""

            response = await self.llm.ask(
                messages=[{"role": "user", "content": prompt}],
                system_msgs=[{"role": "system", "content": "你是一个精确的页面元素匹配器。只返回元素索引数字。"}]
            )

            # 解析索引
            try:
                match = re.search(r'-?\d+', response)
                if not match:
                    return ToolResult(error=f"[smart] 无法解析元素索引: {response}")
                element_idx = int(match.group())
            except ValueError:
                return ToolResult(error=f"[smart] 无法解析元素索引: {response}")

            if element_idx < 0 or element_idx >= len(elements_info):
                # 尝试使用视觉模型作为备选
                logger.warning(f"[smart] LLM 未找到匹配元素，尝试使用视觉模型")
                return await self._execute_vision_action(context, f"点击{element_description}", "click")

            # 3. 使用 JavaScript 点击元素
            target = elements_info[element_idx]
            click_x = target['rect']['x'] + target['rect']['width'] / 2
            click_y = target['rect']['y'] + target['rect']['height'] / 2

            logger.info(f"[smart] 找到元素: [{element_idx}] {target['tag']} '{target['text'][:30]}' at ({click_x:.0f}, {click_y:.0f})")

            await page.mouse.click(click_x, click_y)
            await asyncio.sleep(0.5)

            return ToolResult(
                output=f"[smart] 成功点击元素: {target['tag']} '{target['text'][:50]}' at ({click_x:.0f}, {click_y:.0f})"
            )

        except Exception as e:
            logger.error(f"[smart] 点击失败: {e}")
            # 回退到视觉模型
            return await self._execute_vision_action(context, f"点击{element_description}", "click")

    async def _execute_smart_input(
        self, context: BrowserContext, element_description: str, text: str
    ) -> ToolResult:
        """
        智能输入：通过分析页面 HTML 找到输入框并输入文本。
        结合 LLM 理解和 JavaScript 执行，比纯视觉坐标更精确。
        """
        try:
            page = await context.get_current_page()
            await page.bring_to_front()
            await page.wait_for_load_state()

            # 1. 获取页面的所有输入元素（只获取视窗内可见的）
            viewport_height = await page.evaluate("window.innerHeight")
            viewport_width = await page.evaluate("window.innerWidth")

            inputs_info = await page.evaluate("""
                (viewportInfo) => {
                    const inputs = [];
                    const { height: vh, width: vw } = viewportInfo;

                    // 收集所有输入元素
                    const inputElements = document.querySelectorAll('input[type="text"], input[type="search"], input:not([type]), textarea, [contenteditable="true"], [role="textbox"], [role="combobox"]');
                    inputElements.forEach((el, idx) => {
                        const rect = el.getBoundingClientRect();
                        // 只收集在视窗内的元素
                        if (rect.width > 0 && rect.height > 0 &&
                            rect.y >= 0 && rect.y < vh &&
                            rect.x >= 0 && rect.x < vw) {
                            // 尝试获取关联的 label
                            let labelText = '';
                            if (el.id) {
                                const label = document.querySelector(`label[for="${el.id}"]`);
                                if (label) labelText = label.innerText;
                            }
                            // 检查父元素的文本
                            const parentText = el.parentElement ? (el.parentElement.innerText || '').split('\\n')[0] : '';

                            inputs.push({
                                index: idx,
                                tag: el.tagName.toLowerCase(),
                                placeholder: el.placeholder || '',
                                value: el.value || '',
                                name: el.name || '',
                                id: el.id || '',
                                className: el.className || '',
                                ariaLabel: el.getAttribute('aria-label') || '',
                                labelText: labelText,
                                parentText: parentText.substring(0, 50),
                                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                            });
                        }
                    });
                    return inputs;
                }
            """, {"height": viewport_height, "width": viewport_width})

            if not inputs_info:
                return ToolResult(error="[smart] 未找到输入框元素")

            # 2. 使用 LLM 找到最匹配的输入框
            inputs_text = "\n".join([
                f"[{i['index']}] <{i['tag']}> placeholder='{i['placeholder']}' value='{i['value'][:20]}' label='{i['labelText']}' parent='{i['parentText']}' aria='{i['ariaLabel']}'"
                for i in inputs_info
            ])

            prompt = f"""根据用户描述找到最匹配的输入框。

用户描述: {element_description}

输入框列表:
{inputs_text}

请返回最匹配的输入框索引（只返回数字，如: 0）。如果没有找到匹配的输入框，返回 -1。"""

            response = await self.llm.ask(
                messages=[{"role": "user", "content": prompt}],
                system_msgs=[{"role": "system", "content": "你是一个精确的页面元素匹配器。只返回元素索引数字。"}]
            )

            # 解析索引
            try:
                match = re.search(r'-?\d+', response)
                if not match:
                    return ToolResult(error=f"[smart] 无法解析输入框索引: {response}")
                input_idx = int(match.group())
            except ValueError:
                return ToolResult(error=f"[smart] 无法解析输入框索引: {response}")

            if input_idx < 0 or input_idx >= len(inputs_info):
                # 尝试使用视觉模型作为备选
                logger.warning(f"[smart] LLM 未找到匹配输入框，尝试使用视觉模型")
                return await self._execute_vision_action(context, f"在{element_description}输入'{text}'", "type")

            # 3. 使用 JavaScript 直接聚焦并输入
            target = inputs_info[input_idx]
            click_x = target['rect']['x'] + target['rect']['width'] / 2
            click_y = target['rect']['y'] + target['rect']['height'] / 2

            logger.info(f"[smart] 找到输入框: [{input_idx}] placeholder='{target['placeholder']}' at ({click_x:.0f}, {click_y:.0f})")

            # 使用 JavaScript 找到并聚焦输入框
            # 这比坐标点击更可靠，特别是对于动态出现的输入框
            await page.evaluate(f"""
                () => {{
                    const inputs = document.querySelectorAll('input[type="text"], input[type="search"], input:not([type]), textarea, [contenteditable="true"], [role="textbox"], [role="combobox"]');
                    const visibleInputs = Array.from(inputs).filter(el => {{
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && rect.y >= 0 && rect.y < window.innerHeight;
                    }});
                    if (visibleInputs[{input_idx}]) {{
                        visibleInputs[{input_idx}].focus();
                        visibleInputs[{input_idx}].select();
                    }}
                }}
            """)
            await asyncio.sleep(0.2)

            # 清空并输入新文本
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.type(text)
            await asyncio.sleep(0.3)

            return ToolResult(
                output=f"[smart] 成功在输入框 '{target['placeholder'] or target['labelText'] or element_description}' 中输入: {text}"
            )

        except Exception as e:
            logger.error(f"[smart] 输入失败: {e}")
            # 回退到视觉模型
            return await self._execute_vision_action(context, f"在{element_description}输入'{text}'", "type")

    # ========== 简化的 click 和 type 接口 ==========

    async def _click(self, context: BrowserContext, element_description: str) -> ToolResult:
        """
        简化的点击操作，自动选择最佳策略：
        1. 如果是日期类描述，直接使用视觉模型
        2. 尝试通过 JavaScript 查找包含指定文字的元素
        3. 如果失败，使用视觉模型

        Args:
            context: 浏览器上下文
            element_description: 要点击的元素描述（如"搜索按钮"、"1月30日"）

        Returns:
            ToolResult
        """
        try:
            page = await context.get_current_page()
            await page.bring_to_front()
            await page.wait_for_load_state()

            logger.info(f"[click] 尝试点击: '{element_description}'")

            # 检查是否是日期类描述 - 纯数字或包含"日"、"月"
            is_date_like = (
                element_description.isdigit() or
                re.search(r'^\d+[日号]?$', element_description) or
                re.search(r'\d+月\d+[日号]?', element_description) or
                "日历" in element_description or
                "calendar" in element_description.lower()
            )

            if is_date_like:
                logger.info(f"[click] 检测到日期类描述，尝试 Playwright locator")
                # 提取数字
                match = re.search(r'(\d+)', element_description)
                if match:
                    day_num = match.group(1)
                    try:
                        # 使用 Playwright 定位日历中的日期
                        locator = page.locator(f"text={day_num}").last
                        if await locator.is_visible():
                            await locator.click()
                            await asyncio.sleep(0.5)
                            return ToolResult(
                                output=f"[click] Playwright 点击日期: {day_num}"
                            )
                    except Exception as e:
                        logger.debug(f"[click] Playwright 日期定位失败: {e}")

                # 回退到视觉模型
                return await self._execute_vision_action(context, f"点击日历中的{element_description}", "click")

            # 策略1: 使用 Playwright locator 精确查找（优先文字精确匹配）
            try:
                # 尝试精确文字匹配
                locator = page.get_by_text(element_description, exact=True)
                if await locator.count() > 0:
                    # 找到精确匹配，点击第一个可见的
                    for i in range(await locator.count()):
                        el = locator.nth(i)
                        if await el.is_visible():
                            box = await el.bounding_box()
                            if box and box['y'] < 600:  # 只点击上半部分页面的元素
                                click_x = box['x'] + box['width'] / 2
                                click_y = box['y'] + box['height'] / 2
                                logger.info(f"[click] 精确匹配: '{element_description}' at ({click_x:.0f}, {click_y:.0f})")
                                await page.mouse.click(click_x, click_y)
                                await asyncio.sleep(0.5)
                                return ToolResult(
                                    output=f"[click] 成功点击: '{element_description}' at ({click_x:.0f}, {click_y:.0f})"
                                )

                # 尝试包含文字匹配（但要求元素文字长度不能太长）
                locator = page.get_by_text(element_description, exact=False)
                if await locator.count() > 0:
                    for i in range(min(await locator.count(), 10)):  # 最多检查10个
                        el = locator.nth(i)
                        if await el.is_visible():
                            text_content = await el.text_content()
                            # 只接受文字长度不超过描述3倍的元素
                            if text_content and len(text_content.strip()) <= len(element_description) * 3:
                                box = await el.bounding_box()
                                if box and box['y'] < 600:
                                    click_x = box['x'] + box['width'] / 2
                                    click_y = box['y'] + box['height'] / 2
                                    logger.info(f"[click] 包含匹配: '{text_content[:30]}' at ({click_x:.0f}, {click_y:.0f})")
                                    await page.mouse.click(click_x, click_y)
                                    await asyncio.sleep(0.5)
                                    return ToolResult(
                                        output=f"[click] 成功点击: '{text_content[:30]}' at ({click_x:.0f}, {click_y:.0f})"
                                    )
            except Exception as e:
                logger.debug(f"[click] Playwright locator 失败: {e}")

            # 策略1.5: 针对真实 <button>/<input type=submit>/[role=button] 的兜底匹配
            # 当文字匹配失败时，直接遍历可见且可用的按钮元素，按规范化文字宽松匹配。
            # 解决"搜索按钮"等描述匹配不到按钮真实文字"搜　索"(全角空格)的问题。
            try:
                norm_desc = _norm_text(element_description)
                buttons = await page.evaluate("""() => {
                    const out = [];
                    const sels = 'button, input[type="submit"], [role="button"], [role="submit"]';
                    document.querySelectorAll(sels).forEach(el => {
                        if (el.disabled) return;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 2 || r.height <= 2) return;
                        if (r.y < 0 || r.y > window.innerHeight) return;
                        out.push({
                            text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
                            x: r.x, y: r.y, w: r.width, h: r.height
                        });
                    });
                    return out;
                }""")
                # 收集所有候选按钮并按匹配质量排序，避免误命中"高级搜索"等含关键词的无关按钮
                candidates = []
                for b in buttons:
                    norm_btn = _norm_text(b.get("text", ""))
                    if not norm_btn:
                        continue
                    y = b.get("y", 0)
                    # 优先级：精确相等 > 描述含按钮文字 > 按钮文字含描述 > 同关键词组
                    score = 0
                    if norm_desc == norm_btn:
                        score = 100
                    elif norm_btn in norm_desc:
                        score = 80  # 按钮"搜索"在描述"搜索按钮"里
                    elif norm_desc in norm_btn:
                        score = 40  # 描述"搜索"在按钮"高级搜索"里（弱匹配）
                    else:
                        same_group = any(
                            any(k in norm_desc for k in g) and any(k in norm_btn for k in g)
                            for g in _BUTTON_KEYWORD_GROUPS
                        )
                        if same_group:
                            score = 20
                    if score > 0:
                        # 偏好表单区按钮（y较大），文字更接近描述的（文字越短越接近）
                        y_pref = 5 if y >= 100 else 0
                        candidates.append((score + y_pref, len(norm_btn), b))
                if candidates:
                    # 按 score 降序、文字长度升序（最接近描述的优先）选最佳
                    candidates.sort(key=lambda c: (-c[0], c[1]))
                    best = candidates[0][2]
                    click_x = best["x"] + best["w"] / 2
                    click_y = best["y"] + best["h"] / 2
                    logger.info(f"[click] 按钮兜底匹配: '{best['text']}' at ({click_x:.0f}, {click_y:.0f}) (候选{len(candidates)}个)")
                    await page.mouse.click(click_x, click_y)
                    await asyncio.sleep(0.5)
                    return ToolResult(
                        output=f"[click] 成功点击按钮: '{best['text']}' at ({click_x:.0f}, {click_y:.0f})"
                    )
            except Exception as e:
                logger.debug(f"[click] 按钮兜底匹配失败: {e}")

            # 策略2: 回退到视觉模型
            logger.info(f"[click] JavaScript 未找到匹配元素，使用视觉模型")
            return await self._execute_vision_action(context, f"点击{element_description}", "click")

        except Exception as e:
            logger.error(f"[click] 点击失败: {e}")
            # 最终回退到视觉模型
            return await self._execute_vision_action(context, f"点击{element_description}", "click")

    async def _type(self, context: BrowserContext, element_description: str, text: str) -> ToolResult:
        """
        简化的输入操作，自动选择最佳策略：
        1. 先尝试通过 JavaScript 找到输入框
        2. 如果失败，使用视觉模型识别并输入

        Args:
            context: 浏览器上下文
            element_description: 输入框描述（如"出发城市"、"搜索框"）
            text: 要输入的文本

        Returns:
            ToolResult
        """
        try:
            page = await context.get_current_page()
            await page.bring_to_front()
            await page.wait_for_load_state()

            logger.info(f"[type] 尝试在 '{element_description}' 输入: '{text}'")

            # 策略1: 尝试通过 JavaScript 找到输入框
            inputs = await page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input[type="text"], input[type="search"], input:not([type]), textarea, [contenteditable="true"], [role="textbox"], [role="combobox"]');
                    const results = [];
                    for (const el of inputs) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (rect.y < 0 || rect.y > window.innerHeight) continue;

                        const placeholder = el.getAttribute('placeholder') || '';
                        const ariaLabel = el.getAttribute('aria-label') || '';
                        const name = el.getAttribute('name') || '';
                        const value = el.value || '';

                        // 查找关联的 label
                        let labelText = '';
                        if (el.id) {
                            const label = document.querySelector(`label[for="${el.id}"]`);
                            if (label) labelText = label.textContent?.trim() || '';
                        }
                        // 检查父元素中的文字
                        const parent = el.closest('div, label, li');
                        const parentText = parent ? parent.textContent?.trim().substring(0, 30) : '';

                        results.push({
                            placeholder: placeholder,
                            ariaLabel: ariaLabel,
                            name: name,
                            value: value,
                            labelText: labelText,
                            parentText: parentText,
                            rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                        });
                    }
                    return results;
                }
            """)

            # 构建输入框描述列表供 LLM 分析
            if inputs and len(inputs) > 0:
                inputs_text = "\n".join([
                    f"[{i}] placeholder='{inp['placeholder']}' label='{inp['labelText']}' aria='{inp['ariaLabel']}' value='{inp['value'][:20]}' parent='{inp['parentText'][:20]}'"
                    for i, inp in enumerate(inputs)
                ])

                prompt = f"""找到最匹配的输入框。

用户想在这里输入: {element_description}

输入框列表:
{inputs_text}

返回最匹配的输入框索引（只返回数字，如: 0）。如果没有匹配项，返回 -1。"""

                response = await self.llm.ask(
                    messages=[{"role": "user", "content": prompt}],
                    system_msgs=[{"role": "system", "content": "你是一个精确的页面元素匹配器。只返回元素索引数字。"}]
                )

                # 解析索引
                try:
                    match = re.search(r'-?\d+', response)
                    if match:
                        idx = int(match.group())
                        if 0 <= idx < len(inputs):
                            target = inputs[idx]
                            click_x = target['rect']['x'] + target['rect']['width'] / 2
                            click_y = target['rect']['y'] + target['rect']['height'] / 2

                            logger.info(f"[type] 找到输入框: [{idx}] placeholder='{target['placeholder']}' at ({click_x:.0f}, {click_y:.0f})")

                            # 点击激活输入框
                            await page.mouse.click(click_x, click_y)
                            await asyncio.sleep(0.3)

                            # 全选并输入
                            await page.keyboard.press("Control+a")
                            await asyncio.sleep(0.1)
                            await page.keyboard.type(text)
                            await asyncio.sleep(0.3)

                            # 尝试选择下拉联想项（如城市输入框的联想列表）。
                            # 失败不阻断：最坏退化为原行为（仅输入文字）。
                            await self._try_select_suggestion(page, text)

                            return ToolResult(
                                output=f"[type] 成功在 '{target['placeholder'] or target['labelText'] or element_description}' 中输入: {text}"
                            )
                except (ValueError, IndexError):
                    pass

            # 策略2: 回退到视觉模型
            logger.info(f"[type] JavaScript 未找到匹配输入框，使用视觉模型")
            return await self._execute_vision_action(context, f"在{element_description}输入'{text}'", "type")

        except Exception as e:
            logger.error(f"[type] 输入失败: {e}")
            # 最终回退到视觉模型
            return await self._execute_vision_action(context, f"在{element_description}输入'{text}'", "type")

    async def _try_select_suggestion(self, page, text: str) -> None:
        """输入文字后尝试选择下拉联想项（如城市联想列表）。

        优先点击文字匹配的可见联想项；找不到则按回车兜底。
        任何异常都吞掉，避免影响主输入流程。
        """
        async def _pick():
            return await page.evaluate(
                """(text) => {
                    const norm = (s) => (s || '').replace(/\\s|　/g, '').trim();
                    const target = norm(text);
                    const cands = [];
                    const sels = '[role="option"], [role="listbox"] *, li.city, .city-item, .suggestion, .ac_results li, .autocomplete li, .dropdown-item, a.js-hotcitylist, .m-hct-nav a, .m-hct-nav li, .qcbox-dropdown a, .qcity a, [class*="hct"] a, [class*="hct"] li';
                    document.querySelectorAll(sels).forEach(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return;
                        const t = norm(el.innerText || el.getAttribute('data-text') || '');
                        if (!t) return;
                        cands.push({text: el.innerText.trim(), x: r.x, y: r.y, w: r.width, h: r.height, norm: t});
                    });
                    // 优先文字完全匹配，其次包含关系
                    let best = cands.find(c => c.norm === target);
                    if (!best) best = cands.find(c => c.norm.includes(target) || target.includes(c.norm));
                    if (best) return best;
                    return null;
                }""",
                text,
            )

        try:
            await asyncio.sleep(0.6)  # 等待联想下拉出现
            picked = await _pick()
            # 首次未找到则再等一次（面板可能延迟弹出）
            if not picked:
                await asyncio.sleep(0.8)
                picked = await _pick()
            if picked:
                click_x = picked["x"] + picked["w"] / 2
                click_y = picked["y"] + picked["h"] / 2
                logger.info(f"[type] 选择联想项: '{picked['text']}' at ({click_x:.0f}, {click_y:.0f})")
                await page.mouse.click(click_x, click_y)
                await asyncio.sleep(0.4)
                return

            # 兜底：按回车（部分表单回车即提交/确认）
            logger.debug(f"[type] 未找到联想项，按 Enter 兜底")
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"[type] 选择联想项失败(忽略): {e}")

    async def get_current_state(
        self, context: Optional[BrowserContext] = None
    ) -> ToolResult:
        """
        获取当前浏览器状态作为 ToolResult。
        如果未提供 context，则使用 self.context。
        """
        try:
            # 使用提供的 context 或回退到 self.context
            ctx = context or self.context
            if not ctx:
                return ToolResult(error="Browser context not initialized")

            state = await ctx.get_state()

            # 如果不存在，创建 viewport_info 字典
            viewport_height = 0
            if hasattr(state, "viewport_info") and state.viewport_info:
                viewport_height = state.viewport_info.height
            elif hasattr(ctx, "config") and hasattr(ctx.config, "browser_window_size"):
                viewport_height = ctx.config.browser_window_size.get("height", 0)

            # 为状态拍摄截图
            page = await ctx.get_current_page()

            await page.bring_to_front()
            await page.wait_for_load_state()

            screenshot = await page.screenshot(
                full_page=True, animations="disabled", type="jpeg", quality=100
            )

            screenshot = base64.b64encode(screenshot).decode("utf-8")
            screenshot_size_kb = len(screenshot) * 3 / 4 / 1024  # 估算图片大小（KB）

            # 获取可交互元素信息（用于基础操作，复杂元素如日期选择器使用 vision_click）
            interactive_elements_str = (
                state.element_tree.clickable_elements_to_string()
                if state.element_tree
                else ""
            )
            element_count = interactive_elements_str.count("[") if interactive_elements_str else 0

            # 调试信息
            logger.info(f"[browser] URL: {state.url}")
            logger.info(f"[browser] Elements: {element_count} | Screenshot: {screenshot_size_kb:.1f}KB")
            if element_count == 0:
                logger.warning("[browser] No elements found - page may be empty")
                # 若同时是拦截页，明确提示风控，便于诊断
                try:
                    page_for_check = await self.context.get_current_page()
                    page_html = await page_for_check.content()
                    if any(m in page_html.lower() for m in _BLOCKED_MARKERS):
                        logger.warning(
                            f"[browser] 检测到风控拦截页(whaleguard)，当前 URL 被反爬屏蔽: {state.url}"
                        )
                except Exception:
                    pass
            elif interactive_elements_str:
                # 显示前几个元素作为示例
                lines = interactive_elements_str.split("\n")[:5]
                preview = "\n".join(lines)
                logger.debug(f"[browser] Elements preview:\n{preview}")

            # 保存 HTML 用于调试（特别是日期选择器问题）
            try:
                html_content = await page.content()
                debug_dir = Path("debug_html")
                debug_dir.mkdir(exist_ok=True)

                # 生成文件名：包含时间戳和 URL 的简化版本
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                # 移除 URL 中的协议、特殊字符，保留安全的文件名
                url_safe = state.url.replace("https://", "").replace("http://", "")
                url_safe = re.sub(r'[?#&=:/<>"|*\\]', '_', url_safe)[:50]  # 移除非法文件名字符
                filename = f"{timestamp}_{url_safe}.html"
                filepath = debug_dir / filename

                # 保存 HTML
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(html_content)

                logger.info(f"💾 已保存网页 HTML 到: {filepath}")

                # 如果检测到可能是日期选择器页面，额外保存元素信息
                if "flights.ctrip.com" in state.url and element_count < 150:
                    # 可能是日期选择器打开后的页面
                    elements_file = debug_dir / f"{timestamp}_elements.txt"
                    with open(elements_file, "w", encoding="utf-8") as f:
                        f.write(f"URL: {state.url}\n")
                        f.write(f"Title: {state.title}\n")
                        f.write(f"Element Count: {element_count}\n")
                        f.write(f"\n=== All Interactive Elements ===\n")
                        f.write(interactive_elements_str)
                    logger.info(f"💾 已保存元素信息到: {elements_file}")

            except Exception as e:
                logger.warning(f"⚠️ 保存 HTML 调试文件失败: {e}")

            # 构建包含所有必需字段的状态信息
            state_info = {
                "url": state.url,
                "title": state.title,
                "tabs": [tab.model_dump() for tab in state.tabs],
                "help": "[0], [1], [2], etc., represent clickable indices corresponding to the elements listed. Clicking on these indices will navigate to or interact with the respective content behind them.",
                "interactive_elements": interactive_elements_str,
                "scroll_info": {
                    "pixels_above": getattr(state, "pixels_above", 0),
                    "pixels_below": getattr(state, "pixels_below", 0),
                    "total_height": getattr(state, "pixels_above", 0)
                    + getattr(state, "pixels_below", 0)
                    + viewport_height,
                },
                "viewport_height": viewport_height,
            }

            return ToolResult(
                output=json.dumps(state_info, indent=4, ensure_ascii=False),
                base64_image=screenshot,
            )
        except Exception as e:
            return ToolResult(error=f"Failed to get browser state: {str(e)}")

    async def cleanup(self):
        """清理浏览器资源。"""
        async with self.lock:
            if self.context is not None:
                await self.context.close()
                self.context = None
                self.dom_service = None
            if self.browser is not None:
                await self.browser.close()
                self.browser = None

    def __del__(self):
        """确保在对象销毁时进行清理。"""
        if self.browser is not None or self.context is not None:
            try:
                asyncio.run(self.cleanup())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.cleanup())
                loop.close()

    @classmethod
    def create_with_context(cls, context: Context) -> "BrowserUseTool[Context]":
        """创建具有特定上下文的 BrowserUseTool 的工厂方法。"""
        tool = cls()
        tool.tool_context = context
        return tool
