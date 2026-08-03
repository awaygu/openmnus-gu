import time
from typing import Optional

from daytona_sdk import (
    CreateSandboxFromImageParams,
    Daytona,
    DaytonaConfig,
    Resources,
    Sandbox,
    SandboxState,
    SessionExecuteRequest,
)

from app.config import config
from app.utils.logger import logger

# 条件初始化 Daytona 客户端
daytona: Optional[Daytona] = None

daytona_settings = config.daytona
if daytona_settings and daytona_settings.daytona_api_key:
    logger.info("Initializing Daytona sandbox configuration")
    daytona_config = DaytonaConfig(
        api_key=daytona_settings.daytona_api_key,
        server_url=daytona_settings.daytona_server_url,
        target=daytona_settings.daytona_target,
    )

    logger.info("Daytona API key configured successfully")
    if daytona_config.server_url:
        logger.info(f"Daytona server URL set to: {daytona_config.server_url}")
    if daytona_config.target:
        logger.info(f"Daytona target set to: {daytona_config.target}")

    try:
        daytona = Daytona(daytona_config)
        logger.info("Daytona client initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize Daytona client: {e}")
        daytona = None
else:
    logger.info("Daytona configuration not found or API key not provided. Daytona features will be disabled.")


def _check_daytona_initialized():
    """检查 Daytona 客户端是否已初始化"""
    if daytona is None:
        raise RuntimeError("Daytona client is not initialized. Please configure daytona_api_key in config.toml")


async def get_or_start_sandbox(sandbox_id: str):
    """根据 ID 检索沙箱，检查其状态，如果需要则启动它。"""

    _check_daytona_initialized()
    logger.info(f"Getting or starting sandbox with ID: {sandbox_id}")

    try:
        sandbox = daytona.get(sandbox_id)

        # 检查沙箱是否需要启动
        if (
            sandbox.state == SandboxState.ARCHIVED
            or sandbox.state == SandboxState.STOPPED
        ):
            logger.info(f"Sandbox is in {sandbox.state} state. Starting...")
            try:
                daytona.start(sandbox)
                # 等待沙箱初始化
                # sleep(5)
                # 启动后刷新沙箱状态
                sandbox = daytona.get(sandbox_id)

                # 重启时在会话中启动 supervisord
                start_supervisord_session(sandbox)
            except Exception as e:
                logger.error(f"Error starting sandbox: {e}")
                raise e

        logger.info(f"Sandbox {sandbox_id} is ready")
        return sandbox

    except Exception as e:
        logger.error(f"Error retrieving or starting sandbox: {str(e)}")
        raise e


def start_supervisord_session(sandbox: Sandbox):
    """在会话中启动 supervisord。"""
    _check_daytona_initialized()
    session_id = "supervisord-session"
    try:
        logger.info(f"Creating session {session_id} for supervisord")
        sandbox.process.create_session(session_id)

        # 执行 supervisord 命令
        sandbox.process.execute_session_command(
            session_id,
            SessionExecuteRequest(
                command="exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf",
                var_async=True,
            ),
        )
        logger.info("Waiting for supervisord and browser automation service to start...")
        time.sleep(25)  # 等待 supervisord 启动

        # 等待浏览器自动化服务就绪（最多等待 60 秒）
        max_wait = 60
        wait_interval = 5
        waited = 0
        service_ready = False

        while waited < max_wait:
            try:
                # 检查服务是否可用
                check_cmd = "curl -s -f http://localhost:8003/health > /dev/null 2>&1 && echo 'READY' || echo 'NOT_READY'"
                response = sandbox.process.exec(check_cmd, timeout=10)
                if "READY" in response.result:
                    service_ready = True
                    logger.info(f"Browser automation service is ready after {waited} seconds")
                    break
            except Exception as e:
                logger.debug(f"Service check attempt {waited}s failed: {e}")

            time.sleep(wait_interval)
            waited += wait_interval
            logger.debug(f"Waiting for browser automation service... ({waited}/{max_wait}s)")

        if not service_ready:
            logger.warning(
                f"Browser automation service may not be ready after {max_wait} seconds. "
                "It may still be starting up. Subsequent browser operations may fail."
            )

        logger.info(f"Supervisord started in session {session_id}")
    except Exception as e:
        logger.error(f"Error starting supervisord session: {str(e)}")
        raise e


def create_sandbox(password: str, project_id: str = None):
    """创建配置了所有必需服务并正在运行的新沙箱。"""

    _check_daytona_initialized()
    if daytona_settings is None:
        raise RuntimeError("Daytona settings not configured")

    logger.info("Creating new Daytona sandbox environment")
    logger.info("Configuring sandbox with browser-use image and environment variables")

    labels = None
    if project_id:
        logger.info(f"Using sandbox_id as label: {project_id}")
        labels = {"id": project_id}

    params = CreateSandboxFromImageParams(
        image=daytona_settings.sandbox_image_name,
        public=True,
        labels=labels,
        env_vars={
            "CHROME_PERSISTENT_SESSION": "true",
            "RESOLUTION": "1024x768x24",
            "RESOLUTION_WIDTH": "1024",
            "RESOLUTION_HEIGHT": "768",
            "VNC_PASSWORD": password,
            "ANONYMIZED_TELEMETRY": "false",
            "CHROME_PATH": "",
            "CHROME_USER_DATA": "",
            "CHROME_DEBUGGING_PORT": "9222",
            "CHROME_DEBUGGING_HOST": "localhost",
            "CHROME_CDP": "",
        },
        resources=Resources(
            cpu=2,
            memory=4,
            disk=5,
        ),
        auto_stop_interval=15,
        auto_archive_interval=24 * 60,
    )

    # 创建沙箱
    sandbox = daytona.create(params)
    logger.info(f"Sandbox created with ID: {sandbox.id}")

    # 为新沙箱在会话中启动 supervisord
    start_supervisord_session(sandbox)

    logger.info(f"Sandbox environment successfully initialized")
    return sandbox


async def delete_sandbox(sandbox_id: str):
    """根据 ID 删除沙箱。"""
    _check_daytona_initialized()
    logger.info(f"Deleting sandbox with ID: {sandbox_id}")

    try:
        # 获取沙箱
        sandbox = daytona.get(sandbox_id)

        # 删除沙箱
        daytona.delete(sandbox)

        logger.info(f"Successfully deleted sandbox {sandbox_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting sandbox {sandbox_id}: {str(e)}")
        raise e
