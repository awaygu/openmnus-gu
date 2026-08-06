import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field

from app.utils.logger import logger


# class BaseTool(ABC, BaseModel):
#     name: str
#     description: str
#     parameters: Optional[dict] = None

#     class Config:
#         arbitrary_types_allowed = True

#     async def __call__(self, **kwargs) -> Any:
#         """Execute the tool with given parameters."""
#         return await self.execute(**kwargs)

#     @abstractmethod
#     async def execute(self, **kwargs) -> Any:
#         """Execute the tool with given parameters."""

#     def to_param(self) -> Dict:
#         """Convert tool to function call format."""
#         return {
#             "type": "function",
#             "function": {
#                 "name": self.name,
#                 "description": self.description,
#                 "parameters": self.parameters,
#             },
#         }


class ToolResult(BaseModel):
    """表示工具执行的结果。"""

    output: Any = Field(default=None)
    error: Optional[str] = Field(default=None)
    base64_image: Optional[str] = Field(default=None)
    system: Optional[str] = Field(default=None)

    class Config:
        arbitrary_types_allowed = True

    def __bool__(self):
        return any(getattr(self, field) for field in self.__fields__)

    def __add__(self, other: "ToolResult"):
        def combine_fields(
            field: Optional[str], other_field: Optional[str], concatenate: bool = True
        ):
            if field and other_field:
                if concatenate:
                    return field + other_field
                raise ValueError("Cannot combine tool results")
            return field or other_field

        return ToolResult(
            output=combine_fields(self.output, other.output),
            error=combine_fields(self.error, other.error),
            base64_image=combine_fields(self.base64_image, other.base64_image, False),
            system=combine_fields(self.system, other.system),
        )

    def __str__(self):
        return f"Error: {self.error}" if self.error else self.output

    def replace(self, **kwargs):
        """返回一个替换了给定字段的新 ToolResult。"""
        # return self.copy(update=kwargs)
        return type(self)(**{**self.dict(), **kwargs})


class BaseTool(ABC, BaseModel):
    """所有工具的整合基类，结合了 BaseModel 和 Tool 功能。

    提供：
    - Pydantic 模型验证
    - 模式注册
    - 标准化结果处理
    - 抽象执行接口

    属性:
        name (str): 工具名称
        description (str): 工具描述
        parameters (dict): 工具参数模式
        _schemas (Dict[str, List[ToolSchema]]): 已注册的方法模式
    """

    name: str
    description: str
    parameters: Optional[dict] = None
    # _schemas: Dict[str, List[ToolSchema]] = {}

    class Config:
        arbitrary_types_allowed = True
        underscore_attrs_are_private = False

    # def __init__(self, **data):
    #     """Initialize tool with model validation and schema registration."""
    #     super().__init__(**data)
    #     logger.debug(f"Initializing tool class: {self.__class__.__name__}")
    #     self._register_schemas()

    # def _register_schemas(self):
    #     """Register schemas from all decorated methods."""
    #     for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
    #         if hasattr(method, 'tool_schemas'):
    #             self._schemas[name] = method.tool_schemas
    #             logger.debug(f"Registered schemas for method '{name}' in {self.__class__.__name__}")

    async def __call__(self, **kwargs) -> Any:
        """使用给定参数执行工具。"""
        return await self.execute(**kwargs)

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """使用给定参数执行工具。"""

    def to_param(self) -> Dict:
        """将工具转换为函数调用格式。

        Returns:
            包含 OpenAI 函数调用格式的工具元数据的字典
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    # def get_schemas(self) -> Dict[str, List[ToolSchema]]:
    #     """Get all registered tool schemas.

    #     Returns:
    #         Dict mapping method names to their schema definitions
    #     """
    #     return self._schemas

    def success_response(self, data: Union[Dict[str, Any], str]) -> ToolResult:
        """创建成功的工具结果。

        Args:
            data: 结果数据（字典或字符串）

        Returns:
            带有 success=True 和格式化输出的 ToolResult
        """
        if isinstance(data, str):
            text = data
        else:
            text = json.dumps(data, indent=2)
        logger.debug(f"Created success response for {self.__class__.__name__}")
        return ToolResult(output=text)

    def fail_response(self, msg: str) -> ToolResult:
        """创建失败的工具结果。

        Args:
            msg: 描述失败的错误消息

        Returns:
            带有 success=False 和错误消息的 ToolResult
        """
        logger.debug(f"Tool {self.__class__.__name__} returned failed result: {msg}")
        return ToolResult(error=msg)


class CLIResult(ToolResult):
    """可以渲染为 CLI 输出的 ToolResult。"""


class ToolFailure(ToolResult):
    """表示失败的 ToolResult。"""
