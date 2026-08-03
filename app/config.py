import json
import os
import threading
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = get_project_root()
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"


class LLMSettings(BaseModel):
    model: str = Field(..., description="模型名称")
    base_url: str = Field(..., description="API 基础 URL")
    api_key: str = Field(..., description="API 密钥")
    max_tokens: int = Field(4096, description="每次请求的最大 token 数")
    max_input_tokens: Optional[int] = Field(
        None,
        description="所有请求中使用的最大输入 token 数（None 表示无限制）",
    )
    temperature: float = Field(1.0, description="采样温度")
    api_type: str = Field(..., description="API 类型：Azure、Openai 或 Ollama")
    api_version: str = Field(..., description="如果使用 AzureOpenai，则为 Azure Openai 版本")


class ProxySettings(BaseModel):
    server: str = Field(None, description="代理服务器地址")
    username: Optional[str] = Field(None, description="代理用户名")
    password: Optional[str] = Field(None, description="代理密码")


class SearchSettings(BaseModel):
    engine: str = Field(default="Google", description="LLM 使用的搜索引擎")
    fallback_engines: List[str] = Field(
        default_factory=lambda: ["DuckDuckGo", "Baidu", "Bing"],
        description="主搜索引擎失败时尝试的回退搜索引擎",
    )
    retry_delay: int = Field(
        default=60,
        description="所有搜索引擎都失败后，重新尝试所有引擎前等待的秒数",
    )
    max_retries: int = Field(
        default=3,
        description="所有搜索引擎都失败时的最大重试次数",
    )
    lang: str = Field(
        default="en",
        description="搜索结果的语言代码（例如：en, zh, fr）",
    )
    country: str = Field(
        default="us",
        description="搜索结果的国家代码（例如：us, cn, uk）",
    )


class RunflowSettings(BaseModel):
    use_data_analysis_agent: bool = Field(
        default=False, description="在运行流程中启用数据分析 agent"
    )


class BrowserSettings(BaseModel):
    headless: bool = Field(False, description="是否以无头模式运行浏览器")
    disable_security: bool = Field(
        True, description="禁用浏览器安全功能"
    )
    extra_chromium_args: List[str] = Field(
        default_factory=list, description="传递给浏览器的额外参数"
    )
    chrome_instance_path: Optional[str] = Field(
        None, description="要使用的 Chrome 实例路径"
    )
    wss_url: Optional[str] = Field(
        None, description="通过 WebSocket 连接到浏览器实例"
    )
    cdp_url: Optional[str] = Field(
        None, description="通过 CDP 连接到浏览器实例"
    )
    proxy: Optional[ProxySettings] = Field(
        None, description="浏览器的代理设置"
    )
    max_content_length: int = Field(
        2000, description="内容检索操作的最大长度"
    )


class SandboxSettings(BaseModel):
    """执行沙箱的配置"""

    use_sandbox: bool = Field(False, description="是否使用沙箱")
    image: str = Field("python:3.12-slim", description="基础镜像")
    work_dir: str = Field("/workspace", description="容器工作目录")
    memory_limit: str = Field("512m", description="内存限制")
    cpu_limit: float = Field(1.0, description="CPU 限制")
    timeout: int = Field(300, description="默认命令超时时间（秒）")
    network_enabled: bool = Field(
        False, description="是否允许网络访问"
    )


class DaytonaSettings(BaseModel):
    daytona_api_key: Optional[str] = Field(None, description="Daytona API 密钥")
    daytona_server_url: Optional[str] = Field(
        "https://app.daytona.io/api", description="Daytona 服务器 URL"
    )
    daytona_target: Optional[str] = Field("us", description="区域选择：'eu' 或 'us'")
    sandbox_image_name: Optional[str] = Field("whitezxj/sandbox:0.1.0", description="沙箱镜像名称")
    sandbox_entrypoint: Optional[str] = Field(
        "/usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf",
        description="沙箱入口点",
    )
    # sandbox_id: Optional[str] = Field(
    #     None, description="要使用的 daytona 沙箱 ID（如果有）"
    # )
    VNC_password: Optional[str] = Field(
        "123456", description="沙箱中 VNC 服务的密码"
    )


