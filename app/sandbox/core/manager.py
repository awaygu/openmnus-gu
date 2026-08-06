import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Optional, Set

import docker
from docker.errors import APIError, ImageNotFound

from app.config import SandboxSettings
from app.logger import logger
from app.sandbox.core.sandbox import DockerSandbox


class SandboxManager:
    """Docker 沙箱管理器。

    管理多个 DockerSandbox 实例的生命周期，包括创建、监控和清理。
    为沙箱资源提供并发访问控制和自动清理机制。

    Attributes:
        max_sandboxes: 允许的最大沙箱数量。
        idle_timeout: 沙箱空闲超时时间（秒）。
        cleanup_interval: 清理检查间隔（秒）。
        _sandboxes: 活动沙箱实例映射。
        _last_used: 沙箱最后使用时间记录。
    """

    def __init__(
        self,
        max_sandboxes: int = 100,
        idle_timeout: int = 3600,
        cleanup_interval: int = 300,
    ):
        """初始化沙箱管理器。

        Args:
            max_sandboxes: 最大沙箱数量限制。
            idle_timeout: 空闲超时时间（秒）。
            cleanup_interval: 清理检查间隔（秒）。
        """
        self.max_sandboxes = max_sandboxes
        self.idle_timeout = idle_timeout
        self.cleanup_interval = cleanup_interval

        # Docker 客户端
        self._client = docker.from_env()

        # 资源映射
        self._sandboxes: Dict[str, DockerSandbox] = {}
        self._last_used: Dict[str, float] = {}

        # 并发控制
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._active_operations: Set[str] = set()

        # 清理任务
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_shutting_down = False

        # 启动自动清理
        self.start_cleanup_task()

    async def ensure_image(self, image: str) -> bool:
        """确保 Docker 镜像可用。

        Args:
            image: 镜像名称。

        Returns:
            bool: 镜像是否可用。
        """
        try:
            self._client.images.get(image)
            return True
        except ImageNotFound:
            try:
                logger.info(f"Pulling image {image}...")
                await asyncio.get_event_loop().run_in_executor(
                    None, self._client.images.pull, image
                )
                return True
            except (APIError, Exception) as e:
                logger.error(f"Failed to pull image {image}: {e}")
                return False

    @asynccontextmanager
    async def sandbox_operation(self, sandbox_id: str):
        """沙箱操作的上下文管理器。

        提供并发控制和使用时间更新。

        Args:
            sandbox_id: 沙箱 ID。

        Raises:
            KeyError: 如果沙箱未找到。
        """
        if sandbox_id not in self._locks:
            self._locks[sandbox_id] = asyncio.Lock()

        async with self._locks[sandbox_id]:
            if sandbox_id not in self._sandboxes:
                raise KeyError(f"Sandbox {sandbox_id} not found")

            self._active_operations.add(sandbox_id)
            try:
                self._last_used[sandbox_id] = asyncio.get_event_loop().time()
                yield self._sandboxes[sandbox_id]
            finally:
                self._active_operations.remove(sandbox_id)

    async def create_sandbox(
        self,
        config: Optional[SandboxSettings] = None,
        volume_bindings: Optional[Dict[str, str]] = None,
    ) -> str:
        """创建新的沙箱实例。

        Args:
            config: 沙箱配置。
            volume_bindings: 卷映射配置。

        Returns:
            str: 沙箱 ID。

        Raises:
            RuntimeError: 如果达到最大沙箱数量或创建失败。
        """
        async with self._global_lock:
            if len(self._sandboxes) >= self.max_sandboxes:
                raise RuntimeError(
                    f"Maximum number of sandboxes ({self.max_sandboxes}) reached"
                )

            config = config or SandboxSettings()
            if not await self.ensure_image(config.image):
                raise RuntimeError(f"Failed to ensure Docker image: {config.image}")

            sandbox_id = str(uuid.uuid4())
            try:
                sandbox = DockerSandbox(config, volume_bindings)
                await sandbox.create()

                self._sandboxes[sandbox_id] = sandbox
                self._last_used[sandbox_id] = asyncio.get_event_loop().time()
                self._locks[sandbox_id] = asyncio.Lock()

                logger.info(f"Created sandbox {sandbox_id}")
                return sandbox_id

            except Exception as e:
                logger.error(f"Failed to create sandbox: {e}")
                if sandbox_id in self._sandboxes:
                    await self.delete_sandbox(sandbox_id)
                raise RuntimeError(f"Failed to create sandbox: {e}")

    async def get_sandbox(self, sandbox_id: str) -> DockerSandbox:
        """获取沙箱实例。

        Args:
            sandbox_id: 沙箱 ID。

        Returns:
            DockerSandbox: 沙箱实例。

        Raises:
            KeyError: 如果沙箱不存在。
        """
        async with self.sandbox_operation(sandbox_id) as sandbox:
            return sandbox

    def start_cleanup_task(self) -> None:
        """启动自动清理任务。"""

        async def cleanup_loop():
            while not self._is_shutting_down:
                try:
                    await self._cleanup_idle_sandboxes()
                except Exception as e:
                    logger.error(f"Error in cleanup loop: {e}")
                await asyncio.sleep(self.cleanup_interval)

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def _cleanup_idle_sandboxes(self) -> None:
        """清理空闲的沙箱。"""
        current_time = asyncio.get_event_loop().time()
        to_cleanup = []

        async with self._global_lock:
            for sandbox_id, last_used in self._last_used.items():
                if (
                    sandbox_id not in self._active_operations
                    and current_time - last_used > self.idle_timeout
                ):
                    to_cleanup.append(sandbox_id)

        for sandbox_id in to_cleanup:
            try:
                await self.delete_sandbox(sandbox_id)
            except Exception as e:
                logger.error(f"Error cleaning up sandbox {sandbox_id}: {e}")

    async def cleanup(self) -> None:
        """清理所有资源。"""
        logger.info("Starting manager cleanup...")
        self._is_shutting_down = True

        # 取消清理任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # 获取所有要清理的沙箱 ID
        async with self._global_lock:
            sandbox_ids = list(self._sandboxes.keys())

        # 并发清理所有沙箱
        cleanup_tasks = []
        for sandbox_id in sandbox_ids:
            task = asyncio.create_task(self._safe_delete_sandbox(sandbox_id))
            cleanup_tasks.append(task)

        if cleanup_tasks:
            # 等待所有清理任务完成，设置超时以避免无限等待
            try:
                await asyncio.wait(cleanup_tasks, timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("Sandbox cleanup timed out")

        # 清理剩余引用
        self._sandboxes.clear()
        self._last_used.clear()
        self._locks.clear()
        self._active_operations.clear()

        logger.info("Manager cleanup completed")

    async def _safe_delete_sandbox(self, sandbox_id: str) -> None:
        """安全删除单个沙箱。

        Args:
            sandbox_id: 要删除的沙箱 ID。
        """
        try:
            if sandbox_id in self._active_operations:
                logger.warning(
                    f"Sandbox {sandbox_id} has active operations, waiting for completion"
                )
                for _ in range(10):  # Wait at most 10 times
                    await asyncio.sleep(0.5)
                    if sandbox_id not in self._active_operations:
                        break
                else:
                    logger.warning(
                        f"Timeout waiting for sandbox {sandbox_id} operations to complete"
                    )

            # 获取沙箱对象的引用
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox:
                await sandbox.cleanup()

                # 从管理器中删除沙箱记录
                async with self._global_lock:
                    self._sandboxes.pop(sandbox_id, None)
                    self._last_used.pop(sandbox_id, None)
                    self._locks.pop(sandbox_id, None)
                    logger.info(f"Deleted sandbox {sandbox_id}")
        except Exception as e:
            logger.error(f"Error during cleanup of sandbox {sandbox_id}: {e}")

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Deletes specified sandbox.

        Args:
            sandbox_id: Sandbox ID.
        """
        if sandbox_id not in self._sandboxes:
            return

        try:
            await self._safe_delete_sandbox(sandbox_id)
        except Exception as e:
            logger.error(f"Failed to delete sandbox {sandbox_id}: {e}")

    async def __aenter__(self) -> "SandboxManager":
        """异步上下文管理器入口。"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器退出。"""
        await self.cleanup()

    def get_stats(self) -> Dict:
        """Gets manager statistics.

        Returns:
            Dict: Statistics information.
        """
        return {
            "total_sandboxes": len(self._sandboxes),
            "active_operations": len(self._active_operations),
            "max_sandboxes": self.max_sandboxes,
            "idle_timeout": self.idle_timeout,
            "cleanup_interval": self.cleanup_interval,
            "is_shutting_down": self._is_shutting_down,
        }
