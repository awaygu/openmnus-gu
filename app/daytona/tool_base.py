from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, Optional

from daytona_sdk import Daytona, DaytonaConfig, Sandbox, SandboxState
from pydantic import Field

from app.config import config
from app.daytona.sandbox import create_sandbox, start_supervisord_session, daytona
from app.tool.base import BaseTool
from app.utils.files_utils import clean_path
from app.utils.logger import logger


@dataclass
class ThreadMessage:
    """
    表示要添加到线程的消息。
    """

    type: str
    content: Dict[str, Any]
    is_llm_message: bool = False
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[float] = field(
        default_factory=lambda: datetime.now().timestamp()
    )

    def to_dict(self) -> Dict[str, Any]:
        """将消息转换为字典以供 API 调用"""
        return {
            "type": self.type,
            "content": self.content,
            "is_llm_message": self.is_llm_message,
            "metadata": self.metadata or {},
            "timestamp": self.timestamp,
        }


class SandboxToolsBase(BaseTool):
    """所有沙箱工具的基类，提供基于项目的沙箱访问。"""

    # 类变量，用于跟踪是否已打印沙箱 URL
    _urls_printed: ClassVar[bool] = False

    # 必需字段
    project_id: Optional[str] = None
    # thread_manager: Optional[ThreadManager] = None

    # 私有字段（不属于模型模式）
    _sandbox: Optional[Sandbox] = None
    _sandbox_id: Optional[str] = None
    _sandbox_pass: Optional[str] = None
    workspace_path: str = Field(default="/workspace", exclude=True)
    _sessions: dict[str, str] = {}

    class Config:
        arbitrary_types_allowed = True  # 允许非 pydantic 类型，如 ThreadManager
        underscore_attrs_are_private = True

    async def _ensure_sandbox(self) -> Sandbox:
        """确保我们有一个有效的沙箱实例，如果需要，从项目中检索它。"""
        if self._sandbox is None:
            # 获取或启动沙箱
            if config.daytona is None:
                raise RuntimeError("Daytona configuration not found. Please configure daytona_api_key in config.toml")
            try:
                self._sandbox = create_sandbox(password=config.daytona.VNC_password)
                # 如果尚未打印，则记录 URL
                if not SandboxToolsBase._urls_printed:
                    vnc_link = self._sandbox.get_preview_link(6080)
                    website_link = self._sandbox.get_preview_link(8080)

                    vnc_url = (
                        vnc_link.url if hasattr(vnc_link, "url") else str(vnc_link)
                    )
                    website_url = (
                        website_link.url
                        if hasattr(website_link, "url")
                        else str(website_link)
                    )

                    print("\033[95m***")
                    print(f"VNC URL: {vnc_url}")
                    print(f"Website URL: {website_url}")
                    print("***\033[0m")
                    SandboxToolsBase._urls_printed = True
            except Exception as e:
                logger.error(f"Error retrieving or starting sandbox: {str(e)}")
                raise e
        else:
            if (
                self._sandbox.state == SandboxState.ARCHIVED
                or self._sandbox.state == SandboxState.STOPPED
            ):
                logger.info(f"Sandbox is in {self._sandbox.state} state. Starting...")
                try:
                    if daytona is None:
                        raise RuntimeError("Daytona client is not initialized")
                    daytona.start(self._sandbox)
                    # 等待沙箱初始化
                    # sleep(5)
                    # 启动后刷新沙箱状态

                    # 重启时在会话中启动 supervisord
                    start_supervisord_session(self._sandbox)
                except Exception as e:
                    logger.error(f"Error starting sandbox: {e}")
                    raise e
        return self._sandbox

    @property
    def sandbox(self) -> Sandbox:
        """获取沙箱实例，确保它存在。"""
        if self._sandbox is None:
            raise RuntimeError("Sandbox not initialized. Call _ensure_sandbox() first.")
        return self._sandbox

    @property
    def sandbox_id(self) -> str:
        """获取沙箱 ID，确保它存在。"""
        if self._sandbox_id is None:
            raise RuntimeError(
                "Sandbox ID not initialized. Call _ensure_sandbox() first."
            )
        return self._sandbox_id

    def clean_path(self, path: str) -> str:
        """清理并规范化路径，使其相对于 /workspace。"""
        cleaned_path = clean_path(path, self.workspace_path)
        logger.debug(f"Cleaned path: {path} -> {cleaned_path}")
        return cleaned_path
