from abc import ABC, abstractmethod
from typing import Dict, Optional, Protocol

from app.config import SandboxSettings
from app.sandbox.core.sandbox import DockerSandbox


class SandboxFileOperations(Protocol):
    """沙箱文件操作的协议。"""

    async def copy_from(self, container_path: str, local_path: str) -> None:
        """从容器复制文件到本地。

        Args:
            container_path: 容器中的文件路径。
            local_path: 本地目标路径。
        """
        ...

    async def copy_to(self, local_path: str, container_path: str) -> None:
        """从本地复制文件到容器。

        Args:
            local_path: 本地源文件路径。
            container_path: 容器中的目标路径。
        """
        ...

    async def read_file(self, path: str) -> str:
        """从容器读取文件内容。

        Args:
            path: 容器中的文件路径。

        Returns:
            str: 文件内容。
        """
        ...

    async def write_file(self, path: str, content: str) -> None:
        """将内容写入容器中的文件。

        Args:
            path: 容器中的文件路径。
            content: 要写入的内容。
        """
        ...


class BaseSandboxClient(ABC):
    """沙箱客户端的基础接口。"""

    @abstractmethod
    async def create(
        self,
        config: Optional[SandboxSettings] = None,
        volume_bindings: Optional[Dict[str, str]] = None,
    ) -> None:
        """创建沙箱。"""

    @abstractmethod
    async def run_command(self, command: str, timeout: Optional[int] = None) -> str:
        """执行命令。"""

    @abstractmethod
    async def copy_from(self, container_path: str, local_path: str) -> None:
        """从容器复制文件。"""

    @abstractmethod
    async def copy_to(self, local_path: str, container_path: str) -> None:
        """复制文件到容器。"""

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """读取文件。"""

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None:
        """写入文件。"""

    @abstractmethod
    async def cleanup(self) -> None:
        """清理资源。"""


class LocalSandboxClient(BaseSandboxClient):
    """本地沙箱客户端实现。"""

    def __init__(self):
        """初始化本地沙箱客户端。"""
        self.sandbox: Optional[DockerSandbox] = None

    async def create(
        self,
        config: Optional[SandboxSettings] = None,
        volume_bindings: Optional[Dict[str, str]] = None,
    ) -> None:
        """创建沙箱。

        Args:
            config: 沙箱配置。
            volume_bindings: 卷映射。

        Raises:
            RuntimeError: 如果沙箱创建失败。
        """
        self.sandbox = DockerSandbox(config, volume_bindings)
        await self.sandbox.create()

    async def run_command(self, command: str, timeout: Optional[int] = None) -> str:
        """在沙箱中运行命令。

        Args:
            command: 要执行的命令。
            timeout: 执行超时时间（秒）。

        Returns:
            命令输出。

        Raises:
            RuntimeError: 如果沙箱未初始化。
        """
        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")
        return await self.sandbox.run_command(command, timeout)

    async def copy_from(self, container_path: str, local_path: str) -> None:
        """从容器复制文件到本地。

        Args:
            container_path: 容器中的文件路径。
            local_path: 本地目标路径。

        Raises:
            RuntimeError: 如果沙箱未初始化。
        """
        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")
        await self.sandbox.copy_from(container_path, local_path)

    async def copy_to(self, local_path: str, container_path: str) -> None:
        """从本地复制文件到容器。

        Args:
            local_path: 本地源文件路径。
            container_path: 容器中的目标路径。

        Raises:
            RuntimeError: 如果沙箱未初始化。
        """
        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")
        await self.sandbox.copy_to(local_path, container_path)

    async def read_file(self, path: str) -> str:
        """从容器读取文件。

        Args:
            path: 容器中的文件路径。

        Returns:
            文件内容。

        Raises:
            RuntimeError: 如果沙箱未初始化。
        """
        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")
        return await self.sandbox.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        """写入文件到容器。

        Args:
            path: 容器中的文件路径。
            content: 文件内容。

        Raises:
            RuntimeError: 如果沙箱未初始化。
        """
        if not self.sandbox:
            raise RuntimeError("Sandbox not initialized")
        await self.sandbox.write_file(path, content)

    async def cleanup(self) -> None:
        """清理资源。"""
        if self.sandbox:
            await self.sandbox.cleanup()
            self.sandbox = None


def create_sandbox_client() -> LocalSandboxClient:
    """创建沙箱客户端。

    Returns:
        LocalSandboxClient: 沙箱客户端实例。
    """
    return LocalSandboxClient()


SANDBOX_CLIENT = create_sandbox_client()
