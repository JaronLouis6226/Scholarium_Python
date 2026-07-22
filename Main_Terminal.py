"""
Scholarium Terminal —— 高校教师主页信息采集工具（命令行版）
"""


# 教师主页 URL 列表（每行一个）
URLS = """

"""

# =======================
# 运行参数
# =======================

# HTTP 请求超时（秒）
REQUEST_TIMEOUT = 5

# 最大重试次数
MAX_RETRIES = 3

# 请求间隔随机范围（秒）
RANDOM_DELAY_MIN = 0.1
RANDOM_DELAY_MAX = 0.5

# 输出文件名
OUTPUT_FILENAME = "teachers.xlsx"

# 有效内容最小长度（字符）
MIN_CONTENT_LENGTH = 30

# 简介文本最大长度（写入 Excel 前截断）
MAX_CONTENT_LENGTH = 1500

# =======================
# 程序入口
# =======================

from typing import List, Tuple

from src import crawler
from src import extractor
from src import writer
from src.utils import logger, is_file_locked


def _parse_urls(text: str) -> List[str]:
    """解析多行 URL 文本"""
    parsed: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            parsed.append(line)
    return parsed


def _process_single_url(url: str) -> Tuple[str, str, str]:
    """处理单个教师 URL：抓取 → 提取 → 返回 (简介, 邮箱, 主页是否有内容)"""

    # Step 1: 抓取页面
    fetch_result = crawler.fetch_url(url)

    if fetch_result.error:
        if "404" in fetch_result.error:
            logger.info("状态：网页404")
        elif "超时" in fetch_result.error or "Timeout" in fetch_result.error:
            logger.info("状态：网页连接超时")
        else:
            logger.info("状态：链接无效")
        return ("", "网页无邮箱", "链接无效")

    if not fetch_result.success or not fetch_result.html:
        logger.info("状态：链接无效")
        return ("", "网页无邮箱", "链接无效")

    # Step 2: 提取教师简介
    logger.info("2. 提取教师简介中……")

    profile_text = extractor.extract_teacher_profile(fetch_result.html, url)

    if profile_text:
        logger.info("提取成功")
    else:
        logger.info("提取失败")
        logger.info("页面不存在教师简介信息")

    # Step 3: 提取邮箱
    logger.info("3. 提取教师邮箱中……")
    logger.info("尝试从页面提取……")

    email = extractor.extract_email(fetch_result.html, url)
    if not email:
        email = "网页无邮箱"

    # 清理 PDF 缓存
    extractor.clear_pdf_cache()

    if not profile_text:
        logger.info("状态：主页无内容")
        return ("", email, "主页无内容")

    # 检测是否为边角内容（导航、公告等非教师简介内容）
    if extractor.is_corner_content(profile_text):
        logger.info("检测到内容几乎全为导航/公告等非教师简介，不填入表格")
        return ("", email, "主页无内容")

    # 截断过长文本
    if len(profile_text) > MAX_CONTENT_LENGTH:
        profile_text = profile_text[:MAX_CONTENT_LENGTH]

    logger.info("提取完成")

    return (profile_text, email, "")


def _print_statistics(results: List[writer.ResultRow]) -> None:
    """输出采集统计信息。"""
    total = len(results)
    has_content = sum(1 for _, _, c in results if c == "有内容")
    no_content = sum(1 for _, _, c in results if c == "主页无内容")
    dead_link = sum(1 for _, _, c in results if c == "链接无效")

    logger.info("")
    logger.info("［采集完成］")
  

def main() -> None:
    """程序主入口"""
    crawler.REQUEST_TIMEOUT = REQUEST_TIMEOUT
    crawler.MAX_RETRIES = MAX_RETRIES
    crawler.RANDOM_DELAY_MIN = RANDOM_DELAY_MIN
    crawler.RANDOM_DELAY_MAX = RANDOM_DELAY_MAX
    extractor.MIN_CONTENT_LENGTH = MIN_CONTENT_LENGTH

    # 检查输出文件是否被占用
    if is_file_locked(OUTPUT_FILENAME):
        logger.info(f"输出文件 '{OUTPUT_FILENAME}' 已被占用，请关闭后再运行。")
        return

    # 解析 URL
    url_list = _parse_urls(URLS)

    if not url_list:
        logger.info("未发现有效 URL，请检查 Main.py 中的 URLS 配置。")
        return

    total = len(url_list)
    logger.info("［开始采集］")
    logger.info(f"共 {total} 个URL")
    logger.info("")

    results: List[writer.ResultRow] = []

    for idx, url in enumerate(url_list, 1):
        logger.info(f"［{idx}/{total}］")
        logger.info(f"URL：{url}")
        logger.info("")

        content, email, has_content = _process_single_url(url)
        results.append((content, email, has_content))

        logger.info("")

        if idx < total:
            crawler.random_delay()

    # 写入 Excel
    try:
        writer.write_excel(results, OUTPUT_FILENAME)
    except Exception as e:
        logger.info(f"Excel 写入失败: {e}")
        return

    # 输出统计
    _print_statistics(results)


if __name__ == "__main__":
    main()
