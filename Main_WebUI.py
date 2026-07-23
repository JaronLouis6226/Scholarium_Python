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
    --bg: #F9F8F6;
    --surface: #FFFFFF;
    --accent: #D77757;
    --accent-hover: #C06040;
    --accent-subtle: rgba(215,119,87,0.06);
    --text: #1A1A1A;
    --text-secondary: #6E6E6E;
    --border: #E8E5E0;
    --input-bg: #FFFFFF;
    --input-border: #D9D4CC;
    --log-bg: #F4F2EE;
    --success: #059669;
    --error: #DC2626;
    --radius: 20px;
    --radius-sm: 8px;
    --font: "AnthropicSerif", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --font-mono: "SF Mono", "Fira Code", "Fira Mono", Menlo, monospace;
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.04);
    --shadow-card: 0 1px 2px rgba(0,0,0,0.03), 0 2px 8px rgba(0,0,0,0.04);
  }

  @font-face {
    font-family: "AnthropicSerif";
    src: url("/font/AnthropicSerif.otf") format("opentype");
    font-weight: 400;
  }

  body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    line-height: 1.65;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }

  .header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
  }
  .header-inner {
    max-width: 1240px;
    margin: 0 auto;
    text-align: center;
  }
  .logo {
    font-size: 33px;
    font-weight: 800;
    color: var(--accent);
    letter-spacing: -0.4px;
  }

  .container {
    max-width: 1240px;
    margin: 0 auto;
    padding: 28px 32px;
    display: grid;
    grid-template-columns: 440px 1fr;
    gap: 24px;
    height: calc(100vh - 73px);
  }

  .left-col {
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-height: 0;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    box-shadow: var(--shadow-card);
  }
  .card-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.3px;
    margin-bottom: 16px;
    text-transform: none;
    text-align: center;
  }

  label {
    text-align: center;
    display: block;
    font-size: 15px;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 16px;
  }
  textarea, input[type="text"], input[type="number"] {
    width: 100%;
    border: 1px solid var(--input-border);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    font-size: 14px;
    font-family: var(--font);
    color: var(--text);
    background: var(--input-bg);
    transition: border-color 0.2s, box-shadow 0.2s;
    outline: none;
  }
  textarea:focus, input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(215,119,87,0.1);
  }
  textarea {
    resize: vertical;
    min-height: 150px;
    font-size: 13px;
    line-height: 1.6;
  }

  .param-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 12px;
  }
  .param-group { margin-bottom: 14px; }
  .param-hint {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 3px;
  }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 10px 20px;
    border-radius: var(--radius-sm);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    transition: background 0.2s, box-shadow 0.2s;
    font-family: var(--font);
  }
  .btn-primary {
    background: var(--accent);
    color: #FFF;
  }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-secondary {
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    box-shadow: var(--shadow-sm);
  }
  .btn-secondary:hover { background: var(--log-bg); }

  .btn-row {
    display: flex;
    gap: 10px;
    align-items: center;
    margin-top: 18px;
    justify-content: center;
  }

  .advanced-toggle {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-secondary);
    background: none;
    border: none;
    cursor: pointer;
    padding: 5px 0;
    margin-bottom: 14px;
    margin-left: auto;
    margin-right: auto;
  }
  .advanced-toggle:hover { color: var(--accent); }
  .advanced-toggle .arrow {
    display: inline-block;
    transition: transform 0.25s;
    font-size: 11px;
  }
  .advanced-toggle.open .arrow { transform: rotate(90deg); }
  .advanced-params { display: none; }
  .advanced-params.open { display: block; }

  /* 日志面板 */
  .log-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .log-area {
    flex: 1;
    background: var(--log-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 10px 14px;
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.75;
    overflow-y: auto;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    word-break: break-word;
    overflow-x: hidden;
    color: var(--text);
    text-align: center;
    min-height: 120px;
  }

  .log-line.status-ok { color: var(--success); }
  .log-line.status-warn { color: var(--accent); }
  .log-line.status-err { color: var(--error); }

  .stat-bar {
    display: flex;
    gap: 18px;
    font-size: 13px;
    color: var(--text-secondary);
    margin-top: 12px;
    flex-shrink: 0;
    justify-content: center;
  }
  .stat-bar strong { color: var(--text); }

  /* 结果表格 */
  .results-panel {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .table-wrapper {
    flex: 1;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
  }
  #results-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    line-height: 1.6;
  }
  #results-table thead {
    position: sticky;
    top: 0;
    z-index: 1;
  }
  #results-table th {
    text-align: center;
    background: var(--log-bg);
    border-bottom: 1.5px solid var(--border);
    padding: 12px 14px;
    font-weight: 600;
    color: var(--text);
    white-space: nowrap;
    font-size: 13px;
  }
  #results-table th .th-label {
    display: block;
    margin-bottom: 5px;
  }
  .copy-col-btn {
    font-size: 12px;
    font-weight: 500;
    color: var(--accent);
    background: none;
    border: 1px solid var(--border);
    cursor: pointer;
    padding: 3px 12px;
    border-radius: var(--radius-sm);
    line-height: 1.5;
    transition: background 0.15s, border-color 0.15s;
  }
  .copy-col-btn:hover {
    background: var(--accent-subtle);
    border-color: var(--accent);
  }
  .copy-col-btn:active { background: rgba(215,119,87,0.12); }
  #results-table td {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
    color: var(--text);
  }
  #results-table td.col-content {
    max-width: 420px;
    padding: 0;
  }
  .cell-scroll {
    max-height: 130px;
    overflow-y: auto;
    padding: 10px 14px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  #results-table td.col-email {
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: 12px;
  }
  #results-table td.col-status {
    white-space: nowrap;
  }
  #results-table tbody tr:nth-child(even) { background: rgba(0,0,0,0.015); }
  #results-table tbody tr:hover { background: var(--accent-subtle); }

  @media (max-width: 900px) {
    .container {
      grid-template-columns: 1fr;
      height: auto;
      min-height: 100vh;
    }
    .results-panel { min-height: 400px; }
    .header { padding: 16px 20px; }
    .container { padding: 20px; }
  }
