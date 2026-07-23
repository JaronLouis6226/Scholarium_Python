"""
Scholarium Web UI —— 高校教师主页信息采集工具（Web版）

启动后自动打开浏览器，通过 Web 界面配置 URL 列表和参数，实时查看采集进度。
功能与 Main_Terminal.py 相同。
"""

import json
import os
import queue
import sys
import threading
import time
import webbrowser
from typing import List, Optional, Tuple

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from src import crawler
from src import extractor
from src import writer
from src.utils import logger

# ============================================================================
# Flask 应用
# ============================================================================

app = Flask(__name__)

# SSE 消息队列（线程安全）
_sse_queue: queue.Queue[str] = queue.Queue()

# 采集结果（线程间共享）
_results: List[writer.ResultRow] = []
_output_file: str = ""
_task_running: bool = False
_task_lock = threading.Lock()

# 原始 logger.info 方法引用
_original_logger_info = logger.info


def _emit_log(message: str) -> None:
    """同时输出到终端和 SSE 队列"""
    _original_logger_info(message)
    _sse_queue.put(message)


# ============================================================================
# 核心采集逻辑
# ============================================================================

def _parse_urls(text: str) -> List[str]:
    parsed: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            parsed.append(line)
    return parsed


def _process_single_url(url: str) -> Tuple[str, str, str]:
    fetch_result = crawler.fetch_url(url)

    if fetch_result.error:
        if "404" in fetch_result.error:
            _emit_log("状态：网页404")
        elif "超时" in fetch_result.error or "Timeout" in fetch_result.error:
            _emit_log("状态：网页连接超时")
        else:
            _emit_log("状态：链接无效")
        return ("", "网页无邮箱", "链接无效")

    if not fetch_result.success or not fetch_result.html:
        _emit_log("状态：链接无效")
        return ("", "网页无邮箱", "链接无效")

    _emit_log("2. 提取教师简介中……")
    profile_text = extractor.extract_teacher_profile(fetch_result.html, url)

    if profile_text:
        _emit_log("提取成功")
    else:
        _emit_log("提取失败")
        _emit_log("页面不存在教师简介信息")

    _emit_log("3. 提取教师邮箱中……")
    _emit_log("尝试从页面提取……")

    email = extractor.extract_email(fetch_result.html, url)
    if not email:
        email = "网页无邮箱"

    extractor.clear_pdf_cache()

    if not profile_text:
        _emit_log("状态：主页无内容")
        return ("", email, "主页无内容")

    if extractor.is_corner_content(profile_text):
        _emit_log("检测到内容几乎全为导航/公告等非教师简介，不填入表格")
        return ("", email, "主页无内容")

    max_len = 1500
    if len(profile_text) > max_len:
        profile_text = profile_text[:max_len]

    _emit_log("提取完成")
    return (profile_text, email, "")


def _run_crawl(
    urls_text: str,
    timeout: int,
    retries: int,
    delay_min: float,
    delay_max: float,
    min_content_len: int,
    output_filename: str,
) -> None:
    global _results, _output_file, _task_running

    with _task_lock:
        _task_running = True

    old_info = logger.info
    logger.info = _emit_log

    try:
        crawler.REQUEST_TIMEOUT = timeout
        crawler.MAX_RETRIES = retries
        crawler.RANDOM_DELAY_MIN = delay_min
        crawler.RANDOM_DELAY_MAX = delay_max
        extractor.MIN_CONTENT_LENGTH = min_content_len

        url_list = _parse_urls(urls_text)

        if not url_list:
            _emit_log("未发现有效 URL，请检查输入。")
            return

        total = len(url_list)
        _emit_log("［开始采集］")
        _emit_log(f"共 {total} 个URL")
        _emit_log("")

        results: List[writer.ResultRow] = []

        for idx, url in enumerate(url_list, 1):
            _emit_log(f"［{idx}/{total}］")
            _emit_log(f"URL：{url}")
            _emit_log("")

            content, email, has_content = _process_single_url(url)
            results.append((content, email, has_content))
            _emit_log("")

            if idx < total:
                crawler.random_delay()

        try:
            writer.write_excel(results, output_filename)
            _output_file = output_filename
        except Exception as e:
            _emit_log(f"Excel 写入失败: {e}")
            return

        with _task_lock:
            _results = results

        total = len(results)
        has_content = sum(1 for _, _, c in results if c == "")
        no_content = sum(1 for _, _, c in results if c == "主页无内容")
        dead_link = sum(1 for _, _, c in results if c == "链接无效")

        _emit_log("")
        _emit_log("［采集完成］")
        _emit_log(f"总计: {total}  |  有内容: {has_content}  |  无内容: {no_content}  |  无效: {dead_link}")

    finally:
        logger.info = old_info
        _sse_queue.put("__DONE__")
        with _task_lock:
            _task_running = False


