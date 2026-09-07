"""src/client.py - 森空岛 (Skland) API 客户端（v0.5.0 重写）

对应 Azincc/astrbot_plugin_skland v1.4.0 方案 (2026-09-07 迁移):
  - 设备 ID (dId) 由官方设备中心签发 (see did.py), 而非本地随机 —— 降低风控
  - 森空岛业务接口全部走 Web 版签名 (platform=3, vName=1.0.0)
  - 换票接口 (grant/generate_cred) 无需签名, 只需 dId + Login UA (Azincc 实测)
  - generate_cred 用 /web/v1/user/auth/generate_cred_by_code (Azincc 实测路径)
  - 终末地签到: /web/v1/game/endfield/attendance + sk-game-role 头逐角色签到
  - httpx.AsyncClient 异步 + _request 统一重试 (3 次, 4xx 不重试)
  - 错误分类: 「用户未登录」-> CredExpiredError (上层提示重新登录)

保留 (本插件独有, 经实测可用):
  - 账号服 (as.hypergryph.com) 接口保留 App 设备头 (X-DeviceId/X-DeviceModel 等)
    —— 手机号验证码登录链路 (send_phone_code/token_by_phone_code) 依赖它
  - 论坛版块签到: /api/v1/score/ischeckin + /api/v1/score/checkin (切新签名)
  - 「已签到/重复签到」判定 (HTTP 403 + code=10001 不算失败)
"""

import hashlib
import hmac
import json
import time

import httpx

from .did import get_device_id
from .ua_pool import random_user_agent

# ============================ 常量 ============================
AS_HOST = "https://as.hypergryph.com"
ZONAI_HOST = "https://zonai.skland.com"

# 客户端标识（账号服 App 参数, 手机号登录链路保留）
APP_CODE = "4ca99fa6b56cc2ba"  # 森空岛 appCode
DEVICE_TYPE = "1"
VNAME = "1.62.0"   # App 版 vName (账号服)
PLATFORM = "1"     # App 版 platform (账号服)
VCODE = "106200040"

# Web 版签名参数 (森空岛业务接口, Azincc 方案)
WEB_PLATFORM = "3"    # platform=3 (Web)
WEB_VNAME = "1.0.0"   # vName (Web 版)

# 请求头 UA (登录链路使用, Azincc 风格)
LOGIN_USER_AGENT = "Skland/1.0.1 (com.hypergryph.skland; build:100001014; Android 31; ) Okhttp/4.11.0"

# Android 版本 -> os 内部版本号 (账号服 App 版 os 头)
_OS_INT_MAP = {13: 33, 14: 34, 15: 35, 16: 36}


# ============================ 签名 ============================
def gen_sign(token: str, path: str, body_or_query: str, did: str,
             platform: str = WEB_PLATFORM, vname: str = WEB_VNAME):
    """生成 Web 版签名与 timestamp。

    公式: md5(hex(hmac_sha256(token, path + body_or_query + ts + header_ca)))
    header_ca = {"platform": 3, "timestamp", "dId", "vName": "1.0.0"} 的紧凑 JSON
    """
    timestamp = str(int(time.time()) - 2)
    header_ca = json.dumps({
        "platform": platform,
        "timestamp": timestamp,
        "dId": did,
        "vName": vname,
    }, separators=(",", ":"))
    s = f"{path}{body_or_query}{timestamp}{header_ca}"
    hmac_result = hmac.new(token.encode(), s.encode(), hashlib.sha256).hexdigest()
    sign = hashlib.md5(hmac_result.encode()).hexdigest()
    return sign, timestamp


# ============================ 错误分类 ============================
class CredExpiredError(RuntimeError):
    """凭据失效/用户未登录 (需要重新登录)"""


# 已签到/重复签到关键词（服务端返回 HTTP 403 + code=10001, message 含下列字样）
_DUPLICATE_KEYWORDS = ("已签到", "请勿重复", "重复签到", "签到过", "今日已", "请勿重复签到")


def is_duplicate_message(msg: str) -> bool:
    """判断签到失败 message 是否属于"今日已签到"类"""
    return any(k in msg for k in _DUPLICATE_KEYWORDS)


def _is_expired_message(msg: str) -> bool:
    """判断是否通知「用户未登录」类错误 (Azincc 分类)"""
    return any(k in msg for k in ("用户未登录", "登录已过期", "登录已失效", "未登录"))