class MCPServerConfig(BaseModel):
    """单个 MCP 服务器的配置"""

    type: str = Field(..., description="服务器连接类型（sse 或 stdio）")
    url: Optional[str] = Field(None, description="SSE 连接的服务器 URL")
    command: Optional[str] = Field(None, description="stdio 连接的命令")
    args: List[str] = Field(
        default_factory=list, description="stdio 命令的参数"
    )


class MCPSettings(BaseModel):
    """MCP（Model Context Protocol）的配置"""

    server_reference: str = Field(
        "app.mcp.server", description="MCP 服务器的模块引用"
    )
    servers: Dict[str, MCPServerConfig] = Field(
        default_factory=dict, description="MCP 服务器配置"
    )

    @classmethod
    def load_server_config(cls) -> Dict[str, MCPServerConfig]:
        """从 JSON 文件加载 MCP 服务器配置"""
        config_path = PROJECT_ROOT / "config" / "mcp.json"

        try:
            config_file = config_path if config_path.exists() else None
            if not config_file:
                return {}

            with config_file.open() as f:
                data = json.load(f)
                servers = {}

                for server_id, server_config in data.get("mcpServers", {}).items():
                    servers[server_id] = MCPServerConfig(
                        type=server_config["type"],
                        url=server_config.get("url"),
                        command=server_config.get("command"),
                        args=server_config.get("args", []),
                    )
                return servers
        except Exception as e:
            raise ValueError(f"Failed to load MCP server config: {e}")


class AppConfig(BaseModel):
    llm: Dict[str, LLMSettings]
    sandbox: Optional[SandboxSettings] = Field(
        None, description="Sandbox configuration"
    )
    browser_config: Optional[BrowserSettings] = Field(
        None, description="Browser configuration"
    )
    search_config: Optional[SearchSettings] = Field(
        None, description="Search configuration"
    )
    mcp_config: Optional[MCPSettings] = Field(None, description="MCP configuration")
    run_flow_config: Optional[RunflowSettings] = Field(
        None, description="Run flow configuration"
    )
    daytona_config: Optional[DaytonaSettings] = Field(
        None, description="Daytona configuration"
    )

    class Config:
        arbitrary_types_allowed = True


