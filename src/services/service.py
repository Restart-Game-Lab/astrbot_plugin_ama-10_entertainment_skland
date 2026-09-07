"""src/service.py - 认证/签到业务服务 (v0.5.0 异步版)

封装与 AstrBot 无关的纯业务逻辑:
  - login(phone, code): 验证码完成登录, 返回凭据字典 (设备中心 dId + UA)
  - do_checkin(phone, auth): 对单个账号签到所有绑定游戏
  - checkin_all(uid): 签到指定用户全部账号, 返回展示文本
  - checkin_all_admin(): 管理员批量签到全部账号, 返回汇总文本
  - 自动签到调度(auto_checkin_loop): 每日定时 + 随机延迟

v0.5.0 变更:
  - 全部改 async (httpx 异步)
  - 登录/签到时自动生成/复用官方设备中心 dId (did.py)
  - UA 登录时随机一次, 持久化复用 (ua_pool.py)
  - 终末地逐角色签到 (roles 列表)
  - 错误分类: CredExpiredError -> 提示重新登录
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..api.client import CredExpiredError, SklandClient
from ..api.did import get_device_id
from ..storage.storage import Storage

logger = logging.getLogger("ama10_skland")


class SklandService:
    """森空岛业务服务（依赖注入 Storage 与请求超时, 便于测试）"""

    def __init__(self, storage: Storage, timeout: float = 15.0,
                 game_enabled: bool = True, forum_enabled: bool = True,
                 ua_pool_enabled: bool = True):
        self.storage = storage
        self.timeout = timeout
        self.game_enabled = game_enabled
        self.forum_enabled = forum_enabled
        # UA 池开关: True=随机浏览器 UA 池; False=固定老插件 USER_AGENT(逐字节一致)
        self.ua_pool_enabled = ua_pool_enabled

    # ---------- 工具 ----------
    @staticmethod
    def _ensure_did_and_ua(auth: dict) -> tuple[str, str]:
        """从凭据中取 did/ua; 缺失时生成 (did: 设备中心, ua: 随机)。

        返回 (did, ua)。调用方负责把新值写回 (set_auth)。
        """
        did = auth.get("did") or ""
        ua = auth.get("ua_used") or ""
        return did, ua

    # ---------- 登录 ----------
    @staticmethod
    def _fallback_did() -> str:
        """设备中心不可用时的本地随机兜底 dId (机制: 服务端可能拒绝但链路可通)"""
        import hashlib as _hl
        import uuid as _uuid
        return "B" + _hl.md5(_uuid.uuid4().hex.encode()).hexdigest()[:16]

    async def login(self, phone: str, code: str) -> dict:
        """执行完整验证码登录链路, 返回凭据字典(已由调用方负责保存)。

        抛出 RuntimeError 表示登录失败。
        v0.5.0: 登录成功后生成设备中心 dId, UA 随机一次。
        """
        client = SklandClient(timeout=self.timeout, use_ua_pool=self.ua_pool_enabled)
        try:
            token, hg_id, device_token = await client.token_by_phone_code(phone, code)
            try:
                u = await client.basic_info(token)
            except Exception:
                u = {}

            # v0.5.0: 先申请官方设备中心 dId (grant 需要), UA 随机一次
            try:
                did = await get_device_id(timeout=self.timeout)
            except Exception as e:
                logger.warning(f"AMA-10 Skland: 设备中心 dId 申请失败, 使用本地随机兜底: {e}")
                did = ""
            client.did = did or self._fallback_did()

            # 对齐 PR #17: grant 无 deviceToken 参数
            auth_code = await client.grant(token)
            cred, cred_token = await client.generate_cred(auth_code)

            # 森空岛真实用户名: 优先 user/me(App 同款, 带签名), 失败回退 basic_info
            nick = ""
            try:
                me = await client.user_me(cred, cred_token)
                nick = (me.get("user") or {}).get("nickname") or ""
            except Exception:
                logger.debug("AMA-10 Skland: user/me 获取昵称失败, 回退 basic_info")
            if not nick:
                nick = u.get("nickName") or ""
        finally:
            await client.close()

        return {
            "cred": cred,
            "token": cred_token,
            "login_token": token,
            "device_token": device_token,
            "hgId": hg_id,
            "nickName": nick or "(未设置昵称)",
            # v0.5.0: 设备中心 dId + 真实浏览器 UA (持久化复用)
            "did": client.did,
            "ua_used": client.ua,
            "fingerprint": {},  # 兼容字段保留空 (机型池已弃用)
        }

    async def send_code(self, phone: str):
        """发送短信验证码"""
        client = SklandClient(timeout=self.timeout, use_ua_pool=self.ua_pool_enabled)
        try:
            await client.send_phone_code(phone)
        finally:
            await client.close()

    async def check_auth(self, auth: dict) -> dict:
        """验证登录态是否有效 (实际请求 user/me 端点, 拉取用户名)。

        返回: {"ok": bool, "nickname": str, "error": str}
          - ok=True:  登录态有效, nickname 为最新森空岛用户名
          - ok=False: 凭据失效 (CredExpiredError) 或请求失败
        """
        did, ua = self._ensure_did_and_ua(auth)
        if not did:
            did = self._fallback_did()
        client = SklandClient(timeout=self.timeout, did=did or None, ua=ua or None,
                             use_ua_pool=self.ua_pool_enabled)
        try:
            cred, cred_token = auth["cred"], auth["token"]
            me = await client.user_me(cred, cred_token)
            nickname = (me.get("user") or {}).get("nickname") or ""
            return {"ok": True, "nickname": nickname, "error": ""}
        except CredExpiredError as e:
            return {"ok": False, "nickname": "", "error": str(e)}
        except Exception as e:
            # 网络/响应异常不等同于凭据失效, 但仍提示用户
            return {"ok": False, "nickname": "", "error": str(e)}
        finally:
            await client.close()

    # ---------- 签到 ----------
    @staticmethod
    def _mask_phone(phone: str) -> str:
        """手机号遮罩 186****0000"""
        return f"{phone[:3]}****{phone[-4:]}"

    async def do_checkin(self, phone: str, auth: dict) -> dict:
        """对单个账号执行签到，返回分组行列表 + 论坛球串。

        返回结构:
          game_ok / game_err     : 游戏签到行(直接带球符)
          forum_row / forum_msg  : 论坛一行球串 / 附加说明(失败原因等)
        已签到视为成功(不算失败)。

        v0.5.0: did/ua 从 auth 取 (缺失时生成, 调用方负责写回)。
        """
        # 完全隔离: 两开关全关 -> 不创建 client、不请求设备中心、零 API 调用
        if not self.game_enabled and not self.forum_enabled:
            return {"disabled": True, "game_ok": [], "game_err": [],
                    "forum_row": "", "forum_err": [],
                    "did": None, "ua": None}

        did, ua = self._ensure_did_and_ua(auth)
        if not did:
            try:
                did = await get_device_id(timeout=self.timeout)
            except Exception:
                did = ""
            if not did:
                did = self._fallback_did()
        client = SklandClient(timeout=self.timeout, did=did or None, ua=ua or None,
                             use_ua_pool=self.ua_pool_enabled)
        cred, cred_token = auth["cred"], auth["token"]

        groups = {
            "game_ok": [], "game_err": [],
            "forum_row": "", "forum_err": [],
            "did": client.did,          # 本次实际使用的 dId (写回用)
            "ua": client.ua,            # 本次实际使用的 UA (写回用)
        }

        # ---- 游戏签到（受 game_enabled 开关控制）----
        if self.game_enabled:
            try:
                bindings = await client.get_binding_list(cred, cred_token)
            except CredExpiredError as e:
                groups["game_err"].append(f"⚠️ {e}")
                bindings = []
            except Exception as e:
                groups["game_err"].append(f"⚠️ 绑定列表获取失败: {e}")
                bindings = []

            for b in bindings:
                name = f"{b.get('gameName')} | {b.get('nickName')}"
                try:
                    await client.sign_attendance(cred, cred_token, b["gameId"], b, b.get("uid"))
                    groups["game_ok"].append(f"🟢 {name}")
                except CredExpiredError as e:
                    groups["game_err"].append(f"🔴 {name} {e}")
                except Exception as e:
                    groups["game_err"].append(f"🔴 {name} {e}")

        # ---- 论坛签到（受 forum_enabled 开关控制）----
        if self.forum_enabled:
            try:
                status_list = await client.forum_checkin_status(cred, cred_token)
                balls = []
                for item in status_list:
                    gid = item.get("gameId")
                    name = SklandClient.FORUM_GAME_NAMES.get(gid, f"版块{gid}")
                    if item.get("checked"):
                        balls.append("🟢")
                        continue
                    try:
                        await client.forum_checkin(cred, cred_token, gid)
                        balls.append("🟢")
                    except CredExpiredError as e:
                        balls.append("🔴")
                        groups["forum_err"].append(f"{name}: {e}")
                    except Exception as e:
                        balls.append("🔴")
                        groups["forum_err"].append(f"{name}: {e}")
                groups["forum_row"] = " ".join(balls) if balls else "（无版块）"
            except CredExpiredError as e:
                groups["forum_row"] = "🔴"
                groups["forum_err"].append(f"查询失败: {e}")
            except Exception as e:
                groups["forum_row"] = "🔴"
                groups["forum_err"].append(f"查询失败: {e}")

        await client.close()
        return groups

    # ---------- 展示/写回辅助(单人版与管理版共用) ----------
    def _format_groups(self, g: dict) -> tuple[list[str], int, int]:
        """把 do_checkin 的分组结果转成展示行。

        仅显示启用的模块, 关闭的模块整个段落不输出。
        返回 (sections, ok_n, fail_n): 展示行列表 / 成功数 / 失败数。
        """
        sections: list[str] = []
        ok_n = fail_n = 0

        if self.game_enabled:
            game_rows = g.get("game_ok", []) or ["🟢 (无绑定游戏)"]
            game_rows += g.get("game_err", [])
            sections.append("[游戏签到]:")
            sections.extend(game_rows)
            ok_n += len(g.get("game_ok", []))
            fail_n += len(g.get("game_err", []))

        if self.forum_enabled:
            forum_balls = (g.get("forum_row") or "").split()
            sections.append("[论坛签到]:")
            sections.append(g["forum_row"])
            ok_n += forum_balls.count("🟢")
            fail_n += len(g.get("forum_err", []))
            sections.extend(f"   🔴 {err}" for err in g.get("forum_err", []))

        return sections, ok_n, fail_n

    def _backfill_did_ua(self, uid: str, phone: str, auth: dict, g: dict):
        """v0.5.0: 无 did/ua 的存量账号写回 (之后恒定)。"""
        if not auth.get("did") or not auth.get("ua_used"):
            auth["did"] = g.get("did", "")
            auth["ua_used"] = g.get("ua", "")
            self.storage.set_auth(
                uid, phone,
                cred=auth["cred"], token=auth["token"],
                login_token=auth.get("login_token", ""),
                device_token=auth.get("device_token", ""),
                hg_id=auth.get("hgId", ""),
                fingerprint=auth.get("fingerprint") or {},
                nick_name=auth.get("nickName", ""),
                did=auth.get("did", ""),
                ua_used=auth.get("ua_used", ""),
            )

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
            try:
                g = await self.do_checkin(p, a)
            except CredExpiredError as e:
                lines.append(f"🟡 {self._mask_phone(p)} 凭据失效，请重新登录")
                lines.append(f"   /skland login {self._mask_phone(p)}")
                continue
            except Exception as e:
                lines.append(f"🔴 {self._mask_phone(p)} 签到失败: {e}")
                continue

            # 双关时无任何网络请求: 跳过写回与输出
            if g.get("disabled"):
                continue

            # v0.5.0: 无 did/ua 的存量账号写回 (之后恒定)
            self._backfill_did_ua(uid, p, a, g)

            # 分组输出: 仅显示启用的模块, 关闭的模块整个段落不输出
            sections, ok_n, fail_n = self._format_groups(g)

            if fail_n == 0:
                head = "🟢 签到完成"
            elif ok_n == 0:
                head = "🔴 签到失败"
            else:
                head = "🟡 签到完成异常"

            lines.append(head)
            lines.extend(sections)

        return "\n".join(lines)

    async def checkin_all_admin(self) -> str:
        """管理员全量签到: 遍历所有已登录账号, 逐个签到并返回汇总文本。

        与 checkin_all_users(静默)不同: 返回给管理员看的完整汇总,
        每账号一行主状态(球 + 昵称 + 掩码手机号), 失败/异常展开细节,
        底部三色汇总计数, 风格与 status_all 一致。
        """
        all_auth = self.storage.load_auth()
        if not all_auth:
            return "全量签到完成（共 0 个账号）\n\n🟡 暂无用户登录"

        entries = [
            (uid, phone, a)
            for uid, auths in all_auth.items()
            for phone, a in auths.items()
        ]

        ok_n = err_n = warn_n = 0
        lines = [f"全量签到完成（共 {len(entries)} 个账号）", ""]
        for uid, phone, a in entries:
            nick = a.get("nickName") or "(未设置昵称)"
            phone_m = self._mask_phone(phone)
            try:
                g = await self.do_checkin(phone, a)
            except CredExpiredError:
                warn_n += 1
                lines.append(f"🟡 {nick} | {phone_m}")
                lines.append(f"   ⚠️ 凭据失效，请重新登录（/skland login {phone_m}）")
                continue
            except Exception as e:
                err_n += 1
                lines.append(f"🔴 {nick} | {phone_m}")
                lines.append(f"   ⚠️ 签到失败: {e}")
                continue

            # 双关时无任何网络请求: 跳过输出与计数
            if g.get("disabled"):
                continue

            # v0.5.0: 无 did/ua 的存量账号写回 (之后恒定)
            self._backfill_did_ua(uid, phone, a, g)

            sections, g_ok, g_fail = self._format_groups(g)
            if g_fail == 0:
                mark = "🟢"
                ok_n += 1
            elif g_ok == 0:
                mark = "🔴"
                err_n += 1
            else:
                mark = "🟡"
                warn_n += 1

            lines.append(f"{mark} {nick} | {phone_m}")
            lines.extend(f"   {s}" for s in sections)

        lines.append("")
        lines.append(f"[汇总]: 🟢 {ok_n} · 🔴 {err_n} · 🟡 {warn_n}")
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
    """自动签到调度器（基于 APScheduler AsyncIOScheduler + CronTrigger）。

    参考 Azincc/astrbot_plugin_skland 的定时方案:
      - AsyncIOScheduler + CronTrigger.from_crontab(表达式): 按 cron 触发
      - misfire_grace_time: 错过触发时间(如休眠/重启)仍补执行
      - 随机延迟 0~random_delay 秒 + 按日期去重, 避免重复/固定时刻风控
    ⚠️ 静默模式: 不推送任何消息, 结果仅记录日志。

    生命周期:
      - start(): 启动调度器并注册任务（建议在 Star.initialize() 中调用）
      - stop(): 停止调度器并取消任务（建议在 Star.terminate() 中调用）
    """

    def __init__(self, service: SklandService,
                 auto_cron: str | None,
                 random_delay: int = 0):
        self.service = service
        self.auto_cron = auto_cron  # cron 表达式, 如 "0 6 * * *", None=关闭
        self.random_delay = max(int(random_delay), 0)
        self._last_checkin_date = ""
        self._scheduler: AsyncIOScheduler | None = None

    def start(self):
        """启动调度器（幂等）"""
        if not self.auto_cron or self._scheduler is not None:
            return
        self._scheduler = AsyncIOScheduler()
        try:
            # 先移除旧任务(重载插件时防重复注册)
            self._scheduler.remove_job("skland_auto_checkin")
        except Exception:
            pass
        self._scheduler.add_job(
            self._run_once,
            trigger=CronTrigger.from_crontab(self.auto_cron),
            id="skland_auto_checkin",
            misfire_grace_time=3600,  # 错过(休眠/暂停)1小时内仍补执行
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info(f"AMA-10 Skland: 自动签到任务已启动, cron 表达式: {self.auto_cron}")

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
