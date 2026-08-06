class ToolError(Exception):
    """当工具遇到错误时引发。"""

    def __init__(self, message):
        self.message = message


class OpenManusError(Exception):
    """所有 OpenManus 错误的基础异常"""


class TokenLimitExceeded(OpenManusError):
    """当超过 token 限制时引发的异常"""
