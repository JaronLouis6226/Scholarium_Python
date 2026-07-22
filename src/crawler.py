"""HTTP 请求模块：带反爬策略的网页抓取"""

import random
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .utils import logger

# ============================================================================
# HTTP 配置（从 Main.py 导入，此处提供默认值）
# ============================================================================

REQUEST_TIMEOUT: int = 5
MAX_RETRIES: int = 3
RANDOM_DELAY_MIN: float = 0.5
RANDOM_DELAY_MAX: float = 1.5

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
]

HEADERS_TEMPLATE: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ========================================
# 内部实现
# ========================================

def _build_session() -> requests.Session:
    """构建带重试策略和连接池的 Session
    禁用系统代理以避免本地代理（如 Clash/V2Ray）返回 502 等错误
    """
    session = requests.Session()
    session.trust_env = False  # 禁用系统代理，避免 127.0.0.1:7890 等代理干扰
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_headers(url: str) -> dict[str, str]:
    """构建带随机 User-Agent 的请求头"""
    headers = HEADERS_TEMPLATE.copy()
    headers["User-Agent"] = random.choice(USER_AGENTS)
    headers["Referer"] = url
    return headers


# 共享 Session（跨请求复用）
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """获取或创建共享 Session"""
    global _session
    if _session is None:
        _session = _build_session()
    return _session


class FetchResult:
    """抓取结果"""

    def __init__(
        self,
        html: str = "",
        status_code: int = 0,
        success: bool = False,
        error: str = "",
    ):
        self.html = html
        self.status_code = status_code
        self.success = success
        self.error = error


def random_delay() -> None:
    """请求间随机休眠"""
    import time
    delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
    time.sleep(delay)


def fetch_url(url: str) -> FetchResult:
    """抓取指定 URL，返回 FetchResult 对象，包含抓取结果信息"""
    result = FetchResult()

    try:
        logger.info("1. 请求网页中……")
        headers = _get_headers(url)
        session = _get_session()

        response = session.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            verify=True,
        )

        result.status_code = response.status_code

        if response.status_code == 404:
            result.error = "页面不存在（404）"
            logger.info("请求失败")
            logger.info(f"失败原因：{result.error}")
            return result

        if response.status_code != 200:
            result.error = f"HTTP {response.status_code}"
            logger.info("请求失败")
            logger.info(f"失败原因：{result.error}")
            return result

        response.encoding = response.apparent_encoding or "utf-8"
        result.html = response.text
        result.success = True
        logger.info("请求成功")

    except requests.exceptions.Timeout:
        result.error = "连接超时"
        logger.info("请求失败")
        logger.info(f"失败原因：{result.error}")

    except requests.exceptions.SSLError as e:
        result.error = f"SSL证书异常"
        logger.info("请求失败")
        logger.info(f"失败原因：{result.error}")
        logger.debug(f"SSL详情: {e}")

    except requests.exceptions.ConnectionError as e:
        result.error = "网络连接失败"
        logger.info("请求失败")
        logger.info(f"失败原因：{result.error}")
        logger.debug(f"连接详情: {e}")

    except requests.exceptions.RequestException as e:
        result.error = "请求异常"
        logger.info("请求失败")
        logger.info(f"失败原因：{result.error}")
        logger.debug(f"异常详情: {e}")

    except Exception as e:
        result.error = "未知错误"
        logger.info("请求失败")
        logger.info(f"失败原因：{result.error}")
        logger.debug(f"错误详情: {e}")

    return result
