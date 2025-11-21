"""
日志工具模块

提供结构化日志记录功能，支持JSON和文本格式。
"""

import sys
import logging
import logging.handlers
from pathlib import Path
from typing import Optional, Dict, Any
import structlog


class JSONFormatter(logging.Formatter):
    """JSON格式化器"""

    def format(self, record):
        """格式化日志记录为JSON"""
        log_entry = {
            'timestamp': self.formatTime(record),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno
        }

        # 添加异常信息
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)

        # 添加额外字段
        if hasattr(record, 'extra_fields'):
            log_entry.update(record.extra_fields)

        import json
        return json.dumps(log_entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """文本格式化器"""

    def __init__(self):
        fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        super().__init__(fmt, datefmt='%Y-%m-%d %H:%M:%S')


def setup_logging(
    level: str = "INFO",
    log_format: str = "json",
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5
) -> None:
    """设置日志配置"""

    # 获取根logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # 清除现有处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    if log_format.lower() == 'json':
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(TextFormatter())
    console_handler.setLevel(getattr(logging, level.upper()))
    root_logger.addHandler(console_handler)

    # 文件处理器（如果指定了日志文件）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        if log_format.lower() == 'json':
            file_handler.setFormatter(JSONFormatter())
        else:
            file_handler.setFormatter(TextFormatter())
        file_handler.setLevel(getattr(logging, level.upper()))
        root_logger.addHandler(file_handler)


def get_logger(name: str, extra_fields: Optional[Dict[str, Any]] = None) -> logging.Logger:
    """获取logger实例"""
    logger = logging.getLogger(name)

    # 如果有额外字段，创建适配器
    if extra_fields:
        logger = logging.LoggerAdapter(logger, extra_fields)

    return logger


class LogContext:
    """日志上下文管理器，用于临时添加额外字段"""

    def __init__(self, logger: logging.Logger, **extra_fields):
        self.logger = logger
        self.extra_fields = extra_fields
        self.adapter = None

    def __enter__(self):
        if isinstance(self.logger, logging.LoggerAdapter):
            # 如果已经是适配器，合并字段
            merged_fields = {**self.logger.extra, **self.extra_fields}
            self.adapter = logging.LoggerAdapter(self.logger.logger, merged_fields)
        else:
            self.adapter = logging.LoggerAdapter(self.logger, self.extra_fields)
        return self.adapter

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass