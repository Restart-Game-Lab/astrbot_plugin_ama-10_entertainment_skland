"""main.py - AMA-10 Entertainment Skland 插件主文件 (v0.5.1)

命令组 /skland:
  /skland login <手机号>    发送短信验证码, 然后等待用户在 60s 内回复验证码;
                             超时则提示结束流程; 收到验证码走正常登录流程
  /skland logout           清除当前用户全部凭据(释放手机号占用)
  /skland status           查看当前用户凭据/自动签到配置
  /skland status_all       管理员查看全部用户登录状态总览
  /skland checkin          手动签到一次(当前用户全部游戏+论坛)
  /skland checkin_all      管理员为全部已登录用户批量签到(展示汇总)

自动签到:
  配置 auto_checkin_time (cron 表达式, 默认 "0 6 * * *" = 每天 6:00) 定时自动签到;
  每次在目标时刻后随机延迟 0~random_delay_seconds 秒执行。⚠️ 静默模式: 不推送任何消息, 结果仅记日志。

业务逻辑(签名/登录链路/存储/自动签到调度)见 src/:
  src/client.py  森空岛 API 客户端 (Web 版签名 platform=3, 设备中心 dId)
  src/did.py     官方设备中心 dId 生成 (DES/AES/RSA 指纹上报)
  src/ua_pool.py 真实浏览器 UA 池 (data/browsers.jsonl)
  src/storage.py 凭据/占用/推送目标存储
  src/service.py 登录/签到业务服务 + 静默自动签到调度器
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, StarTools, register

from .src.api.client import SklandClient
from .src.utils.recall import RecallManager
from .src.services.service import AutoCheckinScheduler, SklandService
from .src.storage.storage import Storage

# 验证码等待超时(秒)
CODE_WAIT_TIMEOUT = 60


@dataclass
class _CodeSession:
    """一次验证码等待会话(按用户隔离)"""

    uid: str
    phone: str
    event: Any = None  # 发起事件的引用(仅超时提示时 send 用)
    timeout_task: asyncio.Task | None = None  # 超时任务(取消用)


# 全局会话表: session_key -> 验证码等待会话。
# 每个用户同时只会有一个登录流程, 新流程会挤掉旧流程。
# session_key = 平台id:发送者id(参考 shitu 插件), 见 _session_key()。
_CODE_SESSIONS: dict[str, _CodeSession] = {}

# 插件数据目录: <AstrBot>/data/plugin_data/<插件id>/
# ⚠️ 不能在此处(模块导入时)调用 StarTools.get_data_dir() —— 它靠调用栈回溯
#   + star_map 查找调用者模块, 而模块导入阶段本插件尚未注册进 star_map,
#   会抛 "Unable to resolve metadata"。因此延迟到 __init__(实例化、注册完成后)解析。
_PLUGIN_ID = "astrbot_plugin_ama_10_entertainment_skland"


@register(
    "astrbot_plugin_ama_10_entertainment_skland",
    "Restart-Game-Lab",
    "森空岛 (Skland) 验证码登录与多游戏每日签到插件（/skland 命令组）",
    "v0.5.1",
    "https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland",
)
class Main(Star):
    """森空岛登录 + 签到插件主类（只负责命令编排, 业务在 src/）"""

    def __init__(self, context: Context, config: dict[str, Any] = None):
        super().__init__(context)
        self.config = config or {}

        # 插件数据目录(注册完成后解析, 显式传插件名避免依赖调用栈/star_map)
        self.data_dir = StarTools.get_data_dir(_PLUGIN_ID)
        self.storage = Storage(self.data_dir)
        self.service = SklandService(
            self.storage,
            timeout=float(self.config.get("request_timeout", 15)),
            game_enabled=bool(self.config.get("game_checkin_enabled", True)),
            forum_enabled=bool(self.config.get("forum_checkin_enabled", True)),
            ua_pool_enabled=bool(self.config.get("ua_pool_enabled", True)),
        )

        # 自动签到配置
        self._auto_checkin_enabled = bool(self.config.get("auto_checkin_enabled", True))
        self._auto_cron = self._parse_auto_cron(self.config.get("auto_checkin_time", "0 6 * * *"))
        self._random_delay = max(int(self.config.get("random_delay_seconds", 1200)), 0)

        # 自动撤回配置（消息显示几秒后自动撤回 + 阻断 LLM 链路）
        scopes_raw = str(self.config.get("auto_recall_scopes", "all") or "all")
        scopes = {s.strip().lower() for s in scopes_raw.split(",") if s.strip().lower()}
        self.recall = RecallManager(
            enabled=bool(self.config.get("auto_recall_enabled", False)),
            delay=float(self.config.get("auto_recall_delay_seconds", 10)),
            scopes=scopes or {"all"},
        )

        # 自动签到调度器（静默执行, 不推送消息）
        # 总开关 auto_checkin_enabled 关闭 → 不启动; 开启但时间非法 → 不启动
        # ⚠️ 启动移到 initialize()（插件激活钩子）: __init__ 阶段保证有事件循环,
        #    但 APScheduler 的 AsyncIOScheduler 在 initialize() 中启动更可靠
        self.scheduler = AutoCheckinScheduler(
            service=self.service,
            auto_cron=self._auto_cron if self._auto_checkin_enabled else None,
            random_delay=self._random_delay,
        )

    async def initialize(self):
        """插件激活时调用: 启动自动签到调度"""
        self.scheduler.start()
        if not self._auto_checkin_enabled:
            logger.info("AMA-10 Skland: 自动签到已关闭 (auto_checkin_enabled=false)")
        elif not self._auto_cron:
            logger.info("AMA-10 Skland: 自动签到未启用 (auto_checkin_time cron 非法)")

    async def terminate(self):
        """插件卸载/重载时调用: 停止自动签到调度, 避免残留任务重复触发"""
        self.scheduler.stop()

    # ---------- 发送 + 自动撤回 + 阻断 LLM ----------
    async def _send_and_recall(self, event, text: str, scope: str = "all"):
        """统一的插件消息发送入口。

        行为:
          1. 主动发送消息（直接调协议端 API 拿到 message_id, 用于后续撤回）
          2. call_llm=True 阻断 LLM 链路（无条件, 防止插件回复被 AI 接手）
          3. 按配置延迟 N 秒后自动撤回（仅 aiocqhttp 平台, 失败仅日志）
          4. 发送后将事件标记为已发送（_has_send_oper), 双保险阻断 LLM

        返回: 是否成功发送。发送不依赖 yield, 命令 handler 可直接 await。
        """
        try:
            # 直接调协议端 API 主动发送（拿到 message_id, 用于撤回）
            mid = await self._send_raw(event, text)
        except Exception as e:
            logger.warning(f"AMA-10 Skland: 主动发送失败, 回退 event.send: {e}")
            await event.send(event.plain_result(text))
            event.call_llm = True  # 回退路径也阻断 LLM（_has_send_oper 已由 send 设置）
            return False

        event.call_llm = True  # 无条件阻断 LLM 链路
        if mid:
            self.recall.schedule_recall(getattr(event, "bot", None), mid, scope)
        return True

    async def _send_raw(self, event, text: str):
        """绕开框架直接调 OneBot 协议端发送, 返回 message_id（或 None）。

        仅 aiocqhttp 平台有效; 非该平台返回 None（由调用方回退 event.send）。
        """
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except Exception:
            return None
        if not isinstance(event, AiocqhttpMessageEvent):
            return None
        bot = event.bot
        session_id = event.get_group_id() or event.get_sender_id()
        if event.get_group_id():
            ret = await bot.call_action(
                "send_group_msg",
                group_id=int(session_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
        else:
            ret = await bot.call_action(
                "send_private_msg",
                user_id=int(session_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
        return ret.get("message_id") if ret else None

    def _stop_and_block_llm(self, event):
        """阻断 LLM + 终止事件传播（对不回消息路径也用）"""
        event.call_llm = True
        event.stop_event()

    # ---------- 配置 ----------
    @staticmethod
    def _parse_auto_cron(raw: str):
        """解析 cron 表达式配置, 非法返回 None。

        标准 5 段 cron (分 时 日 月 周), 如 "0 6 * * *" = 每天 06:00。
        """
        if not raw:
            return None
        expr = str(raw).strip()
        if not expr:
            return None
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(expr)
            return expr
        except Exception:
            return None

    # ---------- 用户身份 key ----------
    @staticmethod
    def _uid(event: AstrMessageEvent) -> str:
        """用户标识(数据隔离 key): 平台id + 发送者id。

        ⚠️ 不能用 unified_msg_origin: 群消息里整个群共享同一个值,
        它只能区分「群/私聊会话」, 不能区分人。若以它为 key 存取凭据,
        群里任何人都会共享/操作同一份凭据(查/签/清互串)。
        用发送者 id: 私有互不干扰, 群内也按人隔离。
        (与验证码等待会话 key 同构, 见 _session_key)
        """
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    # ---------- 验证码等待(自实现, 替代 session_waiter) ----------
    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        """验证码等待会话的隔离 key, 与 _uid() 同构(平台id + 发送者id)。"""
        return Main._uid(event)

    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=100000001,
    )
    async def _on_message(self, event: AstrMessageEvent) -> None:
        """监听所有消息, 喂给验证码等待会话(仅发起者本人)。

        不使用框架的 session_waiter: 它会注册一个“全局过滤器”, 由内置
        astrbot star 对每条消息(含机器人自己发的)调 filter() 匹配, 同群消息
        会被反复命中, 造成验证码错误时重复回复“格式不正确”。

        命中会话 == 该事件被本插件消费: 必须 call_llm=True 阻断 LLM 链路
        (数字消息不再触发 AI 回复), 并 stop_event() 终止事件传播。
        """
        session = _CODE_SESSIONS.get(self._session_key(event))
        if not session:
            return

        # 命中会话: 拦截该事件(阻断 LLM + 终止传播), 防止数字被 AI 接手
        event.call_llm = True

        # 只有流程发起者本人后续发的消息才会被处理, 其他用户消息直接忽略
        msg = event.message_str.strip()
        if not (msg.isdigit() and len(msg) == 6):
            # 不合法验证码: 静默忽略(不回复不提示), 等 60s 超时自动结束流程
            event.stop_event()
            return
        code = msg

        try:
            auth = await self.service.login(session.phone, code)
        except Exception as e:
            await self._send_and_recall(
                event,
                f"🔴 登录失败: {e}\n验证码可能错误或已过期，请重新执行 /skland login",
                scope="login",
            )
            self._end_session(session)
            event.stop_event()
            return

        # 凭据保存到当前用户 (单人隔离)
        self.storage.set_auth(
            session.uid,
            session.phone,
            cred=auth["cred"], token=auth["token"],
            login_token=auth["login_token"], device_token=auth["device_token"],
            hg_id=auth["hgId"],
            fingerprint=auth.get("fingerprint"),
            nick_name=auth.get("nickName", ""),
            did=auth.get("did", ""),
            ua_used=auth.get("ua_used", ""),
        )
        self.storage.save_sub(session.uid, self._uid(event))  # 推送目标按用户 key 存
        nick = auth.get("nickName", "(未设置昵称)")
        await self._send_and_recall(
            event,
            f"🟢 登录成功\n"
            f"[登录用户]: {nick} | {session.phone[:3]}****{session.phone[-4:]} (hgId={auth['hgId']})\n"
            "凭据已保存，可随时 /skland checkin 签到",
            scope="login",
        )
        self._end_session(session)
        event.stop_event()  # 验证码已被消费, 事件不再向下传播

    @filter.command_group("skland")
    def skland(self):
        """森空岛登录/签到命令组 /skland（单人指令, 只作用于当前用户）"""
        pass

    @skland.command("login")
    async def skland_login(self, event: AstrMessageEvent, phone: str):
        """/skland login <手机号>: 发送验证码, 60s 内等待用户回复验证码完成登录"""
        if not re.fullmatch(r"1\d{10}", phone):
            await self._send_and_recall(
                event,
                f"🔴 手机号 {phone} 格式不正确，应为 11 位国内手机号",
                scope="login",
            )
            self._stop_and_block_llm(event)
            return

        uid = self._uid(event)

        # 一人一手机号: 当前用户已有账号则禁止再加第二个
        bound_phone = self.storage.get_user_phone(uid)
        if bound_phone and bound_phone != phone:
            await self._send_and_recall(
                event,
                f"🔴 你已绑定手机号 {self._mask_phone(bound_phone)}\n"
                "每人仅可绑定一个手机号。如需更换请先 /skland logout",
                scope="login",
            )
            self._stop_and_block_llm(event)
            return

        # 已有凭据直接复用(仅限当前用户, 同手机号)
        cached = self.storage.get_auth(uid, phone)
        if cached:
            await self._send_and_recall(
                event,
                f"🟡 {self._mask_phone(phone)} 已有保存凭据（保存于 {cached.get('saved_at', '?')}）\n"
                "可直接 /skland checkin 签到；强制重登请先 /skland logout",
                scope="login",
            )
            self._stop_and_block_llm(event)
            return

        # 手机号占用检查: 若被其他用户占用, 拒绝登录(除非对方 logout 释放)
        if self.storage.is_phone_occupied(phone, by_uid=uid):
            await self._send_and_recall(
                event,
                f"🔴 手机号 {self._mask_phone(phone)} 已被其他用户占用\n"
                "请等待占用者 /skland logout 解除占用后再尝试登录",
                scope="login",
            )
            self._stop_and_block_llm(event)
            return

        # ① 发送验证码
        try:
            await self.service.send_code(phone)
        except Exception as e:
            await self._send_and_recall(
                event,
                f"🔴 验证码发送失败: {e}",
                scope="login",
            )
            self._stop_and_block_llm(event)
            return
        await self._send_and_recall(
            event,
            f"🟢 验证码已发送到 {self._mask_phone(phone)}\n"
            f"请 {CODE_WAIT_TIMEOUT} 秒内回复收到的验证码（6 位数字）",
            scope="login",
        )

        # ② 注册验证码等待会话(参考 shitu 插件的 waiting_sessions):
        #    命令立即返回, 后续所有消费/回复/超时都在 _on_message 或超时任务中完成。
        #    会话 key 按「平台id+发送者id」隔离, 只有发起者本人消息才会被消费;
        #    并在 _on_message 中 call_llm=True + stop_event() 阻断 LLM 与传播。
        session = _CodeSession(uid=uid, phone=phone, event=event)
        key = self._session_key(event)
        self._register_session(session, key)
        # 命令已注册等待会话, 本事件不再向下传播(避免被其他插件/AI 接手)
        self._stop_and_block_llm(event)

    def _register_session(self, session: _CodeSession, key: str):
        """注册等待会话, 并启动超时任务"""
        # 同一用户重复发起会挤掉旧流程
        old = _CODE_SESSIONS.get(key)
        if old and old.timeout_task and not old.timeout_task.done():
            old.timeout_task.cancel()
        _CODE_SESSIONS[key] = session
        session.timeout_task = asyncio.create_task(
            self._timeout_check(key, session)
        )

    def _end_session(self, session: _CodeSession):
        """结束会话: 取消超时任务 + 从会话表移除"""
        if session.timeout_task and not session.timeout_task.done():
            session.timeout_task.cancel()
        key = next(
            (k for k, s in _CODE_SESSIONS.items() if s is session),
            None,
        )
        if key:
            _CODE_SESSIONS.pop(key, None)

    async def _timeout_check(self, key: str, session: _CodeSession):
        """超时任务: 等待超时后提示并结束会话"""
        try:
            await asyncio.sleep(CODE_WAIT_TIMEOUT)
            session_now = _CODE_SESSIONS.get(key)
            if session_now is session:
                self._end_session(session)
                try:
                    await self._send_and_recall(
                        session.event,
                        f"🟡 等待验证码超时（{CODE_WAIT_TIMEOUT} 秒），流程已结束\n"
                        "如仍需登录请重新执行 /skland login <手机号>",
                        scope="login",
                    )
                    self._stop_and_block_llm(session.event)
                except Exception as e:
                    logger.warning(f"AMA-10 Skland: 超时提示发送失败: {e}")
        except asyncio.CancelledError:
            pass

    @skland.command("logout")
    async def skland_logout(self, event: AstrMessageEvent):
        """/skland logout: 清除当前用户的全部已保存凭据"""
        n = self.storage.clear_user(self._uid(event))
        self.storage.save_sub(self._uid(event), "")
        await self._send_and_recall(event, f"🟢 已清除你的 {n} 个账号凭据", scope="logout")
        self._stop_and_block_llm(event)

    @skland.command("status")
    async def skland_status(self, event: AstrMessageEvent):
        """/skland status: 查看当前用户状态（实际请求 user/me 验证登录态 + 获取最新昵称）"""
        auth = self.storage.load_auth(self._uid(event))

        if auth:
            phone, a = next(iter(auth.items()))
            nick = a.get("nickName") or "(未设置昵称)"
            lines = [f"🟢 已绑定 {self._mask_phone(phone)}"]

            # 实际请求端点验证登录态 + 刷新昵称
            try:
                result = await self.service.check_auth(a)
                if result.get("ok"):
                    nick = result.get("nickname") or nick
                    lines.append(f"[登录用户]: {nick} | {self._mask_phone(phone)}")
                    lines.append(f"[登录态]: 🟢 有效")
                else:
                    lines.append(f"[登录用户]: {nick} | {self._mask_phone(phone)}")
                    lines.append(f"[登录态]: 🔴 已失效（{result.get('error', '未知错误')}）")
                    lines.append("  请 /skland logout 后重新登录")
            except Exception as e:
                # 请求本身异常不阻塞展示
                lines.append(f"[登录用户]: {nick} | {self._mask_phone(phone)}")
                lines.append(f"[登录态]: ⚠️ 检查失败（{e}）")
        else:
            lines = ["🟡 未绑定手机号"]

        game_txt = "开启" if self.service.game_enabled else "关闭"
        forum_txt = "开启" if self.service.forum_enabled else "关闭"
        auto_on = self._auto_checkin_enabled and bool(self._auto_cron)
        lines.append("[签到配置]:")
        lines.append(f"自动签到: {'开启 (' + self._auto_cron + ')' if auto_on else '关闭'}")
        lines.append(f"随机延迟: {self._random_delay}s")
        lines.append(f"游戏签到: {game_txt}")
        lines.append(f"论坛签到: {forum_txt}")
        await self._send_and_recall(event, "\n".join(lines), scope="status")
        self._stop_and_block_llm(event)

    @permission_type(PermissionType.ADMIN)
    @skland.command("status_all")
    async def skland_status_all(self, event: AstrMessageEvent):
        """/skland status_all: 管理员查看全部用户登录状态总览

        并发验证所有已登录账号的登录态, 每行三色球 + 森空岛昵称 + 掩码手机号,
        底部汇总三色计数。非管理员无权调用(permission_type 拦截)。
        """
        all_auth = self.storage.load_auth()
        if not all_auth:
            await self._send_and_recall(
                event,
                "登录总览（共 0 个用户）\n\n🟡 暂无用户登录",
                scope="status",
            )
            self._stop_and_block_llm(event)
            return

        # 展开为 (uid, phone, auth) 列表, 并发验证登录态 (check_auth 内部不抛异常)
        entries = [
            (uid, phone, a)
            for uid, auths in all_auth.items()
            for phone, a in auths.items()
        ]
        results = await asyncio.gather(
            *(self.service.check_auth(a) for _, _, a in entries),
            return_exceptions=True,
        )

        ok_n = err_n = warn_n = 0
        lines = [f"登录总览（共 {len(entries)} 个账号）", ""]
        for (_, phone, a), res in zip(entries, results):
            nick = a.get("nickName") or "(未设置昵称)"
            phone_m = self._mask_phone(phone)
            if isinstance(res, Exception):
                mark, suffix = "🟡", "（检查失败）"
                warn_n += 1
            elif res.get("ok"):
                nick = res.get("nickname") or nick  # 实时刷新昵称
                mark, suffix = "🟢", ""
                ok_n += 1
            else:
                mark, suffix = "🔴", "（已失效）"
                err_n += 1
            lines.append(f"{mark} {nick} | {phone_m}{suffix}")

        lines.append("")
        lines.append(f"[汇总]: 🟢 {ok_n} · 🔴 {err_n} · 🟡 {warn_n}")
        await self._send_and_recall(event, "\n".join(lines), scope="status")
        self._stop_and_block_llm(event)

    @permission_type(PermissionType.ADMIN)
    @skland.command("checkin_all")
    async def skland_checkin_all(self, event: AstrMessageEvent):
        """/skland checkin_all: 管理员为全部已登录用户批量签到并展示汇总

        与个人 /skland checkin 不同: 遍历所有已登录账号逐个签到,
        每账号一行主状态(球 + 森空岛昵称 + 掩码手机号), 失败/异常展开细节,
        底部三色汇总计数, 风格与 /skland status_all 一致。
        非管理员无权调用(permission_type 拦截)。
        """
        await self._send_and_recall(
            event,
            await self.service.checkin_all_admin(),
            scope="checkin",
        )
        self._stop_and_block_llm(event)

    @skland.command("checkin")
    async def skland_checkin(self, event: AstrMessageEvent):
        """/skland checkin: 手动签到一次（当前用户自己的全部游戏+论坛版块）"""
        uid = self._uid(event)
        self.storage.save_sub(uid, self._uid(event))  # 推送目标按用户 key 存(与凭据 key 一致)
        await self._send_and_recall(event, await self.service.checkin_all(uid), scope="checkin")
        self._stop_and_block_llm(event)

    # ---------- 工具 ----------
    @staticmethod
    def _mask_phone(phone: str) -> str:
        """手机号遮罩 186****0000"""
        return f"{phone[:3]}****{phone[-4:]}"