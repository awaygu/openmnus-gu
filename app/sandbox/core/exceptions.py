"""沙箱系统的异常类。

此模块定义了在整个沙箱系统中使用的自定义异常，
以结构化的方式处理各种错误条件。
"""


class SandboxError(Exception):
    """沙箱相关错误的基类异常。"""


class SandboxTimeoutError(SandboxError):
    """当沙箱操作超时时引发的异常。"""


class SandboxResourceError(SandboxError):
    """资源相关错误引发的异常。"""
