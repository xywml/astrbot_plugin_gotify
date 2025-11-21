"""
消息处理器模块

负责处理从Gotify接收到的消息，包括过滤、格式化、去重等功能。
"""

import asyncio
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from collections import deque

from ..config import GotifyConfig
from ..utils.logger import get_logger
from ..utils.storage import MessageHistory
from ..utils.security import sanitize_message


class MessageBuffer:
    """消息缓冲器，支持批量处理"""

    def __init__(self, batch_size: int = 10, flush_interval: int = 5):
        """初始化消息缓冲器

        Args:
            batch_size: 批量处理大小
            flush_interval: 刷新间隔（秒）
        """
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.buffer: deque = deque()
        self.last_flush_time = time.time()
        self.logger = get_logger(__name__)

    def add_message(self, message_data: Dict[str, Any]) -> bool:
        """添加消息到缓冲器"""
        self.buffer.append(message_data)
        self.logger.debug(f"消息已添加到缓冲器，当前缓冲区大小: {len(self.buffer)}")

        # 检查是否需要立即刷新
        if len(self.buffer) >= self.batch_size:
            return True  # 需要立即刷新

        # 检查时间间隔
        if time.time() - self.last_flush_time >= self.flush_interval:
            return True  # 需要立即刷新

        return False  # 不需要立即刷新

    def get_messages(self) -> List[Dict[str, Any]]:
        """获取缓冲器中的所有消息"""
        messages = list(self.buffer)
        self.buffer.clear()
        self.last_flush_time = time.time()
        return messages

    def size(self) -> int:
        """获取缓冲区大小"""
        return len(self.buffer)


