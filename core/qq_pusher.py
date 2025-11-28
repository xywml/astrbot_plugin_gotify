"""
QQ推送服务模块

负责将处理后的Gotify消息推送到指定QQ用户。
通过AstrBot的消息系统实现QQ推送。
"""

import asyncio
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from dataclasses import dataclass

try:
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
except ImportError:  # AstrBot运行环境外的兼容处理
    MessageChain = None  # type: ignore
    Plain = None  # type: ignore

from ..config import GotifyConfig
from ..utils.logger import get_logger, LogContext
from ..utils.storage import DataStorage
from ..utils.security import validate_session_id, validate_qq_number, sanitize_message


@dataclass
class PushResult:
    """推送结果"""
    success: bool
    qq_number: str
    message_id: str
    error_message: Optional[str] = None
    retry_count: int = 0
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class PushRetryQueue:
    """推送重试队列"""

    def __init__(self, max_retries: int = 3, retry_delay: float = 5.0):
        """初始化重试队列

        Args:
            max_retries: 最大重试次数
            retry_delay: 重试延迟（秒）
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.queue: List[Dict[str, Any]] = []
        self.logger = get_logger(__name__)

    def add_retry(self, message_data: Dict[str, Any], formatted_message: str, qq_number: str, error: str):
        """添加重试任务"""
        retry_data = {
            'message_data': message_data,
            'formatted_message': formatted_message,
            'qq_number': qq_number,
            'error': error,
            'retry_count': 0,
            'first_attempt': time.time(),
            'last_attempt': time.time()
        }

        self.queue.append(retry_data)
        self.logger.info(f"添加重试任务: QQ={qq_number}, 错误={error}")

    def get_pending_retries(self) -> List[Dict[str, Any]]:
        """获取待重试的任务"""
        current_time = time.time()
        pending_tasks = []

        for task in self.queue.copy():
            # 检查重试次数
            if task['retry_count'] >= self.max_retries:
                self.logger.warning(f"任务达到最大重试次数，移除: QQ={task['qq_number']}")
                self.queue.remove(task)
                continue

            # 检查重试时间
            time_since_last = current_time - task['last_attempt']
            if time_since_last >= self.retry_delay:
                pending_tasks.append(task)
                self.queue.remove(task)

        return pending_tasks

    def size(self) -> int:
        """获取队列大小"""
        return len(self.queue)


class QQPusher:
    """QQ推送服务"""

    def __init__(self, config: GotifyConfig, storage: DataStorage, astrbot_context=None):
        """初始化QQ推送服务

        Args:
            config: Gotify配置
            storage: 数据存储管理器
            astrbot_context: AstrBot上下文，用于发送消息
        """
        self.config = config
        self.storage = storage
        self.astrbot_context = astrbot_context
        self.logger = get_logger(__name__)

        # 验证会话ID
        self.target_users = self._validate_session_ids(config.qq.target_users)

        # 重试队列
        self.retry_queue = PushRetryQueue(max_retries=3, retry_delay=5.0)

        # 统计信息
        self.stats = {
            'messages_sent': 0,
            'messages_failed': 0,
            'total_retries': 0,
            'last_send_time': None,
            'success_rate': 0.0
        }

        # 推送任务
        self._push_tasks: List[asyncio.Task] = []

    def _validate_session_ids(self, session_ids: List[str]) -> List[str]:
        """验证会话ID格式

        AstrBot的send_message方法需要unified_msg_origin格式的session。
        格式为: platform_name:message_type:session_id
        例如: aiocqhttp:GroupMessage:123456789 (QQ群)
              aiocqhttp:FriendMessage:123456789 (QQ私聊)
        """
        valid_sessions = []
        for session_id in session_ids:
            session_id = session_id.strip()
            # 检查是否为完整的UMO格式 (platform:message_type:session_id)
            if ':' in session_id:
                parts = session_id.split(':')
                if len(parts) >= 3:
                    valid_sessions.append(session_id)
                    self.logger.info(f"添加目标会话ID(UMO格式): {session_id}")
                else:
                    self.logger.error(f"无效的UMO格式(需要至少3部分): {session_id}")
            # 兼容旧的纯QQ号格式，给出警告但不再自动添加错误前缀
            elif validate_qq_number(session_id):
                self.logger.error(
                    f"检测到旧版QQ号格式 '{session_id}'，无法主动推送消息。"
                    f"请使用完整的UMO格式，如: aiocqhttp:GroupMessage:{session_id} (群聊) "
                    f"或 aiocqhttp:FriendMessage:{session_id} (私聊)。"
                    f"您可以在群聊或私聊中发送 /sid 命令获取正确的会话ID。"
                )
            elif validate_session_id(session_id):
                # 纯session_id格式（不含平台信息），给出警告
                self.logger.error(
                    f"会话ID '{session_id}' 缺少平台和消息类型信息，无法主动推送消息。"
                    f"请使用完整的UMO格式，如: aiocqhttp:GroupMessage:xxx 或 aiocqhttp:FriendMessage:xxx。"
                    f"您可以在群聊或私聊中发送 /sid 命令获取正确的会话ID。"
                )
            else:
                self.logger.error(f"无效的会话ID格式: {session_id}")

        if not valid_sessions:
            raise ValueError(
                "没有有效的目标会话ID。请使用完整的UMO格式配置target_users，"
                "例如: aiocqhttp:GroupMessage:123456789 或 aiocqhttp:FriendMessage:123456789。"
                "您可以在群聊或私聊中发送 /sid 命令获取正确的会话ID。"
            )

        return valid_sessions

    async def send_message(self, message_data: Dict[str, Any], formatted_message: str) -> List[PushResult]:
        """发送消息到所有目标QQ用户

        Args:
            message_data: 原始消息数据
            formatted_message: 格式化后的消息内容

        Returns:
            List[PushResult]: 推送结果列表
        """
        if not self.target_users:
            self.logger.error("没有配置目标会话ID")
            return []

        results = []
        message_id = str(message_data.get('id', ''))

        with LogContext(self.logger, message_id=message_id):
            self.logger.info(f"开始推送消息到 {len(self.target_users)} 个会话")

            # 并发推送到所有会话
            tasks = []
            for session_id in self.target_users:
                task = asyncio.create_task(
                    self._send_to_user(session_id, message_data, formatted_message)
                )
                tasks.append(task)

            # 等待所有推送完成
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果
            final_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    error_msg = f"推送异常: {str(result)}"
                    self.logger.error(f"推送到会话 {self.target_users[i]} 失败: {error_msg}")
                    final_results.append(PushResult(
                        success=False,
                        qq_number=self.target_users[i],  # 保持字段名兼容性
                        message_id=message_id,
                        error_message=error_msg
                    ))
                elif isinstance(result, PushResult):
                    final_results.append(result)

            # 更新统计信息
            self._update_stats(final_results)

            return final_results

    async def _send_to_user(self, session_id: str, message_data: Dict[str, Any], formatted_message: str) -> PushResult:
        """发送消息到指定会话"""
        message_id = str(message_data.get('id', ''))
        start_time = time.time()

        try:
            with LogContext(self.logger, session_id=session_id):
                self.logger.debug(f"推送到会话: {session_id}")

                # 通过AstrBot发送消息
                success = await self._send_via_astrbot(session_id, formatted_message)

                if success:
                    self.logger.info(f"推送到会话 {session_id} 成功")

                    # 记录成功推送
                    self._record_push_success(message_id, session_id)

                    return PushResult(
                        success=True,
                        qq_number=session_id,  # 保持字段名兼容性
                        message_id=message_id,
                        timestamp=datetime.now().isoformat()
                    )
                else:
                    error_msg = "AstrBot推送失败"
                    self.logger.error(f"推送到会话 {session_id} 失败: {error_msg}")

                    # 添加到重试队列
                    self.retry_queue.add_retry(message_data, formatted_message, session_id, error_msg)

                    return PushResult(
                        success=False,
                        qq_number=session_id,  # 保持字段名兼容性
                        message_id=message_id,
                        error_message=error_msg
                    )

        except Exception as e:
            error_msg = f"推送异常: {str(e)}"
            self.logger.error(f"推送到会话 {session_id} 异常: {e}")

            # 添加到重试队列
            self.retry_queue.add_retry(message_data, formatted_message, session_id, error_msg)

            return PushResult(
                success=False,
                qq_number=session_id,  # 保持字段名兼容性
                message_id=message_id,
                error_message=error_msg
            )

        finally:
            send_time = time.time() - start_time
        self.logger.debug(f"推送耗时: {send_time:.2f}秒")

    async def _send_via_astrbot(self, session_id: str, message: str) -> bool:
        """通过AstrBot发送消息"""
        if not self.astrbot_context:
            self.logger.error("AstrBot上下文未初始化")
            return False

        if not hasattr(self.astrbot_context, 'send_message'):
            self.logger.error("AstrBot上下文缺少send_message方法")
            return False

        message_chain = self._build_message_chain(message)

        # 直接使用提供的会话ID，不再构造候选列表
        try:
            result = await self.astrbot_context.send_message(session_id, message_chain)
            if result is False:
                self.logger.warning(f"send_message返回False，session={session_id}")
                return False
            return True
        except TypeError as type_err:
            # 兼容旧版本只接收字符串消息的接口
            try:
                result = await self.astrbot_context.send_message(session_id, sanitize_message(message))
                if result is False:
                    self.logger.warning(f"send_message返回False（字符串模式），session={session_id}")
                    return False
                self.logger.warning("send_message使用字符串参数兼容模式")
                return True
            except Exception as type_fallback_err:
                self.logger.error(f"兼容发送失败: session={session_id}, 错误={type_fallback_err}")
                return False
        except Exception as e:
            self.logger.error(f"通过AstrBot发送消息失败: session={session_id}, 错误={e}")
            return False

    def _record_push_success(self, message_id: str, session_id: str):
        """记录成功的推送"""
        try:
            # 这里可以添加到数据库或其他存储中
            # 目前只记录日志
            self.logger.info(f"记录成功推送: 消息ID={message_id}, 会话={session_id}")
        except Exception as e:
            self.logger.error(f"记录推送成功失败: {e}")

    def _build_message_chain(self, message: str) -> Any:
        """根据格式化文本构建MessageChain"""
        safe_text = sanitize_message(message or "")
        if MessageChain and Plain:
            component = Plain(text=safe_text)
            return MessageChain([component])
        return safe_text

    
    def _update_stats(self, results: List[PushResult]):
        """更新统计信息"""
        success_count = sum(1 for r in results if r.success)
        failed_count = len(results) - success_count

        self.stats['messages_sent'] += success_count
        self.stats['messages_failed'] += failed_count
        self.stats['last_send_time'] = datetime.now().isoformat()

        total = self.stats['messages_sent'] + self.stats['messages_failed']
        if total > 0:
            self.stats['success_rate'] = self.stats['messages_sent'] / total * 100

        self.logger.info(f"推送统计: 成功={success_count}, 失败={failed_count}, 成功率={self.stats['success_rate']:.1f}%")

    async def process_retry_queue(self):
        """处理重试队列"""
        if self.retry_queue.size() == 0:
            return

        self.logger.info(f"处理重试队列，待重试任务数: {self.retry_queue.size()}")

        retry_tasks = self.retry_queue.get_pending_retries()

        for task in retry_tasks:
            message_data = task['message_data']
            formatted_message = task['formatted_message']
            session_id = task['qq_number']  # 字段名保持兼容性
            retry_count = task['retry_count']

            self.logger.info(f"重试推送: 会话={session_id}, 重试次数={retry_count}")

            result = await self._send_to_user(session_id, message_data, formatted_message)

            if not result.success:
                # 如果重试仍然失败，重新加入队列
                task['retry_count'] += 1
                task['last_attempt'] = time.time()
                self.retry_queue.queue.append(task)
                self.stats['total_retries'] += 1

    async def start_retry_processor(self):
        """启动重试处理器"""
        async def retry_loop():
            while True:
                try:
                    await self.process_retry_queue()
                    await asyncio.sleep(10)  # 每10秒检查一次重试队列
                except Exception as e:
                    self.logger.error(f"重试处理器异常: {e}")
                    await asyncio.sleep(30)  # 异常时等待更长时间

        task = asyncio.create_task(retry_loop())
        self._push_tasks.append(task)
        self.logger.info("重试处理器已启动")

    async def stop_retry_processor(self):
        """停止重试处理器"""
        for task in self._push_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._push_tasks.clear()
        self.logger.info("重试处理器已停止")

    def get_status(self) -> Dict[str, Any]:
        """获取推送服务状态"""
        return {
            'target_users': self.target_users,
            'stats': self.stats.copy(),
            'retry_queue_size': self.retry_queue.size(),
            'active_tasks': len(self._push_tasks)
        }

    def get_retry_queue_status(self) -> Dict[str, Any]:
        """获取重试队列状态"""
        return {
            'size': self.retry_queue.size(),
            'max_retries': self.retry_queue.max_retries,
            'retry_delay': self.retry_queue.retry_delay
        }