# ============================================================================
# Flask 路由
# ============================================================================

@app.route("/")
def index() -> str:
    """主页面 —— 内嵌完整前端"""
    return _HTML_PAGE


@app.route("/font/AnthropicSerif.otf")
def font_anthropic() -> Response:
    """提供 AnthropicSerif 字体文件"""
    import os as _os
    font_dir = _os.path.join(_os.path.dirname(__file__), "src")
    return send_from_directory(font_dir, "ANTHROPICSERIF.OTF", mimetype="font/otf")


@app.route("/run", methods=["POST"])
def run() -> Response:
    """启动采集任务"""
    global _results, _output_file, _task_running

    with _task_lock:
        if _task_running:
            return jsonify({"error": "已有任务在运行中"}), 409 # type: ignore
        _results = []
        _output_file = ""

    data = request.get_json(silent=True) or {}
    urls_text = data.get("urls", "")
    timeout = int(data.get("timeout", 5))
    retries = int(data.get("retries", 3))
    delay_min = float(data.get("delay_min", 0.1))
    delay_max = float(data.get("delay_max", 0.5))
    min_content_len = int(data.get("min_content_len", 30))
    output_filename = data.get("output_filename", "teachers.xlsx")

    thread = threading.Thread(
        target=_run_crawl,
        args=(urls_text, timeout, retries, delay_min, delay_max,
              min_content_len, output_filename),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started"})


@app.route("/stream")
def stream() -> Response:
    """SSE 端点：实时推送采集日志"""
    def generate():
        while True:
            try:
                msg = _sse_queue.get(timeout=30)
                if msg == "__DONE__":
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'log', 'text': msg})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/results")
def results() -> Response:
    """获取采集结果"""
    with _task_lock:
        data = [
            {"content": c, "email": e, "status": s}
            for c, e, s in _results
        ]
    return jsonify({"results": data, "output_file": _output_file})


@app.route("/download")
def download() -> Response:
    """下载 Excel 文件"""
    filepath = os.path.join(os.getcwd(), _output_file)
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404 # type: ignore
    return send_file(filepath, as_attachment=True)


@app.route("/status")
def status() -> Response:
    """查询任务是否运行中"""
    with _task_lock:
        return jsonify({"running": _task_running})


# ============================================================================
# 前端页面
# ============================================================================

