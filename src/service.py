"""src/service.py - 认证/签到业务服务

封装与 AstrBot 无关的纯业务逻辑:
  - login(phone, code): 验证码完成登录, 返回凭据字典
  - do_checkin(phone, auth): 对单个账号签到所有绑定游戏
  - checkin_all(phone): 签到指定(或全部)账号, 返回展示文本
  - 自动签到调度(auto_checkin_loop): 每日定时 + 随机延迟
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .client import SklandClient, dict_to_fingerprint, fingerprint_to_dict
from .storage import Storage

logger = logging.getLogger("ama10_skland")


class SklandService:
    """森空岛业务服务（依赖注入 Storage 与请求超时, 便于测试）"""

    def __init__(self, storage: Storage, timeout: float = 15.0,
                 game_enabled: bool = True, forum_enabled: bool = True):
        self.storage = storage
        self.timeout = timeout
        self.game_enabled = game_enabled
        self.forum_enabled = forum_enabled

    # ---------- 登录 ----------
    def login(self, phone: str, code: str) -> dict:
        """执行完整验证码登录链路, 返回凭据字典(已由调用方负责保存)。

        抛出 RuntimeError 表示登录失败。
        """
        client = SklandClient(timeout=self.timeout)
        token, hg_id, device_token = client.token_by_phone_code(phone, code)
        try:
            u = client.basic_info(token)
        except Exception:
            u = {}
        auth_code = client.grant(token, device_token)
        cred, cred_token = client.generate_cred(auth_code)

        # 森空岛真实用户名: 优先 user/me(App 同款, 带签名), 失败回退 basic_info
        nick = ""
        try:
            me = client.user_me(cred, cred_token)
            nick = (me.get("user") or {}).get("nickname") or ""
        except Exception:
            logger.debug("AMA-10 Skland: user/me 获取昵称失败, 回退 basic_info")
        if not nick:
            nick = u.get("nickName") or ""

        return {
            "cred": cred,
            "token": cred_token,
            "login_token": token,
            "device_token": device_token,
            "hgId": hg_id,
            "nickName": nick or "(未设置昵称)",
            # 登录使用的完整设备指纹(持久化复用)
            "fingerprint": fingerprint_to_dict(client.fp),
            "device_model": client.fp["device_model"],
            "device_brand": client.fp.get("brand", ""),
            "device_name": client.fp.get("device_name", ""),
        }

    def send_code(self, phone: str):
        """发送短信验证码"""
        client = SklandClient(timeout=self.timeout)
        client.send_phone_code(phone)

    # ---------- 签到 ----------
    @staticmethod
    def _mask_phone(phone: str) -> str:
        """手机号遮罩 186****0000"""
        return f"{phone[:3]}****{phone[-4:]}"

    def do_checkin(self, phone: str, auth: dict) -> dict:
        """对单个账号执行签到，返回分组行列表 + 论坛球串。

        返回结构:
          game_ok / game_err     : 游戏签到行(直接带球符)
          forum_row / forum_msg  : 论坛一行球串 / 附加说明(失败原因等)
        已签到视为成功(不算失败)。

        设备指纹: 优先复用 auth["fingerprint"] (登录时存档, 保持恒定防风控);
        存量账号无指纹时回退为新生成, 调用方负责写回。
        """
        fp = auth.get("fingerprint") or {}
        client = SklandClient(timeout=self.timeout,
                              fp=dict_to_fingerprint(fp) if fp else None)
        cred, cred_token = auth["cred"], auth["token"]

        groups = {"game_ok": [], "game_err": [],
                  "forum_row": "", "forum_err": [],
                  "fp": fingerprint_to_dict(client.fp)}  # 本次实际使用的设备指纹

        # ---- 游戏签到（受 game_enabled 开关控制）----
        if self.game_enabled:
            bindings = client.get_binding_list(cred, cred_token)
            for b in bindings:
                name = f"{b.get('gameName')} | {b.get('nickName')}"
                try:
                    client.sign_attendance(cred, cred_token, b["gameId"], b, b.get("uid"))
                    groups["game_ok"].append(f"🟢 {name}")
                except Exception as e:
                    groups["game_err"].append(f"🔴 {name} {e}")

        # ---- 论坛签到（受 forum_enabled 开关控制）----
        if self.forum_enabled:
            try:
                # 先查状态: checked=True → 🟢; 未签则逐一签, 成功 🟢 / 失败 🔴
                status_list = client.forum_checkin_status(cred, cred_token)
                balls = []
                for item in status_list:
                    gid = item.get("gameId")
                    name = SklandClient.FORUM_GAME_NAMES.get(gid, f"版块{gid}")
                    if item.get("checked"):
                        balls.append("🟢")
                        continue
                    try:
                        client.forum_checkin(cred, cred_token, gid)
                        balls.append("🟢")
                    except Exception as e:
                        balls.append("🔴")
                        groups["forum_err"].append(f"{name}: {e}")
                groups["forum_row"] = " ".join(balls) if balls else "（无版块）"
            except Exception as e:
                groups["forum_row"] = "🔴"
                groups["forum_err"].append(f"查询失败: {e}")

        return groups

    async def checkin_all(self, uid: str) -> str:
        """签到指定用户(单人)的全部账号，返回给用户/日志的摘要文本。

        Args:
            uid: 用户标识(平台id:发送者id)。只签该用户自己的账号, 不影响他人。
        """
        auth = self.storage.load_auth(uid)
        if not auth:
            return "🟡 你尚未登录任何账号\n请使用 /skland login <手机号> 完成登录"

        lines = []
        for p, a in auth.items():
            # 设备行(取自 auth 存指纹, 与请求一致; 老账号无指纹显示未知)
            dev = a.get("fingerprint") or {}
            dev_txt = f"{dev.get('device_brand') or ''} | {dev.get('device_name') or ''}"
            dev_txt = dev_txt.strip(" |") or "(未知)"
            lines.append(f"[登录设备]: {dev_txt}")

            try:
                g = await asyncio.to_thread(self.do_checkin, p, a)
            except Exception as e:
                if any(k in str(e).lower() for k in ("cred", "401", "token", "未授权")):
                    lines.append(f"🟡 {self._mask_phone(p)} 凭据失效，请重新登录")
                    lines.append(f"   /skland login {self._mask_phone(p)}")
                else:
                    lines.append(f"🔴 {self._mask_phone(p)} 签到失败: {e}")
                continue

            # 存量账号无指纹: 签到成功后用本次实际指纹补写回凭据(之后恒定)
            if not a.get("fingerprint") and g.get("fp"):
                a["fingerprint"] = g["fp"]
                self.storage.set_auth(
                    uid, p,
                    cred=a["cred"], token=a["token"],
                    login_token=a.get("login_token", ""),
                    device_token=a.get("device_token", ""),
                    hg_id=a.get("hgId", ""),
                    fingerprint=g["fp"],
                    nick_name=a.get("nickName", ""),
                )

            # 分组输出: [游戏签到] / [论坛签到] 段落 + 失败说明
            # 标题按全局结果: 全成功=🟢签到完成 / 全失败=🔴签到失败 / 部分失败=🟡签到完成异常
            game_rows = g.get("game_ok", []) or ["🟢 (无绑定游戏)"]
            game_rows += g.get("game_err", [])
            game_ok_n = len(g.get("game_ok", []))
            game_err_n = len(g.get("game_err", []))
            forum_balls = (g.get("forum_row") or "").split()
            forum_ok_n = forum_balls.count("🟢")
            forum_err_n = len(g.get("forum_err", []))
            fail_n = game_err_n + forum_err_n

            if fail_n == 0:
                head = "🟢 签到完成"
            elif (game_ok_n + forum_ok_n) == 0:
                head = "🔴 签到失败"
            else:
                head = "🟡 签到完成异常"

            lines.append(head)
            lines.append(f"[游戏签到]:")
            lines.extend(game_rows)
            if g.get("forum_row"):
                lines.append(f"[论坛签到]:")
                lines.append(g["forum_row"])
            lines.extend(f"   🔴 {err}" for err in g.get("forum_err", []))

        return "\n".join(lines)

    async def checkin_all_users(self) -> None:
        """自动签到(静默): 遍历所有已登录用户, 各自签到自己的账号。

        不推送、不返回内容, 仅记录日志。
        """
        all_auth = self.storage.load_auth()
        for uid in all_auth:
            try:
                summary = await self.checkin_all(uid)
                logger.info(f"AMA-10 Skland: 用户 {uid} 自动签到:\n{summary}")
            except Exception as e:
                logger.error(f"AMA-10 Skland: 用户 {uid} 自动签到失败: {e}")


# ============================ 自动签到 ============================
class AutoCheckinScheduler:
    """每日自动签到调度器（基于 APScheduler AsyncIOScheduler）。

    参考 Azincc/astrbot_plugin_skland 的定时方案:
      - AsyncIOScheduler + CronTrigger(hour, minute): 到点触发
      - misfire_grace_time: 错过触发时间(如休眠/重启)仍补执行
      - 随机延迟 0~random_delay 秒 + 按日期去重, 避免重复/固定时刻风控
    ⚠️ 静默模式: 不推送任何消息, 结果仅记录日志。

    生命周期:
      - start(): 启动调度器并注册每日任务（建议在 Star.initialize() 中调用）
      - stop(): 停止调度器并取消任务（建议在 Star.terminate() 中调用）
    """

    def __init__(self, service: SklandService,
                 auto_time: str | None,
                 random_delay: int = 0):
        self.service = service
        self.auto_time = auto_time  # "HH:MM" 或 None(关闭)
        self.random_delay = max(int(random_delay), 0)
        self._last_checkin_date = ""
        self._scheduler: AsyncIOScheduler | None = None

    def start(self):
        """启动调度器（幂等）"""
        if not self.auto_time or self._scheduler is not None:
            return
        hour, minute = int(self.auto_time[:2]), int(self.auto_time[3:5])
        self._scheduler = AsyncIOScheduler()
        try:
            # 先移除旧任务(重载插件时防重复注册)
            self._scheduler.remove_job("skland_auto_checkin")
        except Exception:
            pass
        self._scheduler.add_job(
            self._run_once,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="skland_auto_checkin",
            misfire_grace_time=3600,  # 错过(休眠/暂停)1小时内仍补执行
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(f"AMA-10 Skland: 自动签到任务已启动，每天 {hour:02d}:{minute:02d} 执行")

    def stop(self):
        """停止调度器（幂等）"""
        if self._scheduler:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def _run_once(self):
        """每日触发一次: 随机延迟 -> 日期去重 -> 静默签到"""
        try:
            # 每次触发都重新读配置(防重载后配置未生效)
            if not self.service.storage.load_auth():
                logger.info("AMA-10 Skland: 自动签到跳过, 无已登录用户")
                return

            if self.random_delay > 0:
                delay = random.randint(0, self.random_delay)
                logger.info(f"AMA-10 Skland: 自动签到随机延迟 {delay}s")
                await asyncio.sleep(delay)

            today = datetime.now().strftime("%Y-%m-%d")
            if today == self._last_checkin_date:
                return
            self._last_checkin_date = today

            # 静默签到: 不推送消息
            await self.service.checkin_all_users()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"AMA-10 Skland: 自动签到执行异常: {e}")