class Config:
    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self._config = None
                    self._load_initial_config()
                    self._initialized = True

    @staticmethod
    def _get_config_path() -> Path:
        root = PROJECT_ROOT
        config_path = root / "config" / "config.toml"
        if config_path.exists():
            return config_path
        example_path = root / "config" / "config.example.toml"
        if example_path.exists():
            return example_path
        raise FileNotFoundError("No configuration file found in config directory")

    def _load_config(self) -> dict:
        config_path = self._get_config_path()
        with config_path.open("rb") as f:
            return tomllib.load(f)

    def _load_initial_config(self):
        raw_config = self._load_config()
        base_llm = raw_config.get("llm", {})
        llm_overrides = {
            k: v for k, v in raw_config.get("llm", {}).items() if isinstance(v, dict)
        }

        # 优先使用配置文件中的 api_key，如果没有则从环境变量 DASHSCOPE_API_KEY 读取
        api_key = base_llm.get("api_key") or os.getenv("DASHSCOPE_API_KEY")

        default_settings = {
            "model": base_llm.get("model"),
            "base_url": base_llm.get("base_url"),
            "api_key": api_key,
            "max_tokens": base_llm.get("max_tokens", 4096),
            "max_input_tokens": base_llm.get("max_input_tokens"),
            "temperature": base_llm.get("temperature", 1.0),
            "api_type": base_llm.get("api_type", ""),
            "api_version": base_llm.get("api_version", ""),
        }

        # 处理浏览器配置
        browser_config = raw_config.get("browser", {})
        browser_settings = None

        if browser_config:
            # 处理代理设置
            proxy_config = browser_config.get("proxy", {})
            proxy_settings = None

            if proxy_config and proxy_config.get("server"):
                proxy_settings = ProxySettings(
                    **{
                        k: v
                        for k, v in proxy_config.items()
                        if k in ["server", "username", "password"] and v
                    }
                )

            # 过滤有效的浏览器配置参数
            valid_browser_params = {
                k: v
                for k, v in browser_config.items()
                if k in BrowserSettings.__annotations__ and v is not None
            }

            # 如果有代理设置，将其添加到参数中
            if proxy_settings:
                valid_browser_params["proxy"] = proxy_settings

            # 仅在存在有效参数时创建 BrowserSettings
            if valid_browser_params:
                browser_settings = BrowserSettings(**valid_browser_params)

        search_config = raw_config.get("search", {})
        search_settings = None
        if search_config:
            search_settings = SearchSettings(**search_config)
        sandbox_config = raw_config.get("sandbox", {})
        if sandbox_config:
            sandbox_settings = SandboxSettings(**sandbox_config)
        else:
            sandbox_settings = SandboxSettings()
        daytona_config = raw_config.get("daytona", {})
        daytona_settings = None
        if daytona_config:
            daytona_settings = DaytonaSettings(**daytona_config)

        mcp_config = raw_config.get("mcp", {})
        mcp_settings = None
        if mcp_config:
            # 从 JSON 文件加载服务器配置
            mcp_config["servers"] = MCPSettings.load_server_config()
            mcp_settings = MCPSettings(**mcp_config)
        else:
            mcp_settings = MCPSettings(servers=MCPSettings.load_server_config())

        run_flow_config = raw_config.get("runflow")
        if run_flow_config:
            run_flow_settings = RunflowSettings(**run_flow_config)
        else:
            run_flow_settings = RunflowSettings()

        # 处理 LLM 覆盖配置，也支持从环境变量读取 api_key
        llm_configs = {}
        for name, override_config in llm_overrides.items():
            # 如果覆盖配置中没有 api_key 或为空，尝试从环境变量读取
            if not override_config.get("api_key"):
                env_api_key = os.getenv("DASHSCOPE_API_KEY")
                if env_api_key:
                    override_config["api_key"] = env_api_key
            llm_configs[name] = {**default_settings, **override_config}

        config_dict = {
            "llm": {
                "default": default_settings,
                **llm_configs,
            },
            "sandbox": sandbox_settings,
            "browser_config": browser_settings,
            "search_config": search_settings,
            "mcp_config": mcp_settings,
            "run_flow_config": run_flow_settings,
            "daytona_config": daytona_settings,
        }

        self._config = AppConfig(**config_dict)

    @property
    def llm(self) -> Dict[str, LLMSettings]:
        return self._config.llm

    @property
    def sandbox(self) -> SandboxSettings:
        return self._config.sandbox

    @property
    def daytona(self) -> Optional[DaytonaSettings]:
        return self._config.daytona_config

    @property
    def browser_config(self) -> Optional[BrowserSettings]:
        return self._config.browser_config

    @property
    def search_config(self) -> Optional[SearchSettings]:
        return self._config.search_config

    @property
    def mcp_config(self) -> MCPSettings:
        """获取 MCP 配置"""
        return self._config.mcp_config

    @property
    def run_flow_config(self) -> RunflowSettings:
        """获取运行流程配置"""
        return self._config.run_flow_config

    @property
    def workspace_root(self) -> Path:
        """获取工作区根目录"""
        return WORKSPACE_ROOT

    @property
    def root_path(self) -> Path:
        """获取应用程序的根路径"""
        return PROJECT_ROOT


config = Config()
