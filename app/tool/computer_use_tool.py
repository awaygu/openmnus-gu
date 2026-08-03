import asyncio
import base64
import logging
import os
import time
from typing import Dict, Literal, Optional

import aiohttp
from pydantic import Field

from app.daytona.tool_base import Sandbox, SandboxToolsBase
from app.tool.base import ToolResult


KEYBOARD_KEYS = [
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "q",
    "r",
    "s",
    "t",
    "u",
    "v",
    "w",
    "x",
    "y",
    "z",
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "enter",
    "esc",
    "backspace",
    "tab",
    "space",
    "delete",
    "ctrl",
    "alt",
    "shift",
    "win",
    "up",
    "down",
    "left",
    "right",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f11",
    "f12",
    "ctrl+c",
    "ctrl+v",
    "ctrl+x",
    "ctrl+z",
    "ctrl+a",
    "ctrl+s",
    "alt+tab",
    "alt+f4",
    "ctrl+alt+delete",
]
MOUSE_BUTTONS = ["left", "right", "middle"]
_COMPUTER_USE_DESCRIPTION = """\
一个全面的计算机自动化工具，允许与桌面环境交互。
* 此工具提供用于控制鼠标、键盘和截图的命令
* 它维护状态，包括当前鼠标位置
* 当你需要自动化桌面应用程序、填写表单或执行 GUI 交互时使用此工具
主要功能包括：
* 鼠标控制：移动、点击、拖拽、滚动
* 键盘输入：输入文本、按下按键或组合键
* 截图：捕获并保存屏幕图像
* 等待：暂停执行指定持续时间
"""