# ============================ 客户端 ============================
class SklandClient:
    """森空岛 API 客户端 (异步)。

    设备指纹策略 (v0.5.0):
      - did: 官方设备中心签发 (B{deviceId}), 由调用方持久化复用
      - ua:  真实浏览器 UA (ua_pool 随机), 由调用方持久化复用
      两者解耦: dId 恒定, UA 登录时随机一次后恒定。
    """

    def __init__(self, timeout: float = 15.0, did: str | None = None,
                 ua: str | None = None):
        self.timeout = timeout
        self.did = did or ""
        self.ua = ua or random_user_agent()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "Accept-Encoding": "gzip",
                    "Connection": "close",
                },
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, headers: dict | None = None,
                       json_data: dict | None = None, data: str | None = None,
                       max_retries: int = 3) -> dict:
        """统一请求入口: 重试 3 次 (仅网络/5xx/429, 4xx 业务错误不重试)。

        返回响应 JSON; 全部重试失败抛 httpx 异常。
        ⚠️ json_data=None 时不发送 body (Azincc 终末地签到无 body, 签名用空串)。
        """
        client = await self._get_client()
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = None
                if method.upper() == "GET":
                    resp = await client.get(url, headers=headers)
                elif data is not None:
                    resp = await client.post(url, headers=headers, data=data)
                elif json_data is not None:
                    resp = await client.post(url, headers=headers, json=json_data)
                else:
                    resp = await client.post(url, headers=headers)  # 无 body

                # 5xx/429 自动重试; 4xx 不重试 (业务错误, 由调用方解析)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                if resp.status_code >= 400:
                    return resp.json() if resp.content else {}
                return resp.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                last_error = e
                if attempt < max_retries:
                    import asyncio
                    await asyncio.sleep(1)
        raise last_error

    # ---------- 账号服 (App 参数, 手机号登录链路保留) ----------
    def _as_headers(self) -> dict:
        """账号服请求头 (App 设备参数, 与 v0.4.0 一致)"""
        did_raw = self.did.replace("B", "") if self.did else ""
        return {
            "X-DeviceId": (did_raw[:32].ljust(32, "0")) or "0" * 32,
            "X-DeviceModel": "SM-A5560",
            "X-DeviceType": DEVICE_TYPE,
            "X-OSVer": "12",
            "User-Agent": LOGIN_USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
            "dId": self.did,
        }

    def _web_headers(self, token: str | None, cred: str | None = None,
                     body_or_query: str = "", path: str = "") -> dict:
        """森空岛 Web 版请求头 (含签名, platform=3)"""
        h = {
            "User-Agent": self.ua,
            "X-Requested-With": "com.hypergryph.skland",
            "dId": self.did,
            "content-type": "application/json",
            # Web 版固定头 (来自 Azincc/Rust)
            "platform": WEB_PLATFORM,
            "vname": WEB_VNAME,
        }
        if cred:
            h["cred"] = cred
        if token:
            sign, ts = gen_sign(token, path, body_or_query, self.did)
            h["timestamp"] = ts
            h["sign"] = sign
        return h

    def _check(self, data: dict, api: str) -> dict:
        """统一检查响应并返回 json (code=0 即成功)"""
        if not isinstance(data, dict):
            raise RuntimeError(f"[{api}] 非 JSON 响应")
        return data

    # ---------- 登录链路 (账号服, 保留) ----------
    async def send_phone_code(self, phone: str):
        """① 发送短信验证码"""
        url = f"{AS_HOST}/general/v1/send_phone_code"
        resp = await self._request(
            "POST", url, headers=self._as_headers(),
            json_data={"phone": phone, "type": 2},
        )
        data = self._check(resp, "send_phone_code")
        if data.get("status") != 0:
            raise RuntimeError(f"[send_phone_code] 失败: {data}")
        return True

    async def token_by_phone_code(self, phone: str, code: str):
        """② 验证码换登录 token"""
        url = f"{AS_HOST}/user/auth/v2/token_by_phone_code"
        resp = await self._request(
            "POST", url, headers=self._as_headers(),
            json_data={"phone": phone, "code": code, "appCode": APP_CODE},
        )
        data = self._check(resp, "token_by_phone_code")
        if data.get("status") != 0:
            raise RuntimeError(
                f"[token_by_phone_code] 失败: {data.get('msg')} (status={data.get('status')})"
            )
        t = data["data"]
        return t["token"], t["hgId"], t["deviceToken"]

    async def basic_info(self, token: str) -> dict:
        """③ 拉取用户信息 (实名/防沉迷判定, 可选)"""
        url = f"{AS_HOST}/user/info/v1/basic"
        resp = await self._request(
            "GET", url, headers=self._as_headers(),
        )
        data = self._check(resp, "basic")
        return data.get("data", {})

    async def grant(self, token: str, device_token: str):
        """④ 登录 token 换授权码 (账号服, 保留现有实现)"""
        url = f"{AS_HOST}/user/oauth2/v2/grant"
        resp = await self._request(
            "POST", url, headers=self._as_headers(),
            json_data={"token": token, "appCode": APP_CODE, "type": 0,
                       "deviceToken": device_token},
        )
        data = self._check(resp, "grant")
        if data.get("status") != 0:
            raise RuntimeError(f"[grant] 失败: {data}")
        return data["data"]["code"]

    # ---------- 森空岛 (Web 版签名) ----------
    async def generate_cred(self, auth_code: str):
        """⑤ 授权码换 cred (Web 版端点 /web/v1/, Azincc 实测无需签名, 只需 dId)"""
        url = f"{ZONAI_HOST}/web/v1/user/auth/generate_cred_by_code"
        resp = await self._request(
            "POST", url,
            headers={"User-Agent": LOGIN_USER_AGENT, "dId": self.did},
            json_data={"code": auth_code, "kind": 1},
        )
        data = self._check(resp, "generate_cred_by_code")
        if data.get("code") != 0:
            raise RuntimeError(
                f"[generate_cred_by_code] 失败: {data.get('message')} (code={data.get('code')})"
            )
        d = data["data"]
        return d["cred"], d["token"]

    async def user_me(self, cred: str, token: str) -> dict:
        """⑥ 拉取森空岛用户信息 (带签名), data.user 含 id/nickname/avatar"""
        path = "/api/v1/user/me"
        resp = await self._request(
            "GET", f"{ZONAI_HOST}{path}",
            headers=self._web_headers(token=token, cred=cred, path=path, body_or_query=""),
        )
        data = self._check(resp, "user/me")
        if data.get("code") != 0:
            raise RuntimeError(f"[user/me] 失败: {data.get('message')} (code={data.get('code')})")
        return data.get("data", {})

    # ---------- 业务: 绑定列表 ----------
    async def get_binding_list(self, cred: str, token: str):
        """⑦ 拉取游戏绑定列表 (带签名, Web 版)

        v0.5.0: 统一读取 defaultRole + roles 双字段 (Azincc 用 roles, 老版用 defaultRole),
        兼容两种响应形态。
        """
        path = "/api/v1/game/player/binding"
        resp = await self._request(
            "GET", f"{ZONAI_HOST}{path}",
            headers=self._web_headers(token=token, cred=cred, path=path, body_or_query=""),
        )
        data = self._check(resp, "game/player/binding")
        if data.get("code") != 0:
            msg = data.get("message", "")
            if _is_expired_message(msg):
                raise CredExpiredError("用户登录已过期，请重新登录")
            raise RuntimeError(f"[game/player/binding] 失败: {msg}")
        bindings = []
        for item in data.get("data", {}).get("list", []):
            app_code = item.get("appCode")
            for b in item.get("bindingList", []):
                nick = b.get("nickName") or (b.get("defaultRole") or {}).get("nickname") or "(未设置)"
                # roles: Azincc 逐角色签到用; defaultRole: 老版单角色
                roles = b.get("roles") or []
                if roles:
                    # roles 结构: [{roleId, serverId, nickname, ...}]
                    roles = [r for r in roles if r.get("roleId")]
                else:
                    dr = b.get("defaultRole") or {}
                    if dr.get("roleId"):
                        roles = [dr]
                bindings.append({
                    "appCode": app_code,
                    "gameName": b.get("gameName"),
                    "nickName": nick,
                    "channelName": b.get("channelName"),
                    "uid": b.get("uid"),
                    "gameId": b.get("gameId"),
                    "roles": roles,                 # v0.5.0 主字段
                    "defaultRole": b.get("defaultRole"),  # 兼容保留
                })
        return bindings

    # ---------- 论坛版块签到 (保留, 切 Web 签名) ----------
    # gameId 映射 (来自 HAR 抓包 + 社区结构)
    FORUM_GAME_NAMES = {
        1: "明日方舟",
        2: "来自星尘",
        3: "明日方舟：终末地",
        4: "泡姆泡姆",
        100: "纳斯特港",
        101: "开拓芯",
    }

    async def forum_checkin_status(self, cred: str, token: str):
        """查询论坛各版块签到状态, 返回 [{gameId, checked}]"""
        path = "/api/v1/score/ischeckin"
        resp = await self._request(
            "GET", f"{ZONAI_HOST}{path}",
            headers=self._web_headers(token=token, cred=cred, path=path, body_or_query=""),
        )
        data = self._check(resp, "score/ischeckin")
        if data.get("code") != 0:
            msg = data.get("message", "")
            if _is_expired_message(msg):
                raise CredExpiredError("用户登录已过期，请重新登录")
            raise RuntimeError(f"[score/ischeckin] 失败: {msg} (code={data.get('code')})")
        return data.get("data", {}).get("list", [])

    async def forum_checkin(self, cred: str, token: str, game_id: int) -> bool:
        """论坛单版块签到 (Web 签名)。

        返回 True=签到成功; False=今日已签到(不算失败); 抛异常=失败。
        """
        path = "/api/v1/score/checkin"
        body_str = json.dumps({"gameId": game_id}, separators=(",", ":"), ensure_ascii=False)
        resp = await self._request(
            "POST", f"{ZONAI_HOST}{path}",
            headers=self._web_headers(token=token, cred=cred, path=path, body_or_query=body_str),
            data=body_str,
        )
        # 重复签到: code=10001 (Azincc 无此端点, 按实测结果处理)
        if resp.get("code") == 10001:
            msg = resp.get("message", "")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[score/checkin] HTTP 403: {msg}")
        if resp.get("code") != 0:
            msg = resp.get("message", "")
            if _is_expired_message(msg):
                raise CredExpiredError("用户登录已过期，请重新登录")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[score/checkin] 失败: {msg} (code={resp.get('code')})")
        return True

    # ---------- 业务: 游戏签到 ----------
    async def sign_attendance(self, cred: str, token: str, game_id: int,
                              binding: dict, uid: str = None) -> bool:
        """签到按游戏区分端点与参数 (v0.5.0 Azincc 方案)。

        返回 True=签到成功; False=已签到(不算失败); 抛异常=失败。
        - arknights(1):  POST /api/v1/game/attendance  body={gameId, uid}
        - endfield(3):   POST /web/v1/game/endfield/attendance
                          头 sk-game-role: 3_{roleId}_{serverId}, 逐角色签到
        """
        if game_id == 3:
            return await self._sign_endfield(cred, token, binding)
        path = "/api/v1/game/attendance"
        body = {"gameId": game_id, "uid": uid}
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        resp = await self._request(
            "POST", f"{ZONAI_HOST}{path}",
            headers=self._web_headers(token=token, cred=cred, path=path, body_or_query=body_str),
            data=body_str,
        )
        if resp.get("code") == 10001:
            msg = resp.get("message", "")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[game/attendance] HTTP 403: {msg}")
        if resp.get("code") != 0:
            msg = resp.get("message", "")
            if _is_expired_message(msg):
                raise CredExpiredError("用户登录已过期，请重新登录")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[game/attendance] 失败: {msg} (code={resp.get('code')})")
        return True

    async def _sign_endfield(self, cred: str, token: str, binding: dict) -> bool:
        """终末地签到 (Azincc 方案): /web/v1/game/endfield/attendance

        逐角色签到 (roles 列表), 头 sk-game-role: 3_{roleId}_{serverId},
        referer/origin: https://game.skland.com/; 签名 body 为空字符串。
        返回 True=全部角色签到成功/已签到; False=有角色未签到(不算失败);
        抛异常=真实失败。
        """
        roles = binding.get("roles") or []
        if not roles:
            raise RuntimeError("[endfield] 无角色数据（roleId/serverId 缺失），无法签到")

        path = "/web/v1/game/endfield/attendance"
        url = f"{ZONAI_HOST}{path}"
        results = []
        for role in roles:
            role_id = role.get("roleId", "")
            server_id = role.get("serverId", "")
            if not role_id or not server_id:
                results.append(False)  # 无角色 ID, 视为未签
                continue
            headers = self._web_headers(token=token, cred=cred, path=path, body_or_query="")
            headers["Content-Type"] = "application/json"
            headers["sk-game-role"] = f"3_{role_id}_{server_id}"
            headers["referer"] = "https://game.skland.com/"
            headers["origin"] = "https://game.skland.com/"

            resp = await self._request("POST", url, headers=headers)
            if resp.get("code") == 10001:
                msg = resp.get("message", "")
                if is_duplicate_message(msg):
                    results.append(True)  # 已签到视为成功
                    continue
                raise RuntimeError(f"[endfield] HTTP 403: {msg}")
            if resp.get("code") != 0:
                msg = resp.get("message", "")
                if _is_expired_message(msg):
                    raise CredExpiredError("用户登录已过期，请重新登录")
                raise RuntimeError(f"[endfield] 失败: {msg} (code={resp.get('code')})")
            results.append(True)
        return all(results)

    # ---------- 工具 ----------
    @staticmethod
    def get_awards_text(resp: dict) -> str:
        """从签到响应 data.awards 汇总奖励文本"""
        awards = []
        for a in (resp.get("data") or {}).get("awards", []):
            name = a.get("resource", {}).get("name", "未知")
            count = a.get("count", 1)
            awards.append(f"{name}x{count}")
        return ", ".join(awards) if awards else "无"
