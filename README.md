# Scholarium —— 高校教师主页信息采集工具

批量采集高校教师个人主页中的**教师简介**和**联系邮箱**，输出为Excel文件。
支持终端模式和 Web UI 模式两种运行方式。

## 功能特性
- **邮箱提取**：支持 TSites 加密字段解密、Cloudflare Email Protection 解密、倒序邮箱还原、mailto 链接、中文混淆还原、联系我们页面反查、PDF 附件提取、JS 变量提取等
- **简介提取**：优先 trafilatura 提取，回退自定义 DOM 文本密度算法 + BeautifulSoup 清洗，自动检测并跳过导航/公告等边角内容
- **动态页面支持**：调用教师主页后端 API 获取动态加载的简介
- **PDF 内容提取**：自动检测页面中的 PDF 附件，下载并提取文本与邮箱
- **HTML 深度清洗**：移除导航栏、页脚、侧边栏、面包屑、脚本、样式等无关内容
- **输出 Excel**：结果写入 `.xlsx` 文件，包含简介、邮箱、采集状态三列
- **Web UI**：浏览器图形界面，实时查看采集日志，结果表格支持按列复制

## 环境要求

Python 3.8+

## 安装
### 创建虚拟环境（推荐）
```
python3 -m venv venv
source venv/bin/activate  # macOS / Linux
venv\Scripts\activate   # Windows
```
### 安装依赖
```
pip install -r requirements.txt
```

## 运行方式

### 方式一：Web UI（推荐）

启动后自动打开浏览器，在图形界面中配置参数并实时查看采集进度：

```bash
python Main_WebUI.py
```

界面功能：

- **左侧**：URL 输入框 + 可折叠的高级设置（超时、重试、间隔等参数），下方为实时滚动日志
- **右侧**：采集结果表格，每列表头带有"复制整列"按钮，简介内容超过 6 行自动出现滚动条
- 采集完成后自动显示统计数据，可一键下载 Excel 文件

### 方式二：终端模式

编辑 `Main_Terminal.py`，在 `URLS` 变量中填入待采集的教师主页 URL：

```python
URLS = """
https://example.edu.cn/teacher/zhangsan
https://example.edu.cn/teacher/lisi
"""
```

```bash
python Main_Terminal.py
```

采集完成后结果保存在 `teachers.xlsx`。

## 配置参数

终端模式下在 `Main_Terminal.py` 中修改，Web UI 模式下在页面左侧面板直接填写：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `REQUEST_TIMEOUT` | `5` | HTTP 请求超时（秒） |
| `MAX_RETRIES` | `3` | 最大重试次数 |
| `RANDOM_DELAY_MIN` | `0.1` | 请求间隔随机范围下限（秒） |
| `RANDOM_DELAY_MAX` | `0.5` | 请求间隔随机范围上限（秒） |
| `OUTPUT_FILENAME` | `"teachers.xlsx"` | 输出文件名 |
| `MIN_CONTENT_LENGTH` | `30` | 有效内容最小长度（字符） |
| `MAX_CONTENT_LENGTH` | `1500` | 简介文本最大长度（写入 Excel 前截断） |


## 输出格式

Excel 文件包含三列：

| 教师简介 | 邮箱 | 采集状态 |
|----------|------|----------|
| 教师简介文本（最多1500字） | 提取到的邮箱 或 "网页无邮箱" | 有内容 / 主页无内容 / 链接无效 |

## 依赖说明

- `requests` —— HTTP 请求
- `beautifulsoup4` + `lxml` —— HTML 解析
- `trafilatura` —— 正文提取
- `openpyxl` —— Excel 读写
- `pdfplumber` —— PDF 文本提取
- `flask` —— Web 服务器（仅 Web UI 模式需要）
- `certifi` —— SSL 证书验证

## License
本项目仅供学习与研究使用。请仅用于合法用途，遵守目标网站的 robots.txt 和使用条款。本项目仅采集教师在主页公开的信息，不会采集非公开信息。


## Scholarium 浏览器插件
本项目还有一个Edge/Chrome浏览器插件版本，更加便携，没有PDF解析功能，详情可跳转`https://github.com/JaronLouis6226/Scholarium`。
