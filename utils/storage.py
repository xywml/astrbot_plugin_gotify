"""
数据存储模块

提供消息历史记录、配置缓存等数据持久化功能。
"""

import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from contextlib import contextmanager
from filelock import FileLock
import time

from .logger import get_logger
from .security import generate_message_id


class DataStorage:
    """数据存储管理器"""

    def __init__(self, data_dir: str = "./data"):
        """初始化数据存储

        Args:
            data_dir: 数据存储目录
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger(__name__)
        self.lock = FileLock(self.data_dir / "storage.lock", timeout=10)

        # 初始化数据库
        self.db_path = self.data_dir / "gotify_plugin.db"
        self._init_database()

    def _init_database(self):
        """初始化SQLite数据库"""
        with self.lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    # 消息历史表
                    conn.execute('''
                        CREATE TABLE IF NOT EXISTS message_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            message_id TEXT UNIQUE NOT NULL,
                            gotify_id INTEGER NOT NULL,
                            title TEXT,
                            message TEXT NOT NULL,
                            priority INTEGER,
                            appid INTEGER,
                            created_at TIMESTAMP,
                            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            processed BOOLEAN DEFAULT FALSE,
                            qq_sent BOOLEAN DEFAULT FALSE,
                            qq_sent_at TIMESTAMP,
                            error_message TEXT,
                            retry_count INTEGER DEFAULT 0
                        )
                    ''')

                    # 连接状态表
                    conn.execute('''
                        CREATE TABLE IF NOT EXISTS connection_status (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            status TEXT NOT NULL,
                            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            error_message TEXT,
                            extra_data TEXT
                        )
                    ''')

                    # 统计信息表
                    conn.execute('''
                        CREATE TABLE IF NOT EXISTS statistics (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            date DATE UNIQUE NOT NULL,
                            messages_received INTEGER DEFAULT 0,
                            messages_sent INTEGER DEFAULT 0,
                            errors INTEGER DEFAULT 0,
                            uptime_seconds INTEGER DEFAULT 0
                        )
                    ''')

                    # 创建索引
                    conn.execute('CREATE INDEX IF NOT EXISTS idx_message_gotify_id ON message_history(gotify_id)')
                    conn.execute('CREATE INDEX IF NOT EXISTS idx_message_created_at ON message_history(created_at)')
                    conn.execute('CREATE INDEX IF NOT EXISTS idx_connection_timestamp ON connection_status(timestamp)')
                    conn.execute('CREATE INDEX IF NOT EXISTS idx_stats_date ON statistics(date)')

                    conn.commit()
                    self.logger.info("数据库初始化完成")

            except sqlite3.Error as e:
                self.logger.error(f"数据库初始化失败: {e}")
                raise

    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                conn.row_factory = sqlite3.Row  # 启用字典式访问
                yield conn
            except sqlite3.Error as e:
                self.logger.error(f"数据库连接错误: {e}")
                raise
            finally:
                if 'conn' in locals():
                    conn.close()

    def save_message(self, message_data: Dict[str, Any]) -> bool:
        """保存消息到数据库"""
        try:
            message_id = generate_message_id(message_data)

            with self.get_connection() as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO message_history
                    (message_id, gotify_id, title, message, priority, appid, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    message_id,
                    message_data['id'],
                    message_data.get('title', ''),
                    message_data['message'],
                    message_data.get('priority', 5),
                    message_data.get('appid'),
                    message_data.get('created_at')
                ))
                conn.commit()

            self.logger.debug(f"消息已保存到数据库: {message_id}")
            return True

        except sqlite3.Error as e:
            self.logger.error(f"保存消息失败: {e}")
            return False

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """根据消息ID获取消息"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    'SELECT * FROM message_history WHERE message_id = ?',
                    (message_id,)
                )
                row = cursor.fetchone()

                if row:
                    return dict(row)
                return None

        except sqlite3.Error as e:
            self.logger.error(f"获取消息失败: {e}")
            return None

    def get_unprocessed_messages(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取未处理的消息"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute('''
                    SELECT * FROM message_history
                    WHERE processed = FALSE
                    ORDER BY gotify_id ASC
                    LIMIT ?
                ''', (limit,))
                return [dict(row) for row in cursor.fetchall()]

        except sqlite3.Error as e:
            self.logger.error(f"获取未处理消息失败: {e}")
            return []

    def mark_message_processed(self, message_id: str, success: bool = True, error_message: Optional[str] = None):
        """标记消息为已处理"""
        try:
            with self.get_connection() as conn:
                conn.execute('''
                    UPDATE message_history
                    SET processed = TRUE, error_message = ?, qq_sent = ?
                    WHERE message_id = ?
                ''', (error_message, success, message_id))
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"标记消息处理状态失败: {e}")

    def save_connection_status(self, status: str, error_message: Optional[str] = None, extra_data: Optional[Dict[str, Any]] = None):
        """保存连接状态"""
        try:
            extra_json = json.dumps(extra_data) if extra_data else None

            with self.get_connection() as conn:
                conn.execute('''
                    INSERT INTO connection_status (status, error_message, extra_data)
                    VALUES (?, ?, ?)
                ''', (status, error_message, extra_json))
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"保存连接状态失败: {e}")

    def get_latest_connection_status(self) -> Optional[Dict[str, Any]]:
        """获取最新的连接状态"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute('''
                    SELECT * FROM connection_status
                    ORDER BY timestamp DESC
                    LIMIT 1
                ''')
                row = cursor.fetchone()

                if row:
                    result = dict(row)
                    if result['extra_data']:
                        result['extra_data'] = json.loads(result['extra_data'])
                    return result
                return None

        except sqlite3.Error as e:
            self.logger.error(f"获取连接状态失败: {e}")
            return None

    def update_statistics(self, date: Optional[datetime] = None, messages_received: int = 0,
                         messages_sent: int = 0, errors: int = 0, uptime_seconds: int = 0):
        """更新统计信息"""
        if date is None:
            date = datetime.now().date()

        try:
            with self.get_connection() as conn:
                conn.execute('''
                    INSERT INTO statistics (date, messages_received, messages_sent, errors, uptime_seconds)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        messages_received = messages_received + excluded.messages_received,
                        messages_sent = messages_sent + excluded.messages_sent,
                        errors = errors + excluded.errors,
                        uptime_seconds = uptime_seconds + excluded.uptime_seconds
                ''', (date, messages_received, messages_sent, errors, uptime_seconds))
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"更新统计信息失败: {e}")

    def get_statistics(self, days: int = 7) -> List[Dict[str, Any]]:
        """获取统计信息"""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute('''
                    SELECT * FROM statistics
                    WHERE date >= date('now', '-{} days')
                    ORDER BY date DESC
                '''.format(days))
                return [dict(row) for row in cursor.fetchall()]

        except sqlite3.Error as e:
            self.logger.error(f"获取统计信息失败: {e}")
            return []

    def cleanup_old_messages(self, days: int = 30) -> int:
        """清理旧消息"""
        try:
            cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

            with self.get_connection() as conn:
                cursor = conn.execute('''
                    DELETE FROM message_history
                    WHERE created_at < ? AND processed = TRUE
                ''', (cutoff_date,))
                deleted_count = cursor.rowcount
                conn.commit()

            self.logger.info(f"清理了 {deleted_count} 条旧消息")
            return deleted_count

        except sqlite3.Error as e:
            self.logger.error(f"清理旧消息失败: {e}")
            return 0


class MessageHistory:
    """消息历史管理器"""

    def __init__(self, storage: DataStorage):
        """初始化消息历史管理器"""
        self.storage = storage
        self.logger = get_logger(__name__)
        self._dedup_cache = {}
        self._cache_lock = threading.Lock()

    def is_duplicate(self, message_data: Dict[str, Any], window_seconds: int = 60) -> bool:
        """检查消息是否重复"""
        message_id = generate_message_id(message_data)
        current_time = time.time()

        with self._cache_lock:
            # 检查缓存中的重复消息
            if message_id in self._dedup_cache:
                last_time = self._dedup_cache[message_id]
                if current_time - last_time < window_seconds:
                    return True
                else:
                    # 超过时间窗口，从缓存中移除
                    del self._dedup_cache[message_id]

            # 添加到缓存
            self._dedup_cache[message_id] = current_time

        # 检查数据库中的重复消息
        existing_message = self.storage.get_message(message_id)
        return existing_message is not None

    def add_message(self, message_data: Dict[str, Any]) -> bool:
        """添加消息到历史记录"""
        if self.is_duplicate(message_data):
            self.logger.debug(f"检测到重复消息，跳过: {message_data.get('id')}")
            return False

        return self.storage.save_message(message_data)

    def get_pending_messages(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取待处理的消息"""
        return self.storage.get_unprocessed_messages(limit)

    def mark_sent(self, message_id: str, success: bool = True, error_message: Optional[str] = None):
        """标记消息发送状态"""
        self.storage.mark_message_processed(message_id, success, error_message)

    def get_recent_messages(self, hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近的消息"""
        try:
            cutoff_time = (datetime.now() - timedelta(hours=hours)).isoformat()

            with self.storage.get_connection() as conn:
                cursor = conn.execute('''
                    SELECT * FROM message_history
                    WHERE created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT ?
                ''', (cutoff_time, limit))
                return [dict(row) for row in cursor.fetchall()]

        except sqlite3.Error as e:
            self.logger.error(f"获取最近消息失败: {e}")
            return []