"""
异步 Docker 终端

此模块为 Docker 容器提供异步终端功能，
允许交互式命令执行和超时控制。
"""

import asyncio
import re
import socket
from typing import Dict, Optional, Tuple, Union

import docker
from docker import APIClient
from docker.errors import APIError
from docker.models.containers import Container


class DockerSession:
    def __init__(self, container_id: str) -> None:
        """初始化 Docker 会话。

        Args:
            container_id: Docker 容器的 ID。
        """
        self.api = APIClient()
        self.container_id = container_id
        self.exec_id = None
        self.socket = None

    async def create(self, working_dir: str, env_vars: Dict[str, str]) -> None:
        """创建与容器的交互式会话。

        Args:
            working_dir: 容器内的工作目录。
            env_vars: 要设置的环境变量。

        Raises:
            RuntimeError: 如果 socket 连接失败。
        """
        startup_command = [
            "bash",
            "-c",
            f"cd {working_dir} && "
            "PROMPT_COMMAND='' "
            "PS1='$ ' "
            "exec bash --norc --noprofile",
        ]

        exec_data = self.api.exec_create(
            self.container_id,
            startup_command,
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
            privileged=True,
            user="root",
            environment={**env_vars, "TERM": "dumb", "PS1": "$ ", "PROMPT_COMMAND": ""},
        )
        self.exec_id = exec_data["Id"]

        socket_data = self.api.exec_start(
            self.exec_id, socket=True, tty=True, stream=True, demux=True
        )

        if hasattr(socket_data, "_sock"):
            self.socket = socket_data._sock
            self.socket.setblocking(False)
        else:
            raise RuntimeError("Failed to get socket connection")

        await self._read_until_prompt()

    async def close(self) -> None:
        """清理会话资源。

        1. 发送退出命令
        2. 关闭 socket 连接
        3. 检查并清理 exec 实例
        """
        try:
            if self.socket:
                # 发送退出命令以关闭 bash 会话
                try:
                    self.socket.sendall(b"exit\n")
                    # 允许命令执行时间
                    await asyncio.sleep(0.1)
                except:
                    pass  # 忽略发送错误，继续清理

                # 关闭 socket 连接
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except:
                    pass  # 某些平台可能不支持 shutdown

                self.socket.close()
                self.socket = None

            if self.exec_id:
                try:
                    # 检查 exec 实例状态
                    exec_inspect = self.api.exec_inspect(self.exec_id)
                    if exec_inspect.get("Running", False):
                        # 如果仍在运行，等待其完成
                        await asyncio.sleep(0.5)
                except:
                    pass  # 忽略检查错误，继续清理

                self.exec_id = None

        except Exception as e:
            # 记录错误但不抛出，确保清理继续
            print(f"Warning: Error during session cleanup: {e}")

    async def _read_until_prompt(self) -> str:
        """读取输出直到找到提示符。

        Returns:
            包含到提示符为止的输出字符串。

        Raises:
            socket.error: 如果 socket 通信失败。
        """
        buffer = b""
        while b"$ " not in buffer:
            try:
                chunk = self.socket.recv(4096)
                if chunk:
                    buffer += chunk
            except socket.error as e:
                if e.errno == socket.EWOULDBLOCK:
                    await asyncio.sleep(0.1)
                    continue
                raise
        return buffer.decode("utf-8")

    async def execute(self, command: str, timeout: Optional[int] = None) -> str:
        """执行命令并返回清理后的输出。

        Args:
            command: 要执行的 Shell 命令。
            timeout: 最大执行时间（秒）。

        Returns:
            命令输出字符串，已移除提示符标记。

        Raises:
            RuntimeError: 如果会话未初始化或执行失败。
            TimeoutError: 如果命令执行超过超时时间。
        """
        if not self.socket:
            raise RuntimeError("Session not initialized")

        try:
            # 清理命令以防止 shell 注入
            sanitized_command = self._sanitize_command(command)
            full_command = f"{sanitized_command}\necho $?\n"
            self.socket.sendall(full_command.encode())

            async def read_output() -> str:
                buffer = b""
                result_lines = []
                command_sent = False

                while True:
                    try:
                        chunk = self.socket.recv(4096)
                        if not chunk:
                            break

                        buffer += chunk
                        lines = buffer.split(b"\n")

                        buffer = lines[-1]
                        lines = lines[:-1]

                        for line in lines:
                            line = line.rstrip(b"\r")

                            if not command_sent:
                                command_sent = True
                                continue

                            if line.strip() == b"echo $?" or line.strip().isdigit():
                                continue

                            if line.strip():
                                result_lines.append(line)

                        if buffer.endswith(b"$ "):
                            break

                    except socket.error as e:
                        if e.errno == socket.EWOULDBLOCK:
                            await asyncio.sleep(0.1)
                            continue
                        raise

                output = b"\n".join(result_lines).decode("utf-8")
                output = re.sub(r"\n\$ echo \$\$?.*$", "", output)

                return output

            if timeout:
                result = await asyncio.wait_for(read_output(), timeout)
            else:
                result = await read_output()

            return result.strip()

        except asyncio.TimeoutError:
            raise TimeoutError(f"Command execution timed out after {timeout} seconds")
        except Exception as e:
            raise RuntimeError(f"Failed to execute command: {e}")

    def _sanitize_command(self, command: str) -> str:
        """清理命令字符串以防止 shell 注入。

        Args:
            command: 原始命令字符串。

        Returns:
            清理后的命令字符串。

        Raises:
            ValueError: 如果命令包含潜在的危险模式。
        """

        # 对特定危险命令的额外检查
        risky_commands = [
            "rm -rf /",
            "rm -rf /*",
            "mkfs",
            "dd if=/dev/zero",
            ":(){:|:&};:",
            "chmod -R 777 /",
            "chown -R",
        ]

        for risky in risky_commands:
            if risky in command.lower():
                raise ValueError(
                    f"Command contains potentially dangerous operation: {risky}"
                )

        return command