class MessageFilter:
    """消息过滤器"""

    def __init__(self, config: GotifyConfig):
        """初始化消息过滤器

        Args:
            config: Gotify配置
        """
        self.min_priority = config.message.filters.min_priority
        self.blocked_app_ids = set(config.message.filters.blocked_app_ids)
        self.blocked_titles = config.message.filters.blocked_titles
        self.logger = get_logger(__name__)

    def should_process(self, message_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """判断消息是否应该被处理

        Returns:
            tuple[bool, Optional[str]]: (是否应该处理, 拒绝原因)
        """
        # 检查优先级
        priority = message_data.get('priority', 5)
        if priority < self.min_priority:
            return False, f"优先级过低: {priority} < {self.min_priority}"

        # 检查应用ID
        appid = message_data.get('appid')
        if appid and appid in self.blocked_app_ids:
            return False, f"应用ID被阻止: {appid}"

        # 检查标题
        title = message_data.get('title', '')
        for blocked_title in self.blocked_titles:
            if blocked_title and blocked_title.lower() in title.lower():
                return False, f"标题包含被阻止的关键词: {blocked_title}"

        return True, None


class MessageFormatter:
    """消息格式化器"""

    def __init__(self, config: GotifyConfig):
        """初始化消息格式化器

        Args:
            config: Gotify配置
        """
        self.format_config = config.qq.message_format
        self.logger = get_logger(__name__)

    def format_message(self, message_data: Dict[str, Any]) -> str:
        """格式化消息内容"""
        try:
            # 获取消息组件
            title = message_data.get('title', '')
            message = message_data.get('message', '')
            priority = message_data.get('priority', 5)
            created_at = message_data.get('created_at')

            # 清理消息内容
            title = sanitize_message(title)
            message = sanitize_message(message)

            # 构建格式化消息
            parts = []

            # 添加优先级图标
            if self.format_config.include_priority:
                priority_icon = self._get_priority_icon(priority)
                parts.append(f"{priority_icon}")

            # 添加标题
            if title and self.format_config.include_title:
                parts.append(f"📌 {title}")

            # 添加消息内容
            if message:
                parts.append(f"💬 {message}")

            # 添加时间戳
            if created_at and self.format_config.include_timestamp:
                try:
                    # 解析时间戳
                    if isinstance(created_at, str):
                        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        time_str = dt.strftime('%m-%d %H:%M')
                        parts.append(f"🕐 {time_str}")
                except Exception:
                    # 时间戳解析失败时使用原始值
                    parts.append(f"🕐 {created_at}")

            # 合并消息
            formatted_message = '\n'.join(parts)

            # 限制消息长度
            max_length = self.format_config.max_message_length
            if len(formatted_message) > max_length:
                formatted_message = formatted_message[:max_length - 3] + "..."

            return formatted_message

        except Exception as e:
            self.logger.error(f"格式化消息失败: {e}")
            # 返回基本格式
            return f"Gotify消息:\n{sanitize_message(message_data.get('message', '无内容'))}"

    def _get_priority_icon(self, priority: int) -> str:
        """获取优先级图标"""
        if priority >= 9:
            return "🔴🔥"  # 紧急
        elif priority >= 7:
            return "🟠"   # 高
        elif priority >= 5:
            return "🟡"   # 中等
        elif priority >= 3:
            return "🔵"   # 低
        else:
            return "⚪"   # 最低


class MessageHandler:
    """消息处理器"""

    def __init__(self, config: GotifyConfig, message_history: MessageHistory):
        """初始化消息处理器

        Args:
            config: Gotify配置
            message_history: 消息历史管理器
        """
        self.config = config
        self.message_history = message_history
        self.logger = get_logger(__name__)

        # 初始化组件
        self.filter = MessageFilter(config)
        self.formatter = MessageFormatter(config)

        # 初始化缓冲器
        if config.message.buffer.enabled:
            self.buffer = MessageBuffer(
                batch_size=config.message.buffer.batch_size,
                flush_interval=config.message.buffer.flush_interval
            )
        else:
            self.buffer = None

        # 统计信息
        self.stats = {
            'messages_received': 0,
            'messages_filtered': 0,
            'messages_processed': 0,
            'messages_buffered': 0,
            'last_process_time': None
        }

        # 处理回调
        self.on_processed_callback: Optional[callable] = None

    def set_processed_callback(self, callback: callable):
        """设置消息处理完成回调"""
        self.on_processed_callback = callback

    async def process_message(self, message_data: Dict[str, Any]) -> bool:
        """处理单个消息

        Args:
            message_data: 消息数据

        Returns:
            bool: 消息是否被处理
        """
        try:
            self.stats['messages_received'] += 1

            with self.logger.bind(
                message_id=message_data.get('id'),
                title=message_data.get('title', 'N/A')
            ):
                self.logger.info("开始处理消息")

                # 检查消息去重
                if self.config.message.deduplication.enabled:
                    if self.message_history.is_duplicate(
                        message_data,
                        self.config.message.deduplication.window_seconds
                    ):
                        self.logger.info("消息重复，跳过处理")
                        self.stats['messages_filtered'] += 1
                        return False

                # 应用过滤器
                should_process, reason = self.filter.should_process(message_data)
                if not should_process:
                    self.logger.info(f"消息被过滤: {reason}")
                    self.stats['messages_filtered'] += 1
                    return False

                # 检查是否需要缓冲
                if self.buffer:
                    return await self._handle_buffered_message(message_data)
                else:
                    return await self._process_single_message(message_data)

        except Exception as e:
            self.logger.error(f"处理消息异常: {e}")
            return False

    async def _handle_buffered_message(self, message_data: Dict[str, Any]) -> bool:
        """处理缓冲消息"""
        try:
            # 添加到缓冲器
            should_flush = self.buffer.add_message(message_data)
            self.stats['messages_buffered'] += 1

            # 检查是否需要刷新缓冲器
            if should_flush:
                await self._flush_buffer()

            return True

        except Exception as e:
            self.logger.error(f"处理缓冲消息异常: {e}")
            return False

    async def _process_single_message(self, message_data: Dict[str, Any]) -> bool:
        """处理单个消息（不缓冲）"""
        try:
            # 格式化消息
            formatted_message = self.formatter.format_message(message_data)

            # 添加到历史记录
            self.message_history.add_message(message_data)

            # 调用处理回调
            if self.on_processed_callback:
                await self._safe_callback_call(
                    self.on_processed_callback,
                    message_data,
                    formatted_message
                )

            # 更新统计
            self.stats['messages_processed'] += 1
            self.stats['last_process_time'] = datetime.now().isoformat()

            self.logger.info("消息处理完成")
            return True

        except Exception as e:
            self.logger.error(f"处理单个消息异常: {e}")
            return False

    async def _flush_buffer(self):
        """刷新消息缓冲器"""
        if not self.buffer or self.buffer.size() == 0:
            return

        try:
            messages = self.buffer.get_messages()
            self.logger.info(f"刷新消息缓冲器，处理 {len(messages)} 条消息")

            for message_data in messages:
                await self._process_single_message(message_data)

        except Exception as e:
            self.logger.error(f"刷新缓冲器异常: {e}")

    async def flush_buffer(self):
        """手动刷新消息缓冲器"""
        await self._flush_buffer()

    async def _safe_callback_call(self, callback, *args, **kwargs):
        """安全调用回调函数"""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(*args, **kwargs)
            else:
                callback(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"回调函数执行异常: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """获取处理统计信息"""
        buffer_info = {}
        if self.buffer:
            buffer_info = {
                'buffer_size': self.buffer.size(),
                'batch_size': self.buffer.batch_size,
                'flush_interval': self.buffer.flush_interval
            }

        return {
            **self.stats,
            'buffer_info': buffer_info,
            'filter_config': {
                'min_priority': self.filter.min_priority,
                'blocked_app_ids': list(self.filter.blocked_app_ids),
                'blocked_titles': self.filter.blocked_titles
            }
        }

    def get_buffer_status(self) -> Dict[str, Any]:
        """获取缓冲器状态"""
        if not self.buffer:
            return {"enabled": False}

        return {
            "enabled": True,
            "size": self.buffer.size(),
            "batch_size": self.buffer.batch_size,
            "flush_interval": self.buffer.flush_interval,
            "last_flush_time": self.buffer.last_flush_time
        }