_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scholarium</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #FAf9F5;
    --surface: #FFFFFF;
    --accent: #D97757;
    --accent-hover: #B56C4E;
    --accent-subtle: rgba(204,125,94,0.06);
    --text: #141413;
    --text-secondary: #6E6E6E;
    --text-tertiary: #999999;
    --border: #E8E5E0;
    --input-border: #D9D4CC;
    --log-bg: #F4F2EE;
    --success: #059669;
    --error: #DC2626;
    --radius: 20px;
    --radius-sm: 8px;
    --font: "AnthropicSerif", -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    --font-mono: "SF Mono", "Fira Code", "Fira Mono", Menlo, monospace;
    --shadow-card: 0 1px 2px rgba(0,0,0,0.03), 0 2px 8px rgba(0,0,0,0.04);
  }

  @font-face {
    font-family: "AnthropicSerif";
    src: url("/font/AnthropicSerif.otf") format("opentype");
    font-weight: 400;
  }

  html { height: 100%; }

  body {
    height: 100%;
    font-family: var(--font);
    font-size: 14px;
    line-height: 1.65;
    color: var(--text);
    background: var(--bg);
    display: flex;
    flex-direction: column;
    padding: 16px;
    gap: 14px;
    -webkit-font-smoothing: antialiased;
  }

  /* Header card */
  .header {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px 18px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-card);
    flex-shrink: 0;
  }
  .logo {
    font-size: 30px;
    font-weight: 800;
    color: var(--accent);
    letter-spacing: -0.3px;
  }

  /* Container */
  .container {
    display: flex;
    flex-direction: row;
    flex: 1;
    overflow: hidden;
    min-height: 0;
    gap: 14px;
  }

  /* Left panel */
  .left-panel {
    flex: 4;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 0;
    min-width: 0;
  }

  /* Right panel */
  .right-panel {
    flex: 6;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
    gap: 12px;
  }

  /* Cards */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    box-shadow: var(--shadow-card);
  }
  .card-stretch {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }

  /* Section label (replaces card-title) */
  .section-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    text-align: center;
    margin-bottom: 12px;
  }

  /* Inputs */
  label {
    display: block;
    font-size: 12px;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 4px;
    text-align: center;
  }
  textarea, input[type="text"], input[type="number"] {
    width: 100%;
    border: 1px solid var(--input-border);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    font-size: 12px;
    font-family: var(--font);
    color: var(--text);
    background: var(--surface);
    transition: border-color 0.2s, box-shadow 0.2s;
    outline: none;
  }
  textarea::placeholder, input::placeholder { color: var(--text-tertiary); }
  textarea:focus, input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(204,125,94,0.1);
  }
  textarea {
    resize: vertical;
    min-height: 120px;
    font-size: 12px;
    line-height: 1.6;
  }

  .param-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-bottom: 8px;
  }
  .param-group { margin-bottom: 8px; }
  .param-group label { text-align: left; font-size: 11px; color: var(--text-secondary); }
  .param-group input { padding: 6px 8px; font-size: 12px; }
  .param-hint { display: none; }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 10px 16px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    transition: background 0.2s;
    font-family: var(--font);
  }
  .btn-primary {
    background: var(--accent);
    color: #FFF;
    width: 100%;
  }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-primary:active { background: #a05038; }
  .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-secondary {
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    font-size: 12px;
    font-weight: 500;
    width: 100%;
  }
  .btn-secondary:hover { background: var(--log-bg); }
  .btn-secondary:active { background: #eae6e0; }

  .btn-row {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 12px;
  }

  /* Advanced toggle */
  .advanced-toggle {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 4px 0;
    cursor: pointer;
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    user-select: none;
    transition: color 0.15s;
    background: none;
    border: none;
    font-family: var(--font);
    margin: 10px auto 8px;
  }
  .advanced-toggle:hover { color: var(--accent); }
  .advanced-toggle .arrow {
    display: inline-block;
    transition: transform 0.2s;
    font-size: 10px;
  }
  .advanced-toggle.open .arrow { transform: rotate(90deg); }
  .advanced-params { display: none; }
  .advanced-params.open { display: block; }

  /* Log area */
  .log-panel {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .log-area {
    flex: 1;
    min-height: 100px;
    overflow-y: auto;
    background: var(--log-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.75;
    color: var(--text);
    text-align: center;
    position: relative;
  }
  .log-placeholder {
    color: var(--text-tertiary);
    font-size: 12px;
    text-align: center;
    line-height: 1.6;
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 100%;
    font-family: var(--font);
    pointer-events: none;
  }
  .log-line {
    white-space: pre-wrap;
    word-break: break-all;
    text-align: left;
  }
  .log-line.status-ok { color: var(--success); }
  .log-line.status-warn { color: var(--accent); }
  .log-line.status-err { color: var(--error); }

  .stat-bar {
    display: flex;
    gap: 14px;
    justify-content: center;
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 8px;
    flex-shrink: 0;
  }
  .stat-bar strong { color: var(--text); }

  /* Right empty state */
  .right-empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    color: var(--text-tertiary);
  }
  .empty-icon { opacity: 0.35; }
  .empty-icon svg { width: 32px; height: 32px; }
  .empty-text { font-size: 13px; text-align: center; line-height: 1.6; }

  /* Results section */
  .results-section {
    display: flex;
    flex-direction: column;
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }
  .results-header {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    padding: 0 0 10px;
    flex-shrink: 0;
  }
  .results-title { font-size: 13px; font-weight: 600; color: var(--text); }
  .results-summary { font-size: 11px; color: var(--text-secondary); }

  .results-table-wrapper {
    flex: 1;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    min-height: 0;
  }

  #results-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    line-height: 1.6;
    table-layout: fixed;
  }
  #results-table thead { position: sticky; top: 0; z-index: 1; }
  #results-table th {
    text-align: center;
    padding: 10px 10px;
    background: var(--log-bg);
    font-weight: 600;
    font-size: 12px;
    color: var(--text);
    border-bottom: 1.5px solid var(--border);
    white-space: nowrap;
  }
  .th-label { display: block; text-align: center; margin-bottom: 4px; }
  .th-copy-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin: 0 auto;
    padding: 2px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--accent);
    font-size: 10px;
    font-family: var(--font);
    font-weight: 500;
    line-height: 1.5;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .th-copy-btn:hover { background: var(--accent-subtle); border-color: var(--accent); }
  .th-copy-btn.copied { color: var(--success); border-color: var(--success); background: transparent; }

  #results-table td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
    color: var(--text);
    overflow: hidden;
  }
  #results-table tbody tr:nth-child(even) { background: rgba(0,0,0,0.015); }
  #results-table tbody tr:hover { background: var(--accent-subtle); }
  #results-table tr:last-child td { border-bottom: none; }

  .col-content { width: 55%; }
  .col-email   { width: 25%; }
  .col-status  { width: 20%; }

  .cell-content {
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    white-space: normal;
    word-break: break-all;
  }
  .cell-empty  { color: var(--text-tertiary); font-style: italic; }
  .cell-email  { font-family: var(--font-mono); font-size: 11px; word-break: break-all; }
  .cell-status { font-size: 11px; }
  .status-ok         { color: var(--success); }
  .status-no-content { color: var(--accent); }
  .status-error      { color: var(--error); }

  .results-actions { padding: 10px 0 0; flex-shrink: 0; }
  .results-actions .btn-primary { width: 100%; }

  @media (max-width: 780px) {
    body { padding: 10px; gap: 10px; }
    .container { flex-direction: column; }
    .left-panel { flex: none; width: 100%; }
    .right-panel { min-height: 400px; }
  }