class ComputerUseTool(SandboxToolsBase):
    """用于控制桌面环境的计算机自动化工具。"""

    name: str = "computer_use"
    description: str = _COMPUTER_USE_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "move_to",
                    "click",
                    "scroll",
                    "typing",
                    "press",
                    "wait",
                    "mouse_down",
                    "mouse_up",
                    "drag_to",
                    "hotkey",
                    "screenshot",
                ],
                "description": "要执行的计算机操作",
            },
            "x": {"type": "number", "description": "鼠标操作的 X 坐标"},
            "y": {"type": "number", "description": "鼠标操作的 Y 坐标"},
            "button": {
                "type": "string",
                "enum": MOUSE_BUTTONS,
                "description": "用于点击/拖拽操作的鼠标按钮",
                "default": "left",
            },
            "num_clicks": {
                "type": "integer",
                "description": "点击次数",
                "enum": [1, 2, 3],
                "default": 1,
            },
            "amount": {
                "type": "integer",
                "description": "滚动量（正数向上，负数向下）",
                "minimum": -10,
                "maximum": 10,
            },
            "text": {"type": "string", "description": "要输入的文本"},
            "key": {
                "type": "string",
                "enum": KEYBOARD_KEYS,
                "description": "要按下的按键",
            },
            "keys": {
                "type": "string",
                "enum": KEYBOARD_KEYS,
                "description": "要按下的组合键",
            },
            "duration": {
                "type": "number",
                "description": "要等待的持续时间（秒）",
                "default": 0.5,
            },
        },
        "required": ["action"],
        "dependencies": {
            "move_to": ["x", "y"],
            "click": [],
            "scroll": ["amount"],
            "typing": ["text"],
            "press": ["key"],
            "wait": [],
            "mouse_down": [],
            "mouse_up": [],
            "drag_to": ["x", "y"],
            "hotkey": ["keys"],
            "screenshot": [],
        },
    }
    session: Optional[aiohttp.ClientSession] = Field(default=None, exclude=True)
    mouse_x: int = Field(default=0, exclude=True)
    mouse_y: int = Field(default=0, exclude=True)
    api_base_url: Optional[str] = Field(default=None, exclude=True)

    def __init__(self, sandbox: Optional[Sandbox] = None, **data):
        """使用可选的沙箱初始化。"""
        super().__init__(**data)
        if sandbox is not None:
            self._sandbox = sandbox  # 直接操作基类的私有属性
            self.api_base_url = sandbox.get_preview_link(8000).url
            logging.info(
                f"Initialized ComputerUseTool with API URL: {self.api_base_url}"
            )

    @classmethod
    def create_with_sandbox(cls, sandbox: Sandbox) -> "ComputerUseTool":
        """创建带有沙箱的工具的工厂方法。"""
        return cls(sandbox=sandbox)  # 通过构造函数初始化

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建用于 API 请求的 aiohttp 会话。"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _api_request(
        self, method: str, endpoint: str, data: Optional[Dict] = None
    ) -> Dict:
        """向自动化服务 API 发送请求。"""
        try:
            session = await self._get_session()
            url = f"{self.api_base_url}/api{endpoint}"
            logging.debug(f"API request: {method} {url} {data}")
            if method.upper() == "GET":
                async with session.get(url) as response:
                    result = await response.json()
            else:  # POST
                async with session.post(url, json=data) as response:
                    result = await response.json()
            logging.debug(f"API response: {result}")
            return result
        except Exception as e:
            logging.error(f"API request failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def execute(
        self,
        action: Literal[
            "move_to",
            "click",
            "scroll",
            "typing",
            "press",
            "wait",
            "mouse_down",
            "mouse_up",
            "drag_to",
            "hotkey",
            "screenshot",
        ],
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: str = "left",
        num_clicks: int = 1,
        amount: Optional[int] = None,
        text: Optional[str] = None,
        key: Optional[str] = None,
        keys: Optional[str] = None,
        duration: float = 0.5,
        **kwargs,
    ) -> ToolResult:
        """
        执行指定的计算机自动化操作。
        Args:
            action: 要执行的操作
            x: 鼠标操作的 X 坐标
            y: 鼠标操作的 Y 坐标
            button: 用于点击/拖拽操作的鼠标按钮
            num_clicks: 要执行的点击次数
            amount: 滚动量（正数向上，负数向下）
            text: 要输入的文本
            key: 要按下的按键
            keys: 要按下的组合键
            duration: 要等待的持续时间（秒）
            **kwargs: 其他参数
        Returns:
            包含操作输出或错误的 ToolResult
        """
        try:
            if action == "move_to":
                if x is None or y is None:
                    return ToolResult(error="x and y coordinates are required")
                x_int = int(round(float(x)))
                y_int = int(round(float(y)))
                result = await self._api_request(
                    "POST", "/automation/mouse/move", {"x": x_int, "y": y_int}
                )
                if result.get("success", False):
                    self.mouse_x = x_int
                    self.mouse_y = y_int
                    return ToolResult(output=f"Moved to ({x_int}, {y_int})")
                else:
                    return ToolResult(
                        error=f"Failed to move: {result.get('error', 'Unknown error')}"
                    )
            elif action == "click":
                x_val = x if x is not None else self.mouse_x
                y_val = y if y is not None else self.mouse_y
                x_int = int(round(float(x_val)))
                y_int = int(round(float(y_val)))
                num_clicks = int(num_clicks)
                result = await self._api_request(
                    "POST",
                    "/automation/mouse/click",
                    {
                        "x": x_int,
                        "y": y_int,
                        "clicks": num_clicks,
                        "button": button.lower(),
                    },
                )
                if result.get("success", False):
                    self.mouse_x = x_int
                    self.mouse_y = y_int
                    return ToolResult(
                        output=f"{num_clicks} {button} click(s) performed at ({x_int}, {y_int})"
                    )
                else:
                    return ToolResult(
                        error=f"Failed to click: {result.get('error', 'Unknown error')}"
                    )
            elif action == "scroll":
                if amount is None:
                    return ToolResult(error="Scroll amount is required")
                amount = int(float(amount))
                amount = max(-10, min(10, amount))
                result = await self._api_request(
                    "POST",
                    "/automation/mouse/scroll",
                    {"clicks": amount, "x": self.mouse_x, "y": self.mouse_y},
                )
                if result.get("success", False):
                    direction = "up" if amount > 0 else "down"
                    steps = abs(amount)
                    return ToolResult(
                        output=f"Scrolled {direction} {steps} step(s) at position ({self.mouse_x}, {self.mouse_y})"
                    )
                else:
                    return ToolResult(
                        error=f"Failed to scroll: {result.get('error', 'Unknown error')}"
                    )
            elif action == "typing":
                if text is None:
                    return ToolResult(error="Text is required for typing")
                text = str(text)
                result = await self._api_request(
                    "POST",
                    "/automation/keyboard/write",
                    {"message": text, "interval": 0.01},
                )
                if result.get("success", False):
                    return ToolResult(output=f"Typed: {text}")
                else:
                    return ToolResult(
                        error=f"Failed to type: {result.get('error', 'Unknown error')}"
                    )
            elif action == "press":
                if key is None:
                    return ToolResult(error="Key is required for press action")
                key = str(key).lower()
                result = await self._api_request(
                    "POST", "/automation/keyboard/press", {"keys": key, "presses": 1}
                )
                if result.get("success", False):
                    return ToolResult(output=f"Pressed key: {key}")
                else:
                    return ToolResult(
                        error=f"Failed to press key: {result.get('error', 'Unknown error')}"
                    )
            elif action == "wait":
                duration = float(duration)
                duration = max(0, min(10, duration))
                await asyncio.sleep(duration)
                return ToolResult(output=f"Waited {duration} seconds")
            elif action == "mouse_down":
                x_val = x if x is not None else self.mouse_x
                y_val = y if y is not None else self.mouse_y
                x_int = int(round(float(x_val)))
                y_int = int(round(float(y_val)))
                result = await self._api_request(
                    "POST",
                    "/automation/mouse/down",
                    {"x": x_int, "y": y_int, "button": button.lower()},
                )
                if result.get("success", False):
                    self.mouse_x = x_int
                    self.mouse_y = y_int
                    return ToolResult(
                        output=f"{button} button pressed at ({x_int}, {y_int})"
                    )
                else:
                    return ToolResult(
                        error=f"Failed to press button: {result.get('error', 'Unknown error')}"
                    )
            elif action == "mouse_up":
                x_val = x if x is not None else self.mouse_x
                y_val = y if y is not None else self.mouse_y
                x_int = int(round(float(x_val)))
                y_int = int(round(float(y_val)))
                result = await self._api_request(
                    "POST",
                    "/automation/mouse/up",
                    {"x": x_int, "y": y_int, "button": button.lower()},
                )
                if result.get("success", False):
                    self.mouse_x = x_int
                    self.mouse_y = y_int
                    return ToolResult(
                        output=f"{button} button released at ({x_int}, {y_int})"
                    )
                else:
                    return ToolResult(
                        error=f"Failed to release button: {result.get('error', 'Unknown error')}"
                    )
            elif action == "drag_to":
                if x is None or y is None:
                    return ToolResult(error="x and y coordinates are required")
                target_x = int(round(float(x)))
                target_y = int(round(float(y)))
                start_x = self.mouse_x
                start_y = self.mouse_y
                result = await self._api_request(
                    "POST",
                    "/automation/mouse/drag",
                    {"x": target_x, "y": target_y, "duration": 0.3, "button": "left"},
                )
                if result.get("success", False):
                    self.mouse_x = target_x
                    self.mouse_y = target_y
                    return ToolResult(
                        output=f"Dragged from ({start_x}, {start_y}) to ({target_x}, {target_y})"
                    )
                else:
                    return ToolResult(
                        error=f"Failed to drag: {result.get('error', 'Unknown error')}"
                    )
            elif action == "hotkey":
                if keys is None:
                    return ToolResult(error="Keys are required for hotkey action")
                keys = str(keys).lower().strip()
                key_sequence = keys.split("+")
                result = await self._api_request(
                    "POST",
                    "/automation/keyboard/hotkey",
                    {"keys": key_sequence, "interval": 0.01},
                )
                if result.get("success", False):
                    return ToolResult(output=f"Pressed key combination: {keys}")
                else:
                    return ToolResult(
                        error=f"Failed to press keys: {result.get('error', 'Unknown error')}"
                    )
            elif action == "screenshot":
                result = await self._api_request("POST", "/automation/screenshot")
                if "image" in result:
                    base64_str = result["image"]
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    # 将截图保存到文件
                    screenshots_dir = "screenshots"
                    if not os.path.exists(screenshots_dir):
                        os.makedirs(screenshots_dir)
                    timestamped_filename = os.path.join(
                        screenshots_dir, f"screenshot_{timestamp}.png"
                    )
                    latest_filename = "latest_screenshot.png"
                    # 解码 base64 字符串并保存到文件
                    img_data = base64.b64decode(base64_str)
                    with open(timestamped_filename, "wb") as f:
                        f.write(img_data)
                    # 保存一个副本作为最新截图
                    with open(latest_filename, "wb") as f:
                        f.write(img_data)
                    return ToolResult(
                        output=f"Screenshot saved as {timestamped_filename}",
                        base64_image=base64_str,
                    )
                else:
                    return ToolResult(error="Failed to capture screenshot")
            else:
                return ToolResult(error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(error=f"Computer action failed: {str(e)}")

    async def cleanup(self):
        """清理资源。"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

    def __del__(self):
        """确保在销毁时进行清理。"""
        if hasattr(self, "session") and self.session is not None:
            try:
                asyncio.run(self.cleanup())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.cleanup())
                loop.close()
