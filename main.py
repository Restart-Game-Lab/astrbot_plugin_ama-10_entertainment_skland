"""main.py - AMA-10 Entertainment Skland 插件主文件

命令组 /skland:
  /skland login <手机号>    发送短信验证码, 然后等待用户在 60s 内回复验证码;
                             超时则提示结束流程; 收到验证码走正常登录流程
  /skland logout           清除当前用户全部凭据(释放手机号占用)
  /skland status           查看当前用户凭据/自动签到配置
  /skland checkin          手动签到一次(当前用户全部游戏+论坛)

自动签到:
  配置 auto_checkin_time (HH:MM) 每天自动签到; 每次在目标时刻后随机延迟
  0~random_delay_seconds 秒执行。⚠️ 静默模式: 不推送任何消息, 结果仅记日志。

业务逻辑(签名/登录链路/存储/自动签到调度)见 src/:
  src/client.py  森空岛 API 客户端
  src/storage.py 凭据/占用/推送目标存储
  src/service.py 登录/签到业务服务 + 静默自动签到调度器
"""

import asyncio
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from .src.client import SklandClient
from .src.service import AutoCheckinScheduler, SklandService
from .src.storage import Storage

# 验证码等待超时(秒)
CODE_WAIT_TIMEOUT = 60

# 插件数据目录: <AstrBot>/data/plugin_data/<插件id>/
# ⚠️ 不能在此处(模块导入时)调用 StarTools.get_data_dir() —— 它靠调用栈回溯
#   + star_map 查找调用者模块, 而模块导入阶段本插件尚未注册进 star_map,
#   会抛 "Unable to resolve metadata"。因此延迟到 __init__(实例化、注册完成后)解析。
_PLUGIN_ID = "astrbot_plugin_ama_10_entertainment_skland"


