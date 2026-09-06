"""src/client.py - 森空岛 (Skland) API 客户端

对应 skland_signin.py 2026-09-06 新版逻辑:
  - 必须用 App 版接口 /api/v1/... + App 版参数 (platform=1, vname=1.62.0, os=内部版本号)
    web 版 /web/v1/... 实测返回 code=10001「设备信息无效」
  - generate_cred_by_code 换票无需 xsm/wtoken 风控参数
  - sign 算法: md5(hex(hmac_sha256(token, path + body_or_query + ts + header_ca)))
  - 签到: arknights(1) 用 /api/v1/game/attendance {gameId,uid};
    endfield(3) 用 /api/v1/game/endfield/attendance {gameId,serverId,roleId}
  - 重复签到返回 HTTP 403 + code=10001, 识别为"已签到"不算失败
"""

import hashlib
import hmac
import json
import random
import string
import time

import requests

# ============================ 常量 ============================
AS_HOST = "https://as.hypergryph.com"
ZONAI_HOST = "https://zonai.skland.com"

# 客户端标识（App v1.62.0 参数, 实测 web 版会返回 10001 设备信息无效）
APP_CODE = "4ca99fa6b56cc2ba"  # 森空岛 appCode
DEVICE_TYPE = "1"
VNAME = "1.62.0"  # App 版 vName
PLATFORM = "1"  # App 版 platform (1=Android)
VCODE = "106200040"

# Android 版本 -> os 内部版本号 (App 版 os 头, Android 16 = 36)
_OS_INT_MAP = {13: 33, 14: 34, 15: 35, 16: 36}

# 固定安卓系统版本(生成指纹时统一定死, 不展示; 13~16 均合法, 默认 14)
FIXED_OS_VER = "14"


def _os_int(os_ver: str) -> str:
    """Android 版本(13~16) → 内部版本号(33~36)，与抓包 os:36 一致"""
    return str(_OS_INT_MAP.get(int(os_ver), 33))


def _rand_hex(n: int) -> str:
    """生成 n 位随机十六进制"""
    return "".join(random.choices(string.hexdigits.lower(), k=n))


def _rand_rid() -> str:
    """rid 形如 1507bfd3f6975d76867（19 位 hex）"""
    return _rand_hex(19)


def _gen_device_fingerprint() -> dict:
    """从机型池随机生成一整套内部一致的设备指纹。

    关键: 所有维度必须一起换（did/X-DeviceId/model/os）, 只换一个字段会
    导致指纹自相矛盾、触发风控。机型取自 device_pool.random_device()
    (data/devices.json 的 11 个白名单品牌), 系统版本固定为 FIXED_OS_VER。
    """
    from .device_pool import random_device

    dev = random_device()  # {model, brand, name}
    os_ver = FIXED_OS_VER
    device_id = _rand_hex(32)  # X-DeviceId 用（32 位 hex）
    did = _rand_hex(16)  # did 用（16 位 hex）
    rid = _rand_rid()
    return {
        "device_model": dev["model"],      # ro.product.model, 用于 API 请求头
        "brand": dev["brand"],             # 展示用
        "device_name": dev["name"],        # 展示用(营销名)
        "os_ver": os_ver,
        "os_int": _os_int(os_ver),         # App 版 os 头(固定 34)
        "device_id": device_id,
        "did": did,
        "rid": rid,
        "device_type": DEVICE_TYPE,
        "vname": VNAME,
        "vcode": VCODE,
        "ua_as": "okhttp/4.12.0",
        "ua_sk": f"Skland/{VNAME} (com.hypergryph.skland; build:{VCODE}; Android {os_ver}; ) Okhttp/4.11.0",
        "channel": "OF",
        "manufacturer": dev["brand"],      # 与机型同品牌, 保持一致
        "language": "zh-cn",
    }


def fingerprint_to_dict(fp: dict) -> dict:
    """设备指纹 -> 可 JSON 序列化 dict(存 auth.json)。"""
    return dict(fp)


def dict_to_fingerprint(d: dict) -> dict:
    """auth.json 中存的指纹 dict -> 设备指纹(直接复用)。"""
    return dict(d)


