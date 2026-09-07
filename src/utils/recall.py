"""src/recall.py - 消息自动撤回 + LLM 阻断辅助

目标（用户要求）:
  1. 插件发出的消息按配置自动撤回（默认关闭, 延迟几秒后删除）
  2. 所有插件回复必须阻断 LLM 链路（call_llm=True）

实现要点（AstrBot v4 源码 ref 6b84809 核实）:
  - ProcessStage LLM 判定:
        not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm
    → 设置 call_llm=True 可无条件阻断 LLM（公开 API, 最可靠）
  - event.send() / yield plain_result() 拿不到 message_id（框架丢弃响应）
    → 直接调 event.bot.call_action("send_group_msg"/"send_private_msg", ...) 主动发送,
      协议端响应 data.message_id 即发送结果, 可用于 delete_msg 撤回
  - 仅 aiocqhttp(OneBot v11) 平台支持 callback: 判断 event.get_platform_name()
  - 撤回: call_action("delete_msg", message_id=...), 失败仅记日志不中断流程
"""

import asyncio
import logging

logger = logging.getLogger("ama10_skland")

# 日志标签
_TAG = "AMA-10 Skland"


class RecallManager:
    """管理自动撤回: 记录待撤回消息, 延迟 N 秒后调用 delete_msg。

    仅当 enabled=True 且 scope 命中时才调度撤回;
    撤回失败（协议端不支持/消息已不存在）仅记日志, 不影响主流程。
    """

    def __init__(self, enabled: bool = False, delay: float = 10.0,
                 scopes: set[str] | None = None):
        self.enabled = enabled
        self.delay = max(float(delay), 0.0)
        # scope 集合: {"all"} 表示全部; 或 {"login", "checkin", ...} 子集
        self.scopes = scopes or {"all"}
        self._tasks: set[asyncio.Task] = set()

    def _should_recall(self, scope: str) -> bool:
        """判断某 scope 的消息是否需要自动撤回"""
        if not self.enabled:
            return False
        return "all" in self.scopes or scope in self.scopes

    async def _delete_later(self, bot, message_id) -> None:
        """延迟后撤回（独立任务, 失败仅日志）"""
        try:
            await asyncio.sleep(self.delay)
            await bot.call_action("delete_msg", message_id=message_id)
            logger.info(f"{_TAG}: 已撤回消息 {message_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"{_TAG}: 撤回消息 {message_id} 失败: {e}")

    def schedule_recall(self, bot, message_id, scope: str = "all") -> None:
        """调度一条消息的自动撤回"""
        if not self._should_recall(scope):
            return
        task = asyncio.create_task(self._delete_later(bot, message_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel_all(self) -> None:
        """取消所有待撤回任务（插件卸载时调用）"""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