</style>
</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <div class="logo">Scholarium</div>
  </div>
</div>

<div class="container">
  <!-- 左侧：配置 + 日志 -->
  <div class="left-col">
    <div class="card">
      <div class="param-group">
        <label for="urls">教师主页 URL（每行一个）</label>
        <textarea id="urls" placeholder="https://example.edu.cn/teacher/zhangsan&#10;https://example.edu.cn/teacher/lisi"></textarea>
      </div>

      <button class="advanced-toggle" id="advanced-toggle" onclick="toggleAdvanced()">
        <span class="arrow">▸</span> 高级设置
      </button>

      <div class="advanced-params" id="advanced-params">
      <div class="param-row">
        <div class="param-group">
          <label for="timeout">请求超时（秒）</label>
          <input type="number" id="timeout" value="5" min="1" max="60" step="1">
          <div class="param-hint">默认 5 秒</div>
        </div>
        <div class="param-group">
          <label for="retries">最大重试次数</label>
          <input type="number" id="retries" value="3" min="0" max="10" step="1">
          <div class="param-hint">默认 3 次</div>
        </div>
      </div>

      <div class="param-row">
        <div class="param-group">
          <label for="delay_min">请求间隔下限（秒）</label>
          <input type="number" id="delay_min" value="0.1" min="0" max="10" step="0.1">
          <div class="param-hint">默认 0.1 秒</div>
        </div>
        <div class="param-group">
          <label for="delay_max">请求间隔上限（秒）</label>
          <input type="number" id="delay_max" value="0.5" min="0" max="10" step="0.1">
          <div class="param-hint">默认 0.5 秒</div>
        </div>
      </div>

      <div class="param-row">
        <div class="param-group">
          <label for="min_content_len">最小内容长度（字符）</label>
          <input type="number" id="min_content_len" value="30" min="10" max="500" step="10">
          <div class="param-hint">默认 30 字符</div>
        </div>
        <div class="param-group">
          <label for="output_filename">输出文件名</label>
          <input type="text" id="output_filename" value="teachers.xlsx">
          <div class="param-hint">默认 teachers.xlsx</div>
        </div>
      </div>
      </div>

      <div class="btn-row">
        <button class="btn btn-primary" id="btn-run" onclick="startCrawl()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          开始采集
        </button>
        <button class="btn btn-secondary" id="btn-download" onclick="downloadExcel()" disabled>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          下载 Excel
        </button>
      </div>
    </div>

    <div class="card log-panel">
      <div class="log-area" id="log-area">
      </div>
      <div class="stat-bar" id="stat-bar" style="display:none"></div>
    </div>
  </div>

  <!-- 右侧：结果表格 -->
  <div class="card results-panel" id="results-panel">
    <div class="table-wrapper">
      <table id="results-table">
        <thead>
          <tr>
            <th style="width:55%"><span class="th-label">教师简介</span><button class="copy-col-btn" onclick="copyColumn(0)">复制整列</button></th>
            <th style="width:25%"><span class="th-label">邮箱</span><button class="copy-col-btn" onclick="copyColumn(1)">复制整列</button></th>
            <th style="width:20%"><span class="th-label">采集状态</span><button class="copy-col-btn" onclick="copyColumn(2)">复制整列</button></th>
          </tr>
        </thead>
        <tbody id="results-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const logArea = document.getElementById('log-area');