@register(
    "astrbot_plugin_ama_10_entertainment_skland",
    "Restart-Game-Lab",
    "森空岛 (Skland) 验证码登录与多游戏每日签到插件（/skland 命令组）",
    "v0.4.0",
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
        )

        # 自动签到配置
        self._auto_checkin_enabled = bool(self.config.get("auto_checkin_enabled", True))
        self._auto_time = self._parse_auto_time(self.config.get("auto_checkin_time", "08:00"))
        self._random_delay = max(int(self.config.get("random_delay_seconds", 1200)), 0)

        # 自动签到调度器（静默执行, 不推送消息）
        # 总开关 auto_checkin_enabled 关闭 → 不启动; 开启但时间非法 → 不启动
        self.scheduler = AutoCheckinScheduler(
            service=self.service,
            auto_time=self._auto_time if self._auto_checkin_enabled else None,
            random_delay=self._random_delay,
        )
        self.scheduler.start()
        if not self._auto_checkin_enabled:
            logger.info("AMA-10 Skland: 自动签到已关闭 (auto_checkin_enabled=false)")
        elif not self._auto_time:
            logger.info("AMA-10 Skland: 自动签到未启用 (auto_checkin_time 为空)")

    # ---------- 配置 ----------
    @staticmethod
    def _parse_auto_time(raw: str):
        """解析 HH:MM 配置, 非法返回 None"""
        if not raw:
            return None
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw).strip())
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"

    # ---------- 命令组 ----------
    @staticmethod
    def _uid(event: AstrMessageEvent) -> str:
        """用户标识: 用 unified_msg_origin 区分「同一个人的同会话」, 实现单人隔离"""
        return event.unified_msg_origin

    @filter.command_group("skland")
    def skland(self):
        """森空岛登录/签到命令组 /skland（单人指令, 只作用于当前用户）"""
        pass

    @skland.command("login")
    async def skland_login(self, event: AstrMessageEvent, phone: str):
        """/skland login <手机号>: 发送验证码, 60s 内等待用户回复验证码完成登录"""
        if not re.fullmatch(r"1\d{10}", phone):
            yield event.plain_result(f"🔴 手机号 {phone} 格式不正确，应为 11 位国内手机号")
            return

        uid = self._uid(event)

        # 一人一手机号: 当前用户已有账号则禁止再加第二个
        bound_phone = self.storage.get_user_phone(uid)
        if bound_phone and bound_phone != phone:
            yield event.plain_result(
                f"🔴 你已绑定手机号 {self._mask_phone(bound_phone)}\n"
                "每人仅可绑定一个手机号。如需更换请先 /skland logout"
            )
            return

        # 已有凭据直接复用(仅限当前用户, 同手机号)
        cached = self.storage.get_auth(uid, phone)
        if cached:
            yield event.plain_result(
                f"🟡 {self._mask_phone(phone)} 已有保存凭据（保存于 {cached.get('saved_at', '?')}）\n"
                "可直接 /skland checkin 签到；强制重登请先 /skland logout"
            )
            return

        # 手机号占用检查: 若被其他用户占用, 拒绝登录(除非对方 logout 释放)
        if self.storage.is_phone_occupied(phone, by_uid=uid):
            yield event.plain_result(
                f"🔴 手机号 {self._mask_phone(phone)} 已被其他用户占用\n"
                "请等待占用者 /skland logout 解除占用后再尝试登录"
            )
            return

        # ① 发送验证码
        try:
            self.service.send_code(phone)
        except Exception as e:
            yield event.plain_result(f"🔴 验证码发送失败: {e}")
            return
        yield event.plain_result(
            f"🟢 验证码已发送到 {self._mask_phone(phone)}\n"
            f"请 {CODE_WAIT_TIMEOUT} 秒内回复收到的验证码（6 位数字）"
        )

        # ② 等待用户下一条消息(验证码), 超时结束流程
        @session_waiter(timeout=CODE_WAIT_TIMEOUT, record_history_chains=False)
        async def wait_code(controller: SessionController, ev: AstrMessageEvent):
            code = ev.message_str.strip()
            if not (code.isdigit() and len(code) == 6):
                await ev.send(ev.plain_result("🔴 验证码格式不正确，请重新发送 6 位数字验证码"))
                return  # 不 stop, 继续等待(剩余时间)
            try:
                auth = await asyncio.to_thread(self.service.login, phone, code)
            except Exception as e:
                await ev.send(ev.plain_result(
                    f"🔴 登录失败: {e}\n验证码可能错误或已过期，请重新执行 /skland login"
                ))
                controller.stop()
                return
            # 凭据保存到当前用户 (单人隔离)
            self.storage.set_auth(
                uid,
                phone,
                cred=auth["cred"], token=auth["token"],
                login_token=auth["login_token"], device_token=auth["device_token"],
                hg_id=auth["hgId"],
                fingerprint=auth.get("fingerprint"),
            )
            self.storage.save_sub(uid, ev.unified_msg_origin)
            dev = auth.get("device_name") or auth.get("device_model") or ""
            dev_txt = f"📱 设备: {dev}\n" if dev else ""
            await ev.send(ev.plain_result(
                f"🟢 登录成功\n用户: {auth.get('nickName', '(未设置昵称)')}（hgId={auth['hgId']}）\n"
                f"{dev_txt}"
                "凭据已保存，可随时 /skland checkin 签到"
            ))
            controller.stop()

        try:
            await wait_code(event)
        except TimeoutError:
            yield event.plain_result(
                f"🟡 等待验证码超时（{CODE_WAIT_TIMEOUT} 秒），流程已结束\n"
                "如仍需登录请重新执行 /skland login <手机号>"
            )
        except Exception as e:
            yield event.plain_result(f"🔴 登录流程出错: {e}")
        finally:
            event.stop_event()

    @skland.command("logout")
    async def skland_logout(self, event: AstrMessageEvent):
        """/skland logout: 清除当前用户的全部已保存凭据"""
        n = self.storage.clear_user(self._uid(event))
        self.storage.save_sub(self._uid(event), "")
        yield event.plain_result(f"🟢 已清除你的 {n} 个账号凭据")

    @skland.command("status")
    async def skland_status(self, event: AstrMessageEvent):
        """/skland status: 查看当前用户状态"""
        auth = self.storage.load_auth(self._uid(event))
        lines = ["森空岛签到状态"]

        if auth:
            phone, a = next(iter(auth.items()))
            lines.append(f"🟢 已绑定 {self._mask_phone(phone)}（保存于 {a.get('saved_at', '?')}）")
            dev = (a.get("fingerprint") or {}).get("device_name") or a.get("device_model") or "(未知)"
            lines.append(f"📱 设备: {dev}")
        else:
            lines.append("🟡 未绑定手机号")

        game_txt = "开启" if self.service.game_enabled else "关闭"
        forum_txt = "开启" if self.service.forum_enabled else "关闭"
        auto_on = self._auto_checkin_enabled and bool(self._auto_time)
        lines.append(f"自动签到: {'开启 (' + self._auto_time + ')' if auto_on else '关闭'}")
        lines.append(f"随机延迟: {self._random_delay}s")
        lines.append(f"游戏签到: {game_txt}")
        lines.append(f"论坛签到: {forum_txt}")
        yield event.plain_result("\n".join(lines))

    @skland.command("checkin")
    async def skland_checkin(self, event: AstrMessageEvent):
        """/skland checkin: 手动签到一次（当前用户自己的全部游戏+论坛版块）"""
        uid = self._uid(event)
        self.storage.save_sub(uid, event.unified_msg_origin)
        yield event.plain_result(await self.service.checkin_all(uid))

    # ---------- 工具 ----------
    @staticmethod
    def _mask_phone(phone: str) -> str:
        """手机号遮罩 186****0000"""
        return f"{phone[:3]}****{phone[-4:]}"