def gen_sign(token: str, path: str, body_or_query: str,
             did: str, platform: int = PLATFORM, vname: str = VNAME):
    """生成 sign 与 timestamp。

    公式: md5(hex(hmac_sha256(token, path + body_or_query + ts + header_ca)))
    header_ca = {"platform", "timestamp", "dId", "vName"} 的紧凑 JSON
    """
    timestamp = str(int(time.time()) - 2)
    header_ca = json.dumps({
        "platform": str(platform),
        "timestamp": timestamp,
        "dId": did,
        "vName": vname,
    }, separators=(",", ":"))
    s = f"{path}{body_or_query}{timestamp}{header_ca}"
    hmac_result = hmac.new(token.encode(), s.encode(), hashlib.sha256).hexdigest()
    sign = hashlib.md5(hmac_result.encode()).hexdigest()
    return sign, timestamp


# 已签到/重复签到关键词（服务端返回 HTTP 403 + code=10001, message 含下列字样）
_DUPLICATE_KEYWORDS = ("已签到", "请勿重复", "重复签到", "签到过", "今日已", "请勿重复签到")


def is_duplicate_message(msg: str) -> bool:
    """判断签到失败 message 是否属于"今日已签到"类"""
    return any(k in msg for k in _DUPLICATE_KEYWORDS)