</style>
</head>
<body>
  <!-- Header Card -->
  <header class="header">
    <div class="logo">Scholarium</div>
  </header>

  <div class="container">
    <!-- Left Panel -->
    <div class="left-panel">
      <!-- URL Input Card -->
      <div class="card">
        <div class="section-label">教师主页 URL</div>
        <textarea id="urls" placeholder="粘贴教师主页 URL，每行一个&#10;例如：https://example.edu.cn/teacher/zhangsan" rows="4"></textarea>

        <button class="advanced-toggle" id="advanced-toggle" onclick="toggleAdvanced()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="3"/>
            <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
          </svg>
          <span>高级设置</span>
          <svg class="arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </button>

        <div class="advanced-params" id="advanced-params">
          <div class="param-row">
            <div class="param-group">
              <label for="timeout">请求超时（秒）</label>
              <input type="number" id="timeout" value="5" min="1" max="60" step="1">
            </div>
            <div class="param-group">
              <label for="retries">最大重试次数</label>
              <input type="number" id="retries" value="3" min="0" max="10" step="1">
            </div>
          </div>
          <div class="param-row">
            <div class="param-group">
              <label for="delay_min">最小延迟（秒）</label>
              <input type="number" id="delay_min" value="0.1" min="0" max="10" step="0.1">
            </div>
            <div class="param-group">
              <label for="delay_max">最大延迟（秒）</label>
              <input type="number" id="delay_max" value="0.5" min="0" max="10" step="0.1">
            </div>
          </div>
          <div class="param-row">
            <div class="param-group">
              <label for="min_content_len">最小内容长度</label>
              <input type="number" id="min_content_len" value="30" min="10" max="500" step="10">
            </div>
            <div class="param-group">
              <label for="output_filename">输出文件名</label>
              <input type="text" id="output_filename" value="teachers.xlsx">
            </div>
          </div>
        </div>

        <div class="btn-row">
          <button class="btn btn-primary" id="btn-run" onclick="startCrawl()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            开始采集
          </button>
          <button class="btn btn-secondary" id="btn-download" onclick="downloadExcel()" disabled>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            导出 Excel
          </button>
        </div>
      </div>

      <!-- Log Card -->
      <div class="card card-stretch log-panel">
        <div class="log-area" id="log-area">
          <div class="log-placeholder" id="log-placeholder">点击"开始采集"后<br>运行日志将显示在此处</div>
        </div>
        <div class="stat-bar" id="stat-bar" style="display:none"></div>
      </div>
    </div>

    <!-- Right Panel -->
    <div class="right-panel">
      <!-- Empty State -->
      <div class="card card-stretch" id="right-empty">
        <div class="right-empty">
          <div class="empty-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
          </div>
          <div class="empty-text">采集完成后<br>结果将显示在此处</div>
        </div>
      </div>

      <!-- Results Card -->
      <div class="card card-stretch results-section" id="results-panel" style="display:none">
        <div class="results-header">
          <span class="results-title" id="results-title">采集结果</span>
          <span class="results-summary" id="results-summary"></span>
        </div>
        <div class="results-table-wrapper">
          <table id="results-table">
            <thead>
              <tr>
                <th class="col-content"><span class="th-label">教师简介</span><button class="th-copy-btn" onclick="copyColumn(0)">复制整列</button></th>
                <th class="col-email"><span class="th-label">邮箱</span><button class="th-copy-btn" onclick="copyColumn(1)">复制整列</button></th>
                <th class="col-status"><span class="th-label">状态</span><button class="th-copy-btn" onclick="copyColumn(2)">复制整列</button></th>
              </tr>
            </thead>
            <tbody id="results-tbody"></tbody>
          </table>
        </div>
        <div class="results-actions" id="results-actions">
          <button class="btn btn-primary" id="btn-download-bottom" onclick="downloadExcel()">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            导出 Excel
          </button>
        </div>
      </div>
    </div>
  </div>