const btnRun = document.getElementById('btn-run');
const btnDownload = document.getElementById('btn-download');
const statBar = document.getElementById('stat-bar');
const resultsPanel = document.getElementById('results-panel');
const resultsTbody = document.getElementById('results-tbody');

let eventSource = null;
var _allResults = [];

function appendLog(text, cls) {
  const span = document.createElement('span');
  span.textContent = text + '\n';
  if (cls) span.className = 'log-line ' + cls;
  logArea.appendChild(span);
  logArea.scrollTop = logArea.scrollHeight;
}

function clearLog() {
  logArea.innerHTML = '';
}

async function startCrawl() {
  const urls = document.getElementById('urls').value.trim();
  if (!urls) { alert('请输入至少一个 URL'); return; }

  btnRun.disabled = true;
  btnDownload.disabled = true;
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

    var html = '';
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      var statusText = r.status;
      var statusClass = r.status === '链接无效' ? 'color:var(--error)' :
                        r.status === '主页无内容' ? 'color:var(--text-secondary)' :
                        'color:var(--success)';
      html += '<tr>' +
        '<td class="col-content"><div class="cell-scroll">' + escHtml(r.content || '') + '</div></td>' +
        '<td class="col-email">' + escHtml(r.email || '') + '</td>' +
        '<td class="col-status"><span style="' + statusClass + '">' + escHtml(statusText) + '</span></td>' +
        '</tr>';
    }
    resultsTbody.innerHTML = html;

    if (data.output_file) {
      btnDownload.disabled = false;
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
    if (val.indexOf('\n') !== -1 || val.indexOf('\"') !== -1 || val.indexOf(',') !== -1) {
      val = '\"' + val.replace(/\"/g, '\"\"') + '\"';
    }
    values.push(val);
  }
  var text = values.join('\n');
  navigator.clipboard.writeText(text).then(function() {
    // brief flash feedback — find all copy buttons in this column
    var btns = document.querySelectorAll('#results-table th .copy-col-btn');
    var btn = btns[colIdx];
    if (btn) {
      var orig = btn.textContent;
      btn.textContent = '已复制';
      btn.style.color = 'var(--success)';
      setTimeout(function() {
        btn.textContent = orig;
        btn.style.color = '';
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
