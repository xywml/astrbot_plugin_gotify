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
from ..utils.security import validate_qq_number, sanitize_message


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

        # 验证QQ号
        self.target_users = self._validate_qq_numbers(config.qq.target_users)

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

    def _validate_qq_numbers(self, qq_numbers: List[str]) -> List[str]:
        """验证QQ号格式"""
        valid_qq = []
        for qq in qq_numbers:
            if validate_qq_number(qq):
                valid_qq.append(qq)
                self.logger.info(f"添加目标QQ号: {qq}")
            else:
                self.logger.warning(f"无效的QQ号格式: {qq}")

        if not valid_qq:
            raise ValueError("没有有效的目标QQ号")

        return valid_qq

    async def send_message(self, message_data: Dict[str, Any], formatted_message: str) -> List[PushResult]:
        """发送消息到所有目标QQ用户

        Args:
            message_data: 原始消息数据
            formatted_message: 格式化后的消息内容

        Returns:
            List[PushResult]: 推送结果列表
        """
        if not self.target_users:
            self.logger.error("没有配置目标QQ号")
            return []

        results = []
        message_id = str(message_data.get('id', ''))

        with LogContext(self.logger, message_id=message_id):
            self.logger.info(f"开始推送消息到 {len(self.target_users)} 个QQ用户")

            # 并发推送到所有用户
            tasks = []
            for qq_number in self.target_users:
                task = asyncio.create_task(
                    self._send_to_user(qq_number, message_data, formatted_message)
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
                    self.logger.error(f"推送到QQ {self.target_users[i]} 失败: {error_msg}")
                    final_results.append(PushResult(
                        success=False,
                        qq_number=self.target_users[i],
                        message_id=message_id,
                        error_message=error_msg
                    ))
                elif isinstance(result, PushResult):
                    final_results.append(result)

            # 更新统计信息
            self._update_stats(final_results)

            return final_results

    async def _send_to_user(self, qq_number: str, message_data: Dict[str, Any], formatted_message: str) -> PushResult:
        """发送消息到指定QQ用户"""
        message_id = str(message_data.get('id', ''))
        start_time = time.time()

        try:
            with LogContext(self.logger, qq_number=qq_number):
                self.logger.debug(f"推送到QQ用户: {qq_number}")

                # 通过AstrBot发送消息
                success = await self._send_via_astrbot(qq_number, formatted_message)

                if success:
                    self.logger.info(f"推送到QQ {qq_number} 成功")

                    # 记录成功推送
                    self._record_push_success(message_id, qq_number)

                    return PushResult(
                        success=True,
                        qq_number=qq_number,
                        message_id=message_id,
                        timestamp=datetime.now().isoformat()
                    )
                else:
                    error_msg = "AstrBot推送失败"
                    self.logger.error(f"推送到QQ {qq_number} 失败: {error_msg}")

                    # 添加到重试队列
                    self.retry_queue.add_retry(message_data, formatted_message, qq_number, error_msg)

                    return PushResult(
                        success=False,
                        qq_number=qq_number,
                        message_id=message_id,
                        error_message=error_msg
                    )

        except Exception as e:
            error_msg = f"推送异常: {str(e)}"
            self.logger.error(f"推送到QQ {qq_number} 异常: {e}")

            # 添加到重试队列
            self.retry_queue.add_retry(message_data, formatted_message, qq_number, error_msg)

            return PushResult(
                success=False,
                qq_number=qq_number,
                message_id=message_id,
                error_message=error_msg
            )

        finally:
            send_time = time.time() - start_time
        self.logger.debug(f"推送耗时: {send_time:.2f}秒")

    async def _send_via_astrbot(self, qq_number: str, message: str) -> bool:
        """通过AstrBot发送消息"""
        if not self.astrbot_context:
            self.logger.error("AstrBot上下文未初始化")
            return False

        if not hasattr(self.astrbot_context, 'send_message'):
            self.logger.error("AstrBot上下文缺少send_message方法")
            return False

        message_chain = self._build_message_chain(message)
        session_candidates = self._build_session_candidates(qq_number)

        last_error: Optional[Exception] = None
        for session_id in session_candidates:
            try:
                result = await self.astrbot_context.send_message(session_id, message_chain)
                if result is False:
                    self.logger.warning(f"send_message返回False，session={session_id}")
                    continue
                return True
            except TypeError as type_err:
                # 兼容旧版本只接收字符串消息的接口
                try:
                    result = await self.astrbot_context.send_message(session_id, sanitize_message(message))
                    if result is False:
                        continue
                    self.logger.warning("send_message使用字符串参数兼容模式")
                    return True
                except Exception as type_fallback_err:
                    last_error = type_fallback_err
                    self.logger.error(f"兼容发送失败: session={session_id}, 错误={type_fallback_err}")
                    continue
            except Exception as e:
                last_error = e
                self.logger.error(f"通过AstrBot发送消息失败: session={session_id}, 错误={e}")

        if last_error:
            self.logger.error(f"所有session发送失败，最后错误: {last_error}")
        return False

    def _build_session_candidates(self, qq_number: str) -> List[str]:
        """构造可能的会话ID列表"""
        normalized = qq_number.strip()
        candidates = []

        if not normalized:
            return candidates

        candidates.append(normalized)

        if ':' not in normalized:
            candidates.append(f"qq:{normalized}")
            candidates.append(f"private:qq:{normalized}")

        # 去重但保持顺序
        seen = set()
        ordered = []
        for session in candidates:
            if session not in seen:
                seen.add(session)
                ordered.append(session)
        return ordered

    def _build_message_chain(self, message: str) -> Any:
        """根据格式化文本构建MessageChain"""
        safe_text = sanitize_message(message or "")
        if MessageChain and Plain:
            component = Plain(text=safe_text)
            return MessageChain([component])
        return safe_text

    def _record_push_success(self, message_id: str, qq_number: str):
        """记录成功的推送"""
        try:
            # 这里可以添加到数据库或其他存储中
            # 目前只记录日志
            self.logger.info(f"记录成功推送: 消息ID={message_id}, QQ={qq_number}")
        except Exception as e:
            self.logger.error(f"记录推送成功失败: {e}")

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
            qq_number = task['qq_number']
            retry_count = task['retry_count']

            self.logger.info(f"重试推送: QQ={qq_number}, 重试次数={retry_count}")

            result = await self._send_to_user(qq_number, message_data, formatted_message)

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