class SklandClient:
    """森空岛 API 客户端。

    设备指纹策略(防风控):
      - 传入 fp: 复用调用方提供的存档指纹(同一个账号签到/重登时保持一致)
      - 不传 fp: 生成全新指纹(仅首次登录时用)
    """

    def __init__(self, timeout: float = 15.0, fp: dict | None = None):
        self.timeout = timeout
        self.fp = fp if fp else _gen_device_fingerprint()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept-Encoding": "gzip",
            "Connection": "close",
        })

    # ---------- 请求头 ----------
    def _as_headers(self) -> dict:
        """账号服请求头（使用当前设备指纹）"""
        return {
            "X-DeviceId": self.fp["device_id"],
            "X-DeviceModel": self.fp["device_model"],
            "X-DeviceType": self.fp["device_type"],
            "X-OSVer": self.fp["os_ver"],
            "User-Agent": self.fp["ua_as"],
            "Content-Type": "application/json; charset=utf-8",
        }

    def _sk_headers(self, token=None, cred=None, body_or_query: str = "", path: str = ""):
        """森空岛请求头（含签名）"""
        h = {
            "platform": str(PLATFORM),
            "did": self.fp["did"],
            "language": self.fp["language"],
            "os": self.fp["os_int"],
            "nid": "1",
            "vname": self.fp["vname"],
            "vcode": self.fp["vcode"],
            "user-agent": self.fp["ua_sk"],
            "channel": self.fp["channel"],
            "manufacturer": self.fp["manufacturer"],
            "content-type": "application/json",
            "rid": self.fp["rid"],
        }
        if cred:
            h["cred"] = cred
        if token:
            sign, ts = gen_sign(token, path, body_or_query, self.fp["did"])
            h["timestamp"] = ts
            h["sign"] = sign
        return h

    def _check(self, resp: requests.Response, api: str) -> dict:
        """统一检查响应并返回 json"""
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"[{api}] 非 JSON 响应: {resp.status_code} {resp.text[:200]}")
        if resp.status_code != 200:
            raise RuntimeError(f"[{api}] HTTP {resp.status_code}: {resp.text[:200]}")
        return data

    # ---------- 登录链路 ----------
    def send_phone_code(self, phone: str):
        """① 发送短信验证码"""
        url = f"{AS_HOST}/general/v1/send_phone_code"
        body = {"phone": phone, "type": 2}
        resp = self.session.post(url, headers=self._as_headers(), json=body, timeout=self.timeout)
        data = self._check(resp, "send_phone_code")
        if data.get("status") != 0:
            raise RuntimeError(f"[send_phone_code] 失败: {data}")
        return True

    def token_by_phone_code(self, phone: str, code: str):
        """② 验证码换登录 token"""
        url = f"{AS_HOST}/user/auth/v2/token_by_phone_code"
        body = {"phone": phone, "code": code, "appCode": APP_CODE}
        resp = self.session.post(url, headers=self._as_headers(), json=body, timeout=self.timeout)
        data = self._check(resp, "token_by_phone_code")
        if data.get("status") != 0:
            raise RuntimeError(
                f"[token_by_phone_code] 失败: {data.get('msg')} (status={data.get('status')})"
            )
        t = data["data"]
        return t["token"], t["hgId"], t["deviceToken"]

    def basic_info(self, token: str) -> dict:
        """③ 拉取用户信息（实名/防沉迷判定，可选）。token 是 base64, 用 params= 自动编码。"""
        url = f"{AS_HOST}/user/info/v1/basic"
        resp = self.session.get(url, headers=self._as_headers(),
                                params={"token": token}, timeout=self.timeout)
        data = self._check(resp, "basic")
        return data.get("data", {})

    def grant(self, token: str, device_token: str):
        """④ 登录 token 换授权码"""
        url = f"{AS_HOST}/user/oauth2/v2/grant"
        body = {"token": token, "appCode": APP_CODE, "type": 0, "deviceToken": device_token}
        resp = self.session.post(url, headers=self._as_headers(), json=body, timeout=self.timeout)
        data = self._check(resp, "grant")
        if data.get("status") != 0:
            raise RuntimeError(f"[grant] 失败: {data}")
        return data["data"]["code"]

    def generate_cred(self, auth_code: str):
        """⑤ 授权码换 cred（App 版 /api/v1/, 实测无需 xsm/wtoken 也能过）"""
        url = f"{ZONAI_HOST}/api/v1/user/auth/generate_cred_by_code"
        body = {"code": auth_code, "kind": 1}
        resp = self.session.post(url, headers=self._sk_headers(), json=body, timeout=self.timeout)
        data = self._check(resp, "generate_cred_by_code")
        if data.get("code") != 0:
            raise RuntimeError(
                f"[generate_cred_by_code] 失败: {data.get('message')} (code={data.get('code')})"
            )
        d = data["data"]
        return d["cred"], d["token"]

    # ---------- 业务 ----------
    def get_binding_list(self, cred: str, token: str):
        """⑥ 拉取游戏绑定列表（带签名）"""
        path = "/api/v1/game/player/binding"
        resp = self.session.get(
            f"{ZONAI_HOST}{path}",
            headers=self._sk_headers(token=token, cred=cred, path=path, body_or_query=""),
            timeout=self.timeout,
        )
        data = self._check(resp, "game/player/binding")
        if data.get("code") != 0:
            raise RuntimeError(f"[game/player/binding] 失败: {data.get('message')}")
        bindings = []
        for item in data.get("data", {}).get("list", []):
            app_code = item.get("appCode")
            for b in item.get("bindingList", []):
                nick = b.get("nickName") or (b.get("defaultRole") or {}).get("nickname") or "(未设置)"
                bindings.append({
                    "appCode": app_code,
                    "gameName": b.get("gameName"),
                    "nickName": nick,
                    "channelName": b.get("channelName"),
                    "uid": b.get("uid"),
                    "gameId": b.get("gameId"),
                    "defaultRole": b.get("defaultRole"),  # endfield 签到需要 serverId/roleId
                })
        return bindings

    # ---------- 论坛版块签到 ----------
    # HAR 实测 (2026-09-06):
    #   GET  /api/v1/score/ischeckin  -> 查询各版块签到状态 {list:[{gameId, checked}]}
    #   POST /api/v1/score/checkin    -> body {"gameId":N} 签到
    # gameId 映射（来自 HAR 抓包 + 社区结构）
    FORUM_GAME_NAMES = {
        1: "明日方舟",
        2: "来自星尘",
        3: "明日方舟：终末地",
        4: "泡姆泡姆",
        100: "纳斯特港",
        101: "开拓芯",
    }

    def forum_checkin_status(self, cred: str, token: str):
        """查询论坛各版块签到状态，返回 [{gameId, checked}]（已签到的会被跳过）"""
        path = "/api/v1/score/ischeckin"
        resp = self.session.get(
            f"{ZONAI_HOST}{path}",
            headers=self._sk_headers(token=token, cred=cred, path=path, body_or_query=""),
            timeout=self.timeout,
        )
        data = self._check(resp, "score/ischeckin")
        if data.get("code") != 0:
            raise RuntimeError(f"[score/ischeckin] 失败: {data.get('message')} (code={data.get('code')})")
        return data.get("data", {}).get("list", [])

    def forum_checkin(self, cred: str, token: str, game_id: int) -> bool:
        """论坛单版块签到。

        返回 True=签到成功; False=今日已签到(不算失败); 抛异常=失败。
        """
        path = "/api/v1/score/checkin"
        body_str = json.dumps({"gameId": game_id}, separators=(",", ":"), ensure_ascii=False)
        resp = self.session.post(
            f"{ZONAI_HOST}{path}",
            headers=self._sk_headers(token=token, cred=cred, path=path, body_or_query=body_str),
            data=body_str,
            timeout=self.timeout,
        )
        # 重复签到同样会返回 HTTP 403 + code=10001
        if resp.status_code == 403:
            try:
                j = resp.json()
            except Exception:
                raise RuntimeError(f"[score/checkin] HTTP 403: {resp.text[:200]}")
            msg = j.get("message", "")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[score/checkin] HTTP 403: {resp.text[:200]}")
        data = self._check(resp, "score/checkin")
        if data.get("code") != 0:
            msg = data.get("message", "")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[score/checkin] 失败: {msg} (code={data.get('code')})")
        return True

    def sign_attendance(self, cred: str, token: str, game_id, binding: dict, uid: str = None) -> bool:
        """⑦ 签到（按游戏区分端点与参数）。

        返回 True=签到成功; False=已签到(不算失败); 抛异常=失败。
        - arknights(1):  POST /api/v1/game/attendance          body={gameId, uid}
        - endfield(3):   POST /api/v1/game/endfield/attendance body={gameId, serverId, roleId}
        """
        if game_id == 3:
            path = "/api/v1/game/endfield/attendance"
            dr = binding.get("defaultRole") or {}
            body = {"gameId": game_id, "serverId": dr.get("serverId"), "roleId": dr.get("roleId")}
            if not body["serverId"] or not body["roleId"]:
                raise RuntimeError("[game/endfield/attendance] 无 defaultRole（角色未绑定），无法签到")
        else:
            path = "/api/v1/game/attendance"
            body = {"gameId": game_id, "uid": uid}

        # 必须用紧凑 JSON（无空格）作为签名体, 且发送时用 data= 原样发送
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        resp = self.session.post(
            f"{ZONAI_HOST}{path}",
            headers=self._sk_headers(token=token, cred=cred, path=path, body_or_query=body_str),
            data=body_str,
            timeout=self.timeout,
        )

        # 已签到/重复签到时服务端返回 HTTP 403 + code=10001, 在此处理而不是走 _check_resp
        if resp.status_code == 403:
            try:
                j = resp.json()
            except Exception:
                raise RuntimeError(f"[game/attendance] HTTP 403: {resp.text[:200]}")
            msg = j.get("message", "")
            if is_duplicate_message(msg):
                return False  # 视为"已签到"（不算失败）
            raise RuntimeError(f"[game/attendance] HTTP 403: {resp.text[:200]}")

        data = self._check(resp, "game/attendance")
        if data.get("code") != 0:
            msg = data.get("message", "")
            if is_duplicate_message(msg):
                return False
            raise RuntimeError(f"[game/attendance] 失败: {msg} (code={data.get('code')})")
        return True

    def get_awards_text(self, resp: requests.Response) -> str:
        """从签到响应 data.awards 汇总奖励文本"""
        try:
            data = resp.json()
        except Exception:
            return "无"
        awards = []
        for a in data.get("data", {}).get("awards", []):
            name = a.get("resource", {}).get("name", "未知")
            count = a.get("count", 1)
            awards.append(f"{name}x{count}")
        return ", ".join(awards) if awards else "无"