"""工具模块：日志输出 + 文件锁检测 + 文本辅助函数"""

import os
import platform
import re
import sys
from datetime import datetime
from typing import Optional, TextIO


# ============================================================================
# 日志输出
# ============================================================================

class Logger:
    """轻量日志器，按规范格式输出到终端
    所有 info 级别输出为纯文本，debug 级别仅在启用时输出
    """

    def __init__(self, stream: TextIO = sys.stdout, debug_enabled: bool = False):
        self._stream = stream
        self._debug_enabled = debug_enabled

    def info(self, message: str) -> None:
        self._stream.write(f"{message}\n")
        self._stream.flush()

    def debug(self, message: str) -> None:
        if self._debug_enabled:
            self._stream.write(f"[DEBUG] {message}\n")
            self._stream.flush()


logger = Logger()


# ============================================================================
# 文件锁检测
# ============================================================================

def is_file_locked(filepath: str) -> bool:
    """检测文件是否被其他进程占用（仅 Windows 启用）。

    macOS / Linux 下文件可被多进程同时写入，文件锁检测不适用，
    直接返回 False 跳过检查。
    """
    # 仅在 Windows 上启用文件占用检查
    if platform.system() != "Windows":
        return False

    if not os.path.exists(filepath):
        return False
    try:
        import msvcrt  # type: ignore[import-untyped]
        with open(filepath, "a", buffering=1) as f:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return False
    except (IOError, PermissionError, OSError, BlockingIOError):
        return True


# ======================================
# 文本辅助
# ======================================

def clean_whitespace(text: str) -> str:
    """清理多余空白字符"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()
