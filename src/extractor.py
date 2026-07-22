"""内容提取模块：教师简介提取 + 邮箱提取 + HTML 清洗

采用混合策略：
1. 优先 trafilatura 提取
2. 回退自定义 DOM 密度算法 + BeautifulSoup 清洗
3. 自动解密 TSites 加密字段（HUST等加密的高校系统）
"""

import html as html_mod
import re
from io import BytesIO
from typing import Optional, Union
from urllib.parse import unquote, urljoin, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup, Comment, Tag

from .utils import logger

# =======================================
# TSites 解密（共享 Session）
# =======================================

_tsites_session: Optional[requests.Session] = None


def _get_tsites_session() -> requests.Session:
    """获取或创建 TSites 解密专用 Session"""
    global _tsites_session
    if _tsites_session is None:
        _tsites_session = requests.Session()
        _tsites_session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "",
        })
    return _tsites_session


def _decrypt_tsites_fields(html: str, page_url: str = "") -> str:
    """解密 HTML 中的 TSites 加密字段
    某些高校教师主页系统使用 _tsites_encrypt_field 机制，
    对邮箱、电话、地址等联系信息进行前端加密  （点名华中科技大学😡）
    加密内容存放在<span _tsites_encrypt_field=\"_tsites_encrypt_field\"> 中，
    需要 POST 到服务端解密接口获取明文
    """
    if not html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    encrypted_spans = soup.find_all(
        "span", attrs={"_tsites_encrypt_field": "_tsites_encrypt_field"}
    )

    if not encrypted_spans:
        return html  # 无加密字段，直接返回

    # 构造解密接口 URL
    base_url = ""
    if page_url:
        parsed = urlparse(page_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    decrypt_url = urljoin(base_url, "/system/resource/tsites/tsitesencrypt.jsp")
    session = _get_tsites_session()
    session.headers["Referer"] = page_url

    logger.info("TSites解密中……")

    decrypt_count = 0
    for span in encrypted_spans:
        span_id = str(span.get("id", ""))
        encrypted_content = span.text.strip()

        if not encrypted_content:
            continue

        try:
            resp = session.post(
                decrypt_url,
                data={
                    "id": span_id,
                    "content": encrypted_content,
                    "mode": "3",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                plain_text = data.get("content", "")
                if plain_text:
                    span.string = plain_text
                    del span["_tsites_encrypt_field"]
                    if span.get("style") == "display:none;":
                        del span["style"]
                    decrypt_count += 1
        except Exception as e:
            logger.debug(f"TSites 解密失败 [{span_id[:30]}...]: {e}")

    if decrypt_count == 0:
        logger.info("TSites解密失败")

    return str(soup)

# ==========================================
# 提取配置（从 Main.py 导入，此处提供默认值）
# ==========================================

MIN_CONTENT_LENGTH: int = 30

CONTENT_TAGS: list[str] = [
    "article", "main",
    "div[class*=content]", "div[class*=article]", "div[class*=main]",
    "div[class*=teacher]", "div[class*=profile]", "div[class*=info]",
    "div[class*=detail]", "div[class*=intro]", "div[class*=personal]",
    "div[id*=content]", "div[id*=article]", "div[id*=main]",
    "div[id*=teacher]", "div[id*=profile]", "div[id*=info]",
    "div[id*=detail]", "div[id*=intro]", "div[class*=page]",
    "section[class*=content]", "section[class*=article]",
    "section[class*=main]", "section[class*=teacher]",
    "section[class*=profile]", "section[class*=info]",
    "section[class*=intro]",
]

REMOVE_KEYWORDS: list[str] = [
    "导航", "菜单", "面包屑", "快速导航", "网站导航", "校内链接", "校外链接",
    "返回首页", "首页", "页脚", "版权", "版权所有", "Copyright", "ICP备",
    "公安备案", "联系学校", "联系我们", "联系学院", "办公电话", "招生咨询",
    "邮箱登录", "OA系统", "教务系统", "信息门户", "推荐阅读", "相关阅读",
    "最新新闻", "学院新闻", "学院简介", "学校简介", "通知公告", "通知", "公告",
    "新闻", "党建", "工会", "学生工作", "研究生工作", "本科生工作", "人才招聘",
    "友情链接", "快速链接", "网站地图", "上一篇", "下一篇", "在线留言",
    "设为首页", "加入收藏", "打印本页", "关闭窗口", "扫描二维码",
    "微信二维码", "微博", "公众号", "二维码", "浏览次数", "发布时间",
    "发布日期", "文章来源", "责任编辑", "编辑", "点击量", "阅读次数",
    "访问次数", "English", "english", "EN", "ENGLISH", "关于我们",
    "院长信箱", "书记信箱", "院长致辞", "师资队伍", "教师名录",
]


# ==========================================
# PDF 内容提取
# ==========================================

# PDF 文本缓存（避免同一 PDF 被重复下载解析）
_pdf_text_cache: dict[str, str] = {}


def clear_pdf_cache() -> None:
    """清空 PDF 文本缓存，释放内存"""
    _pdf_text_cache.clear()
    logger.debug("PDF 缓存已清空")


def _find_pdf_urls(html: str, page_url: str) -> list[str]:
    """从 HTML 中查找 PDF 文件链接"""
    soup = BeautifulSoup(html, "html.parser")
    pdf_urls: list[str] = []

    # 从 iframe src 查找
    for iframe in soup.find_all("iframe", src=True):
        src = str(iframe["src"])
        if ".pdf" in src.lower():
            pdf_urls.append(urljoin(page_url, src))

    # 从 embed src 查找
    for embed in soup.find_all("embed", src=True):
        src = str(embed["src"])
        if ".pdf" in src.lower():
            pdf_urls.append(urljoin(page_url, src))

    # 从 object data 查找
    for obj in soup.find_all("object", attrs={"data": True}):
        data = str(obj["data"])
        if ".pdf" in data.lower():
            pdf_urls.append(urljoin(page_url, data))

    # 从 <a> 链接查找
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if ".pdf" in href.lower():
            pdf_urls.append(urljoin(page_url, href))

    # 从 HTML 文本中直接搜索 PDF URL 模式
    pdf_pattern = re.compile(
        r'["\'(]([^"\'()\s]*\.pdf[^"\'()\s]*)["\')]',
        re.IGNORECASE,
    )
    for match in pdf_pattern.finditer(html):
        pdf_urls.append(urljoin(page_url, match.group(1)))

    # 去重并过滤无效 URL
    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in pdf_urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            unique_urls.append(u)

    return unique_urls


def _extract_text_from_pdf(pdf_url: str, page_url: str) -> str:
    """下载 PDF 并提取文本内容"""
    # 检查缓存
    if pdf_url in _pdf_text_cache:
        logger.debug("使用缓存的 PDF 文本")
        return _pdf_text_cache[pdf_url]

    try:
        import pdfplumber
    except ImportError:
        logger.debug("pdfplumber 未安装，跳过 PDF 提取")
        return ""

    try:
        # 首次下载时输出进度
        logger.info("检测到PDF文件")
        logger.info("正在下载PDF……")

        session = _get_tsites_session()
        session.headers["Referer"] = page_url
        resp = session.get(pdf_url, timeout=30)
        if resp.status_code != 200:
            logger.debug(f"PDF 下载失败: HTTP {resp.status_code}")
            _pdf_text_cache[pdf_url] = ""
            return ""

        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
            if not resp.content[:5] == b"%PDF-":
                logger.debug("响应不是 PDF 格式")
                _pdf_text_cache[pdf_url] = ""
                return ""

        logger.info("正在解析PDF……")

        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            full_text_parts: list[str] = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text_parts.append(text)

            full_text = "\n".join(full_text_parts)
            _pdf_text_cache[pdf_url] = full_text
            return full_text

    except Exception as e:
        logger.debug(f"PDF 提取异常: {e}")
        _pdf_text_cache[pdf_url] = ""
        return ""


def _extract_email_from_pdf_pages(
    html: str, page_url: str
) -> tuple[str, str]:
    """从页面中链接的 PDF 文件提取邮箱和完整文本"""
    pdf_urls = _find_pdf_urls(html, page_url)
    if not pdf_urls:
        return "", ""

    standard_pattern = re.compile(
        r"[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10}"
    )

    for pdf_url in pdf_urls:
        # _extract_text_from_pdf 内部会输出 PDF 检测/下载/解析进度
        pdf_text = _extract_text_from_pdf(pdf_url, page_url)
        if not pdf_text:
            continue

        # 提取邮箱
        emails = standard_pattern.findall(pdf_text)
        for email in emails:
            email_lower = email.strip(".").lower()
            if not any(
                x in email_lower
                for x in [
                    "office", "admin", "master", "system", "xyw",
                    "nic@", "library", "postmaster", "baoming", "advice",
                ]
            ):
                return email_lower, pdf_text

        # 如果有邮箱但是业务邮箱，也返回
        if emails:
            return emails[0].strip(".").lower(), pdf_text

        # PDF 有内容但没有邮箱 — 仍然返回文本用于简介提取
        if pdf_text.strip():
            return "", pdf_text

    return "", ""


# ====================================
# 邮箱提取
# ====================================

def _decode_cloudflare_email(cf_string: str) -> str:
    """解密 Cloudflare Email Protection 混淆邮箱"""
    try:
        if not cf_string:
            return ""
        hex_num = int(cf_string[:2], 16)
        return "".join([
            chr(int(cf_string[i:i + 2], 16) ^ hex_num)
            for i in range(2, len(cf_string), 2)
        ])
    except Exception:
        return ""


def _extract_email_from_contact_page(
    soup: BeautifulSoup, page_url: str
) -> str:
    """
    从当前页面的「联系我们」链接反查邮箱
    在教师个人页面找不到邮箱时，尝试找到「联系我们」链接并抓取该页面，
    从中提取邮箱（通常为院系公共邮箱）
    """
    # 查找「联系我们」链接
    contact_href = None
    contact_keywords = [
        "联系我们", "联系方式", "contact", "联系", "contact us",
    ]
    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(strip=True).lower()
        href = str(a_tag.get("href", ""))
        for kw in contact_keywords:
            if kw.lower() in text or kw.lower() in href.lower():
                contact_href = href
                break
        if contact_href:
            break

    if not contact_href:
        return ""

    # 构造完整 URL
    contact_url = urljoin(page_url, contact_href)

    # 避免抓取外部站点
    page_domain = urlparse(page_url).netloc
    contact_domain = urlparse(contact_url).netloc
    if contact_domain != page_domain:
        return ""

    try:
        logger.debug(f"尝试从联系我们页面提取: {contact_url}")
        session = _get_tsites_session()
        session.headers["Referer"] = page_url
        resp = session.get(contact_url, timeout=10)
        if resp.status_code != 200:
            return ""

        # 用同样的正则提取邮箱
        standard_pattern = re.compile(
            r"[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10}"
        )
        emails = standard_pattern.findall(resp.text)
        for email in emails:
            email_lower = email.lower().strip(".")
            if not any(
                x in email_lower
                for x in [
                    "office", "admin", "master", "system", "xyw",
                    "nic@", "library", "postmaster", "baoming", "advice",
                ]
            ):
                return email_lower

    except Exception as e:
        logger.debug(f"联系我们页面提取失败: {e}")

    return ""


def _extract_split_email(soup: BeautifulSoup, add_email_func) -> None:
    """
    提取被 HTML 元素拆分的邮箱（反爬虫拼接技术）
    某些页面将邮箱拆分到多个元素中以对抗爬虫，例如：
    <span>user</span><span>@</span><span>domain</span><span>.</span><span>com</span>
    或者 <a href="mailto:user">user</a>@<a>domain.com</a>
    """
    # 方法 1: 查找包含 @ 或邮件相关标记的连续行内元素组
    # 获取所有文本内容的连续性
    body_text = soup.get_text(separator="\n")
    # 在连接后的文本中（无分隔符）寻找可能被拆分的邮箱
    joined_text = soup.get_text(separator="")
    standard_pattern = re.compile(
        r"[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10}"
    )
    for email in standard_pattern.findall(joined_text):
        add_email_func(email)

    # 方法 2: 处理通过 <a> 标签拆分的情况
    # 如 <a href="mailto:user">user</a>@<span>domain.com</span>
    all_elements = soup.find_all(["span", "a", "em", "strong", "b", "i", "code"])
    for i, el in enumerate(all_elements):
        text = el.get_text(strip=True)
        if "@" in text:
            # 检查前后相邻元素是否可以拼接出完整邮箱
            combined = text
            for j in range(i + 1, min(i + 5, len(all_elements))):
                next_text = all_elements[j].get_text(strip=True)
                if next_text and len(next_text) < 30:
                    combined += next_text
            for email in standard_pattern.findall(combined):
                add_email_func(email)


def _extract_reversed_email(html_content: str) -> str:
    """提取并还原倒序存储的邮箱（如 HIT 教师主页系统将邮箱字符反转）。

    例：nc.ude.tih@yjgnef → fengjy@hit.edu.cn
    """
    if not html_content:
        return ""

    # 匹配类似邮箱的倒序字符串：包含 @ 且 @ 前后有字母数字和点号
    reversed_pattern = re.compile(
        r"[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-zA-Z0-9._-]*[a-zA-Z0-9]"
    )
    standard_pattern = re.compile(
        r"[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10}"
    )

    # 在 HTML 文本中查找候选倒序邮箱
    soup = BeautifulSoup(html_content, "html.parser")
    # 优先查找 class 包含 "Email" 或 "email" 的元素（如 HIT 的 EmailText）
    for tag in soup.find_all(
        attrs={"class": re.compile(r"[Ee]mail", re.IGNORECASE)} # type: ignore
    ):
        text = tag.get_text(strip=True)
        for candidate in reversed_pattern.findall(text):
            # 倒序还原
            restored = candidate[::-1]
            if standard_pattern.fullmatch(restored):
                # 过滤业务邮箱
                restored_lower = restored.lower()
                if not any(
                    x in restored_lower
                    for x in ["office", "admin", "master", "system", "xyw",
                              "nic@", "library", "postmaster", "baoming", "advice"]
                ):
                    return restored_lower

    # 全局搜索：在整个页面文本中查找
    text = soup.get_text()
    for candidate in reversed_pattern.findall(text):
        restored = candidate[::-1]
        if standard_pattern.fullmatch(restored):
            restored_lower = restored.lower()
            if not any(
                x in restored_lower
                for x in ["office", "admin", "master", "system", "xyw",
                          "nic@", "library", "postmaster", "baoming", "advice"]
            ):
                return restored_lower

    return ""


def extract_email(html_content: str, page_url: str = "") -> str:
    """从 HTML 中提取教师邮箱（综合多种策略）。
    策略：
    1. TSites 加密字段自动解密
    2. Cloudflare Email Protection 解密
    3. 标准 mailto: 链接提取
    4. 中文混淆还原（[at], [dot], （at） 等）
    5. 业务邮箱过滤（排除 office/admin/system 等）
    6. 联系我们页面反查
    7. PDF 附件提取
    """
    if not html_content:
        return ""

    # 策略 1: 先解密 TSites 加密字段
    html_content = _decrypt_tsites_fields(html_content, page_url)

    html_content = html_mod.unescape(html_content)
    soup = BeautifulSoup(html_content, "html.parser")
    found_emails: list[str] = []

    standard_pattern = re.compile(
        r"[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10}"
    )

    def _add_email(email_str: str) -> None:
        email_str = email_str.strip().strip(".").lower()
        email_str = re.sub(r"\s+", "", email_str)
        if email_str and "@" in email_str and email_str not in found_emails:
            if standard_pattern.fullmatch(email_str):
                found_emails.append(email_str)

    # 策略 2: Cloudflare Email Protection 解密
    for cf_tag in soup.find_all(attrs={"data-cfemail": True}):  # type: ignore[arg-type]
        decoded = _decode_cloudflare_email(str(cf_tag["data-cfemail"]))
        if decoded:
            _add_email(decoded)

    for a_cf in soup.find_all(
        "a", href=re.compile(r"cdn-cgi/l/email-protection", re.IGNORECASE)
    ):
        match = re.search(r"#([a-fA-F0-9]+)", str(a_cf.get("href", "")))
        if match:
            decoded = _decode_cloudflare_email(match.group(1))
            if decoded:
                _add_email(decoded)

    # 策略 3: 标准 mailto: 链接提取
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.IGNORECASE)):
        match = re.search(
            r"mailto:\s*([a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10})",
            unquote(str(a["href"])),
            re.IGNORECASE,
        )
        if match:
            _add_email(match.group(1))

    # 策略 4: 中文混淆还原
    dense_text = soup.get_text(separator="")
    text_content = re.sub(r"\s+", " ", soup.get_text(separator=" "))

    replacements = {
        "[at]": "@", "(at)": "@", "（at）": "@", "【at】": "@",
        "_at_": "@", "（艾特）": "@",
        " 圈 ": "@", "(圈)": "@", "（圈）": "@",
        "＠": "@", "#": "@", " AT ": "@", " a t ": "@",
        "[dot]": ".", "(dot)": ".", "（点）": ".", "【点】": ".",
        " dot ": ".", "。": ".", "．": ".", " D O T ": ".",
    }

    norm_text, norm_dense = text_content, dense_text
    for k, v in replacements.items():
        norm_text = norm_text.replace(k, v)
        norm_dense = norm_dense.replace(k, v)

    for ts in [norm_text, norm_dense]:
        for email in standard_pattern.findall(ts):
            _add_email(email)

    # 策略 4.5: 跨元素拆分邮箱还原（反爬虫拼接）
    # 某些页面将邮箱拆分为多个 span/元素，如 <span>user</span>@<span>domain.com</span>
    _extract_split_email(soup, _add_email)

    # 策略 4.6: 增强混淆还原 — 处理 HTML 注释插入和零宽字符
    enhanced_text = soup.get_text(separator="")
    # 移除 HTML 注释残留
    enhanced_text = re.sub(r"<!--.*?-->", "", enhanced_text)
    # 移除零宽字符 (zero-width space, zero-width non-joiner, etc.)
    enhanced_text = re.sub(r"[\u200b\u200c\u200d\u2060\u00ad\ufeff]", "", enhanced_text)
    # 处理 URL 编码的 @ (%40)
    enhanced_text = enhanced_text.replace("%40", "@")
    # 处理常见的空格/换行分隔的邮箱（如 "user @ domain . com"）
    enhanced_text = re.sub(r"(\w)\s+@\s+(\w)", r"\1@\2", enhanced_text)
    enhanced_text = re.sub(r"(\w)\s+\.\s+(\w)", r"\1.\2", enhanced_text)
    # 再次尝试匹配
    for email in standard_pattern.findall(enhanced_text):
        _add_email(email)

    # 策略 4.7: 从 JavaScript 变量中提取邮箱
    for script in soup.find_all("script"):
        if script.string:
            # 常见的 JS 邮箱变量模式
            js_patterns = [
                r'["\']([a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,10})["\']',
                r'var\s+\w*email\w*\s*=\s*["\']([^"\']+@[^"\']+\.[^"\']+)["\']',
                r'email\s*[:=]\s*["\']([^"\']+@[^"\']+\.[^"\']+)["\']',
            ]
            for js_pat in js_patterns:
                for match in re.finditer(js_pat, script.string, re.IGNORECASE):
                    email_candidate = match.group(1)
                    if standard_pattern.fullmatch(email_candidate):
                        _add_email(email_candidate)

    # 策略 5: 业务邮箱过滤 — 返回第一个非业务邮箱
    for email in found_emails:
        email_lower = email.lower()
        if not any(
            x in email_lower
            for x in [
                "office", "admin", "master", "system", "xyw",
                "nic@", "library", "postmaster", "baoming", "advice",
            ]
        ):
            logger.info(f"提取到：")
            logger.info(f"{email}")
            return email

    if found_emails:
        logger.info(f"提取到：")
        logger.info(f"{found_emails[0]}")
        return found_emails[0]

    # 策略 6: 从「联系我们」页面反查邮箱
    if page_url:
        contact_email = _extract_email_from_contact_page(soup, page_url)
        if contact_email:
            logger.info(f"提取到：")
            logger.info(f"{contact_email}")
            return contact_email

    # 策略 7: 从 PDF 附件中提取邮箱
    if page_url:
        pdf_email, _ = _extract_email_from_pdf_pages(html_content, page_url)
        if pdf_email:
            logger.info(f"解析后提取到：")
            logger.info(f"{pdf_email}")
            return pdf_email

    # 策略 8: 倒序邮箱还原（如 HIT 系统将邮箱字符反转存储）
    reversed_email = _extract_reversed_email(html_content)
    if reversed_email:
        logger.info(f"提取到（倒序还原）：")
        logger.info(f"{reversed_email}")
        return reversed_email

    logger.info("未提取到邮箱")
    return ""


# ======================================
# HTML 清洗
# ======================================

def _remove_unwanted_tags(soup: BeautifulSoup) -> None:
    """就地移除无用 HTML 标签"""
    for tag_name in [
        "script", "style", "nav", "footer", "header", "iframe",
        "noscript", "svg", "form", "button", "select", "input", "aside",
    ]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    nav_selectors = [
        "[class*=sidebar]", "[id*=sidebar]",
        "[class*=breadcrumb]", "[id*=breadcrumb]",
        "[class*=bread-crumb]", "[id*=bread-crumb]",
        "[class*=toolbar]", "[id*=toolbar]",
        "[class*=pagination]", "[id*=pagination]",
        "[class*=pageNum]", "[id*=pageNum]",
        "[class*=recommend]", "[id*=recommend]",
        "[class*=related]", "[id*=related]",
        "[class*=comment]", "[id*=comment]",
        "[class*=subnav]", "[id*=subnav]",
        "[class*=sub-menu]", "[id*=sub-menu]",
        "[class*=topbar]", "[id*=topbar]",
        "[class*=scroll]", "[id*=scroll]",
    ]
    for selector in nav_selectors:
        for tag in soup.select(selector):
            tag.decompose()


def _remove_by_class_or_id(soup: BeautifulSoup) -> None:
    """移除 class/id 包含无关关键词的元素"""
    unwanted_selectors: list[str] = []
    for keyword in REMOVE_KEYWORDS:
        for attr in ("class", "id"):
            unwanted_selectors.append(f"[{attr}*={keyword}]")
    selector_str = ",".join(unwanted_selectors)
    for element in soup.select(selector_str):
        element.decompose()


def _remove_html_comments(soup: BeautifulSoup) -> None:
    """移除 HTML 注释"""
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()


def _extract_visible_text(soup: Union[BeautifulSoup, Tag]) -> str:
    """从清洗后的 soup 提取可见文本"""
    lines: list[str] = []
    for element in soup.find_all([
        "p", "div", "span", "li", "td", "h1", "h2", "h3",
        "h4", "h5", "h6", "section", "article",
        "dd", "dt", "blockquote", "pre",
    ]):
        text = element.get_text(strip=True)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _get_dom_text_density(element: Tag) -> float:
    """计算 DOM 元素的文本密度（文本长度 / 标签数）"""
    text_len = len(element.get_text(strip=True))
    tag_count = len(element.find_all(True))
    if tag_count == 0:
        return 0.0
    return text_len / tag_count


def _find_main_content_by_density(soup: BeautifulSoup) -> Optional[Tag]:
    """通过文本密度定位主要内容区域"""
    candidates: list[tuple[float, Tag]] = []

    for tag in soup.find_all(True):
        if tag.name not in ("div", "section", "article", "main", "td", "body"):
            continue
        if tag.find_parent(["header", "footer", "nav", "aside"]):
            continue
        density = _get_dom_text_density(tag)
        text = tag.get_text(strip=True)
        if len(text) > 50:
            candidates.append((density, tag))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _remove_meaningless_lines(lines: list[str]) -> list[str]:
    """过滤无意义文本行"""
    noise_patterns = re.compile(
        r"^[\s>#*=\-~]{2,}$"
        "|"
        r"^(点击(查看|下载|进入|更多|关闭|展开|收起)|更多>>|阅读全文|查看详情|返回顶部|下载附件)$"
        "|"
        r"^(打印本页|关闭窗口|加入收藏|设为首页|在线留言|联系我们)$"
        "|"
        r"^(Copyright|ICP备|公安备案|版权所有).*$"
        "|"
        r"^[\d\s/:-]{3,}$"
    )
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if noise_patterns.search(stripped):
            continue
        result.append(stripped)
    return result


def _deduplicate_lines(lines: list[str]) -> list[str]:
    """去除连续重复行"""
    result: list[str] = []
    for line in lines:
        if not result or line != result[-1]:
            result.append(line)
    return result


def _normalize_whitespace(text: str) -> str:
    """规范化所有空白字符"""
    text = re.sub(r"[\s\t\r\u00A0\u3000]+", " ", text)
    return text.strip()


def clean_html(html: str) -> str:
    """清洗 HTML，移除无关内容并提取正文

    策略：
    1. BeautifulSoup 解析
    2. 移除无用标签
    3. 按 class/id 关键词移除
    4. 移除 HTML 注释
    5. DOM 密度算法定位主内容
    6. 提取可见文本
    7. 过滤无意义行
    8. 去除连续重复行
    9. 规范化空白
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "lxml")

    _remove_unwanted_tags(soup)
    _remove_by_class_or_id(soup)
    _remove_html_comments(soup)

    main_content = _find_main_content_by_density(soup)

    if main_content:
        main_soup = BeautifulSoup(str(main_content), "lxml")
        _remove_unwanted_tags(main_soup)
        _remove_html_comments(main_soup)
        text = _extract_visible_text(main_soup)
    else:
        body = soup.find("body")
        if body:
            text = _extract_visible_text(body)
        else:
            text = soup.get_text(separator="\n", strip=True)

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    lines = _remove_meaningless_lines(lines)
    lines = _deduplicate_lines(lines)

    text = "\n".join(lines)
    text = _normalize_whitespace(text)

    return text


# =======================================
# 边角内容检测
# =======================================

# 导航/网站结构关键词（大量出现说明提取到的是导航而非简介）
NAV_KEYWORDS: list[str] = [
    "本站首页", "学院概况", "学院简介", "学院领导", "组织机构", "学院宣传片",
    "师资队伍", "专任教师", "行政人员", "党建思政", "工会之窗",
    "教育教学", "本科生教育", "硕士生教育", "教育教学审核评估",
    "科学研究", "科研动态", "科研平台", "学团工作",
    "招生就业", "招生信息", "就业动态", "下载专区",
    "返回首页", "设为首页", "加入收藏", "网站地图",
    "友情链接", "快速链接", "快速导航", "首页",
    "通知公告", "学院新闻", "新闻动态",
    "部门首页", "学院介绍", "机构设置", "现任领导",
    "思政课建设", "本科生思政课教学", "研究生思政课教学", "教学研讨",
    "学科建设", "学科概况", "培养方案", "导师队伍",
    "科研项目", "教研成果", "教研获奖",
    "党群工作", "党组织架构", "分工会组成", "工作快讯",
    "学生工作", "规章制度", "组织设置", "获奖情况", "活动掠影",
    "智慧马院", "联系我们",
]

# 公告/通知类关键词（非教师简介的公告内容）
ANNOUNCEMENT_KEYWORDS: list[str] = [
    "不动产权证", "房改政策", "房屋所有权", "转移登记",
    "不动产登记中心", "公告期满", "房屋坐落", "房屋产权证号",
    "公告", "公示", "通知",
]

# 页脚/版权/面包屑标记（出现即非教师简介，这些标记不会出现在正常简介中）
FOOTER_MARKERS: list[str] = [
    "当前位置", "您的位置", "正文",
    "版权所有", "Copyright", "COPYRIGHT",
    "京ICP备", "ICP备", "京公网安备", "公网安备",
    "扫一扫", "分享到", "浏览次数", "发布时间", "文章来源",
    "升级浏览器", "请升级", "浏览器版本", "旧版本浏览器",
]

# 教师简介应包含的关键词
TEACHER_KEYWORDS: list[str] = [
    "姓名", "性别", "民族", "出生", "职称", "职务",
    "研究方向", "研究领域", "研究兴趣",
    "教育背景", "学历", "学位", "毕业",
    "工作经历", "工作单位",
    "科研", "论文", "项目", "获奖", "专利", "著作",
    "主讲课程", "教学", "授课",
    "联系方式", "邮箱", "电话",
    "导师", "硕导", "博导",
    "教师", "教授", "副教授", "讲师", "研究员",
    "博士", "硕士", "学士",
]


def is_corner_content(text: str) -> bool:
    """检测提取的文本是否为网页边角内容（导航菜单、公告等非教师简介内容）。

    判断逻辑：
    1. 包含页脚/版权/面包屑标记且无教师信息 → 边角内容
    2. 导航关键词数量远超教师关键词（比例 >= 3:1）→ 边角内容
    3. 导航关键词 >= 5 且教师关键词 < 2 → 边角内容
    4. 公告关键词 >= 2 且教师关键词为 0 → 边角内容
    5. 文本长度 > 100 且教师关键词为 0 → 边角内容
    """
    if not text or len(text.strip()) < 10:
        return True

    nav_count = sum(1 for kw in NAV_KEYWORDS if kw in text)
    announcement_count = sum(1 for kw in ANNOUNCEMENT_KEYWORDS if kw in text)
    teacher_count = sum(1 for kw in TEACHER_KEYWORDS if kw in text)

    # 页脚/版权/面包屑标记 — 这些标记绝不会出现在正常教师简介中
    if any(kw in text for kw in FOOTER_MARKERS) and teacher_count == 0:
        return True

    # 导航关键词远超教师关键词（导航菜单中常有"教授/副教授/讲师"等菜单项）
    if nav_count >= 5 and nav_count >= teacher_count * 3:
        return True

    # 导航关键词占主导（大量导航菜单文本）
    if nav_count >= 5 and teacher_count < 2:
        return True

    # 公告类内容且完全不包含教师信息
    if announcement_count >= 2 and teacher_count == 0:
        return True

    # 较长文本但没有任何教师相关关键词
    if len(text) > 100 and teacher_count == 0:
        return True

    return False


# =======================================
# 教师简介提取（对外接口）
# =======================================

def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    """使用 trafilatura 提取正文"""
    try:
        result = trafilatura.extract(
            html,
            url=url,
            output_format="txt",
            include_images=False,
            include_links=False,
            include_tables=True,
            no_fallback=False,
            favor_precision=True,
        )
        if result and len(result.strip()) >= MIN_CONTENT_LENGTH:
            return result.strip()
    except Exception as e:
        logger.debug(f"trafilatura 提取失败: {e}")
    return None


def _extract_with_custom(html: str) -> Optional[str]:
    """回退：使用自定义 HTML 清洗器提取"""
    try:
        text = clean_html(html)
        if text and len(text) >= MIN_CONTENT_LENGTH:
            return text
    except Exception as e:
        logger.debug(f"自定义提取失败: {e}")
    return None


def _fetch_hit_teacher_body(html: str, page_url: str) -> Optional[str]:
    """从 HIT 教师主页 API（teacherBody.do）获取完整简介内容。

    哈尔滨工业大学教师个人主页系统（homepage.hit.edu.cn）使用 AJAX 动态加载
    简介内容，原始 HTML 中不含实际文本，需调用后端接口获取。
    """
    if "homepage.hit.edu.cn" not in page_url:
        return None

    # 从 HTML 中提取教师 ID（data-tid 属性）
    match = re.search(r'data-tid="([^"]+)"', html)
    if not match:
        logger.debug("HIT 页面未找到 data-tid")
        return None

    teacher_id = match.group(1)
    api_url = urljoin(page_url, "/TeacherHome/teacherBody.do")

    try:
        session = _get_tsites_session()
        session.headers["Referer"] = page_url
        logger.info("检测到 HIT 教师主页，通过 API 获取简介……")
        resp = session.post(api_url, data={"id": teacher_id}, timeout=15)
        if resp.status_code != 200:
            logger.debug(f"HIT API 请求失败: HTTP {resp.status_code}")
            return None

        # API 返回 HTML 片段，提取纯文本
        soup = BeautifulSoup(resp.text, "html.parser")
        # 移除管理按钮
        for btn in soup.find_all("i", onclick=True):
            btn.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 清理空白行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        text = "\n".join(lines)

        if text and len(text) >= MIN_CONTENT_LENGTH:
            logger.info("HIT API 提取成功")
            return text
        else:
            logger.debug("HIT API 返回内容过短")
            return None

    except Exception as e:
        logger.debug(f"HIT API 提取异常: {e}")
        return None


def extract_teacher_profile(html: str, url: str) -> str:
    """提取教师简介文本（动态 API 优先，PDF 次之，trafilatura / 自定义清洗兜底）"""
    if not html:
        return ""

    # 先解密 TSites 加密字段，确保邮箱等联系信息对提取器可见
    html = _decrypt_tsites_fields(html, url)

    # HIT 等动态加载网站：优先通过后端 API 获取简介
    hit_text = _fetch_hit_teacher_body(html, url)
    if hit_text:
        return hit_text

    # 优先从 PDF 提取（PDF 通常包含完整的教师简历）
    pdf_text = ""
    if url:
        _, pdf_text = _extract_email_from_pdf_pages(html, url)

    if pdf_text and len(pdf_text) >= MIN_CONTENT_LENGTH:
        return pdf_text

    # HTML 提取兜底
    text = _extract_with_trafilatura(html, url)
    if text and len(text) >= MIN_CONTENT_LENGTH:
        return text

    text = _extract_with_custom(html)
    if text and len(text) >= MIN_CONTENT_LENGTH:
        return text

    return ""