class AsyncDockerizedTerminal:
    def __init__(
        self,
        container: Union[str, Container],
        working_dir: str = "/workspace",
        env_vars: Optional[Dict[str, str]] = None,
        default_timeout: int = 60,
    ) -> None:
        """初始化 Docker 容器的异步终端。

        Args:
            container: Docker 容器 ID 或 Container 对象。
            working_dir: 容器内的工作目录。
            env_vars: 要设置的环境变量。
            default_timeout: 默认命令执行超时时间（秒）。
        """
        self.client = docker.from_env()
        self.container = (
            container
            if isinstance(container, Container)
            else self.client.containers.get(container)
        )
        self.working_dir = working_dir
        self.env_vars = env_vars or {}
        self.default_timeout = default_timeout
        self.session = None

    async def init(self) -> None:
        """初始化终端环境。

        确保工作目录存在并创建交互式会话。

        Raises:
            RuntimeError: 如果初始化失败。
        """
        await self._ensure_workdir()

        self.session = DockerSession(self.container.id)
        await self.session.create(self.working_dir, self.env_vars)

    async def _ensure_workdir(self) -> None:
        """确保容器中的工作目录存在。

        Raises:
            RuntimeError: 如果目录创建失败。
        """
        try:
            await self._exec_simple(f"mkdir -p {self.working_dir}")
        except APIError as e:
            raise RuntimeError(f"Failed to create working directory: {e}")

    async def _exec_simple(self, cmd: str) -> Tuple[int, str]:
        """使用 Docker 的 exec_run 执行简单命令。

        Args:
            cmd: 要执行的命令。

        Returns:
            (exit_code, output) 元组。
        """
        result = await asyncio.to_thread(
            self.container.exec_run, cmd, environment=self.env_vars
        )
        return result.exit_code, result.output.decode("utf-8")

    async def run_command(self, cmd: str, timeout: Optional[int] = None) -> str:
        """在容器中运行带超时的命令。

        Args:
            cmd: 要执行的 Shell 命令。
            timeout: 最大执行时间（秒）。

        Returns:
            命令输出字符串。

        Raises:
            RuntimeError: 如果终端未初始化。
        """
        if not self.session:
            raise RuntimeError("Terminal not initialized")

        return await self.session.execute(cmd, timeout=timeout or self.default_timeout)

    async def close(self) -> None:
        """关闭终端会话。"""
        if self.session:
            await self.session.close()

    async def __aenter__(self) -> "AsyncDockerizedTerminal":
        """异步上下文管理器入口。"""
        await self.init()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器退出。"""
        await self.close()
