import logging
import os

import structlog

# 从环境变量获取模式，默认为本地模式
ENV_MODE = os.getenv("ENV_MODE", "LOCAL")

# 根据环境模式选择渲染器：本地模式使用控制台渲染器，其他模式使用 JSON 渲染器
renderer = [structlog.processors.JSONRenderer()]
if ENV_MODE.lower() == "local".lower():
    renderer = [structlog.dev.ConsoleRenderer()]

# 配置 structlog
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,  # 添加日志级别
        structlog.stdlib.PositionalArgumentsFormatter(),  # 格式化位置参数
        structlog.processors.dict_tracebacks,  # 字典化堆栈跟踪
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FILENAME,  # 文件名
                structlog.processors.CallsiteParameter.FUNC_NAME,  # 函数名
                structlog.processors.CallsiteParameter.LINENO,  # 行号
            }
        ),
        structlog.processors.TimeStamper(fmt="iso"),  # ISO 格式时间戳
        structlog.contextvars.merge_contextvars,  # 合并上下文变量
        *renderer,  # 渲染器
    ],
    cache_logger_on_first_use=True,  # 首次使用时缓存 logger
)

# 创建全局 logger 实例
logger: structlog.stdlib.BoundLogger = structlog.get_logger(level=logging.DEBUG)