<script>
const logArea = document.getElementById('log-area');
const logPlaceholder = document.getElementById('log-placeholder');
const btnRun = document.getElementById('btn-run');
const btnDownload = document.getElementById('btn-download');
const statBar = document.getElementById('stat-bar');
const resultsPanel = document.getElementById('results-panel');
const rightEmpty = document.getElementById('right-empty');
const resultsTbody = document.getElementById('results-tbody');
const resultsSummary = document.getElementById('results-summary');

let eventSource = null;
var _allResults = [];

function appendLog(text, cls) {
  if (logPlaceholder) logPlaceholder.style.display = 'none';
  const span = document.createElement('div');
  span.textContent = text;
  span.className = 'log-line' + (cls ? ' ' + cls : '');
  logArea.appendChild(span);
  logArea.scrollTop = logArea.scrollHeight;
}

function clearLog() {
  logArea.querySelectorAll('.log-line').forEach(el => el.remove());
  if (logPlaceholder) logPlaceholder.style.display = '';
}

async function startCrawl() {
  const urls = document.getElementById('urls').value.trim();
  if (!urls) { alert('请输入至少一个 URL'); return; }

  btnRun.disabled = true;
  btnDownload.disabled = true;
  resultsPanel.style.display = 'none';
  rightEmpty.style.display = '';
  statBar.style.display = 'none';
  clearLog();
  appendLog('正在启动采集任务…', 'status-warn');

  const config = {
    urls: urls,
    timeout: parseInt(document.getElementById('timeout').value) || 5,
    retries: parseInt(document.getElementById('retries').value) || 3,
    delay_min: parseFloat(document.getElementById('delay_min').value) || 0.1,
    delay_max: parseFloat(document.getElementById('delay_max').value) || 0.5,
    min_content_len: parseInt(document.getElementById('min_content_len').value) || 30,
    output_filename: document.getElementById('output_filename').value || 'teachers.xlsx'
  };

  try {
    const resp = await fetch('/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });
    if (!resp.ok) {
      const err = await resp.json();
      appendLog('错误: ' + (err.error || '启动失败'), 'status-err');
      btnRun.disabled = false;
      return;
    }
  } catch(e) {
    appendLog('连接失败: ' + e.message, 'status-err');
    btnRun.disabled = false;
    return;
  }

  if (eventSource) eventSource.close();
  eventSource = new EventSource('/stream');

  eventSource.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.type === 'log') {
      appendLog(data.text);
    } else if (data.type === 'done') {
      eventSource.close();
      eventSource = null;
      btnRun.disabled = false;
      fetchResults();
    }
  };

  eventSource.onerror = function() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    btnRun.disabled = false;
  };
}

async function fetchResults() {
  try {
    const resp = await fetch('/results');
    const data = await resp.json();
    const results = data.results || [];
    if (results.length === 0) return;

    _allResults = results;

    const hasC = results.filter(r => r.status === '').length;
    const noC  = results.filter(r => r.status === '主页无内容').length;
    const dead = results.filter(r => r.status === '链接无效').length;

    statBar.innerHTML =
      '<span>总计: <strong>' + results.length + '</strong></span>' +
      '<span style="color:var(--success)">有内容: <strong>' + hasC + '</strong></span>' +
      '<span style="color:var(--accent)">无内容: <strong>' + noC + '</strong></span>' +
      '<span style="color:var(--error)">无效: <strong>' + dead + '</strong></span>';
    statBar.style.display = 'flex';

    resultsSummary.textContent = '共 ' + results.length + ' 条';

    var html = '';
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      var statusText = r.status || '';
      var statusCls = r.status === '链接无效' ? 'status-error' :
                      r.status === '主页无内容' ? 'status-no-content' :
                      'status-ok';

      html += '<tr>' +
        '<td class="col-content"><div class="cell-content">' + escHtml(r.content || '') + '</div></td>' +
        '<td class="col-email"><div class="cell-email">' + escHtml(r.email || '') + '</div></td>' +
        '<td class="col-status"><div class="cell-status ' + statusCls + '">' + escHtml(statusText) + '</div></td>' +
        '</tr>';
    }
    resultsTbody.innerHTML = html;

    rightEmpty.style.display = 'none';
    resultsPanel.style.display = 'flex';

    if (data.output_file) {
      btnDownload.disabled = false;
      document.getElementById('btn-download-bottom').disabled = false;
    }
  } catch(e) {
    console.error(e);
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggleAdvanced() {
  var btn = document.getElementById('advanced-toggle');
  var panel = document.getElementById('advanced-params');
  btn.classList.toggle('open');
  panel.classList.toggle('open');
}

function copyColumn(colIdx) {
  var values = [];
  for (var i = 0; i < _allResults.length; i++) {
    var r = _allResults[i];
    var val = colIdx === 0 ? (r.content || '') :
              colIdx === 1 ? (r.email || '') :
              (r.status || '');
    if (val.indexOf('\n') !== -1 || val.indexOf('"') !== -1 || val.indexOf(',') !== -1) {
      val = '"' + val.replace(/"/g, '""') + '"';
    }
    values.push(val);
  }
  var text = values.join('\n');
  navigator.clipboard.writeText(text).then(function() {
    var btns = document.querySelectorAll('.th-copy-btn');
    var btn = btns[colIdx];
    if (btn) {
      var orig = btn.textContent;
      btn.textContent = '已复制';
      btn.classList.add('copied');
      setTimeout(function() {
        btn.textContent = orig;
        btn.classList.remove('copied');
      }, 1500);
    }
  }).catch(function(err) {
    console.error('复制失败:', err);
  });
}

function downloadExcel() {
  window.location.href = '/download';
}
</script>
</body>
</html>"""



def main() -> None:
    port = 5080
    url = f"http://127.0.0.1:{port}"
    print(f"Scholarium Web UI 启动: {url}")
    print("按 Ctrl+C 停止服务器\n")

    def _open_browser():
        time.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
