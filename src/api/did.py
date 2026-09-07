"""src/did.py - 森空岛官方设备中心 dId 生成

参考 Azincc/astrbot_plugin_skland (skland_api.py) 移植的 Rust 官方客户端协议:
  设备 ID 由官方设备中心 (fp-it.portal101.cn/deviceprofile/v4) 签发, 而非本地随机。
  本地随机的 dId 会被服务端识别为无效/风控 (code=10001「设备信息无效」)。

流程:
  1. 生成 UUID + priId(桌面密钥)
  2. RSA 加密 UUID -> ep (公钥来自官方客户端常量)
  3. 组装浏览器指纹 (BROWSER_ENV: canvas/timezone/plugins/res/screen 等)
     + DES_TARGET (appId/organization/os/version/sdkver...)
  4. 按 DES_RULE 逐字段 DES 加密 (ECB, key 来自常量表)
  5. 计算 tn (排序字段值拼接 + md5)
  6. gzip 压缩 + AES-CBC 加密 -> data (iv 固定 0102030405060708)
  7. POST 上报 -> 返回 B{deviceId} 格式 dId

依赖: pycryptodome (AES/DES/RSA/PKCS1_v1_5)

持久化约定: 生成的 dId 按用户缓存到 auth.json 的 "did" 字段(登录时生成一次,
整机复用), 与 UA 轮换解耦 —— dId 恒定, UA 可在登录时随机一次后持久化。
"""

import base64
import gzip
import hashlib
import json
import time
import uuid

from Crypto.Cipher import AES, DES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

# ============================ 常量(来自官方客户端/Rust 实现) ============================

# DES 加密规则: 字段名 -> {cipher, is_encrypt, key, obfuscated_name}
DES_RULE = {
    "appId": {"cipher": "DES", "is_encrypt": 1, "key": "uy7mzc4h", "obfuscated_name": "xx"},
    "box": {"is_encrypt": 0, "obfuscated_name": "jf"},
    "canvas": {"cipher": "DES", "is_encrypt": 1, "key": "snrn887t", "obfuscated_name": "yk"},
    "clientSize": {"cipher": "DES", "is_encrypt": 1, "key": "cpmjjgsu", "obfuscated_name": "zx"},
    "organization": {"cipher": "DES", "is_encrypt": 1, "key": "78moqjfc", "obfuscated_name": "dp"},
    "os": {"cipher": "DES", "is_encrypt": 1, "key": "je6vk6t4", "obfuscated_name": "pj"},
    "platform": {"cipher": "DES", "is_encrypt": 1, "key": "pakxhcd2", "obfuscated_name": "gm"},
    "plugins": {"cipher": "DES", "is_encrypt": 1, "key": "v51m3pzl", "obfuscated_name": "kq"},
    "pmf": {"cipher": "DES", "is_encrypt": 1, "key": "2mdeslu3", "obfuscated_name": "vw"},
    "protocol": {"is_encrypt": 0, "obfuscated_name": "protocol"},
    "referer": {"cipher": "DES", "is_encrypt": 1, "key": "y7bmrjlc", "obfuscated_name": "ab"},
    "res": {"cipher": "DES", "is_encrypt": 1, "key": "whxqm2a7", "obfuscated_name": "hf"},
    "rtype": {"cipher": "DES", "is_encrypt": 1, "key": "x8o2h2bl", "obfuscated_name": "lo"},
    "sdkver": {"cipher": "DES", "is_encrypt": 1, "key": "9q3dcxp2", "obfuscated_name": "sc"},
    "status": {"cipher": "DES", "is_encrypt": 1, "key": "2jbrxxw4", "obfuscated_name": "an"},
    "subVersion": {"cipher": "DES", "is_encrypt": 1, "key": "eo3i2puh", "obfuscated_name": "ns"},
    "svm": {"cipher": "DES", "is_encrypt": 1, "key": "fzj3kaeh", "obfuscated_name": "qr"},
    "time": {"cipher": "DES", "is_encrypt": 1, "key": "q2t3odsk", "obfuscated_name": "nb"},
    "timezone": {"cipher": "DES", "is_encrypt": 1, "key": "1uv05lj5", "obfuscated_name": "as"},
    "tn": {"cipher": "DES", "is_encrypt": 1, "key": "x9nzj1bp", "obfuscated_name": "py"},
    "trees": {"cipher": "DES", "is_encrypt": 1, "key": "acfs0xo4", "obfuscated_name": "pi"},
    "ua": {"cipher": "DES", "is_encrypt": 1, "key": "k92crp1t", "obfuscated_name": "bj"},
    "url": {"cipher": "DES", "is_encrypt": 1, "key": "y95hjkoo", "obfuscated_name": "cf"},
    "version": {"is_encrypt": 0, "obfuscated_name": "version"},
    "vpw": {"cipher": "DES", "is_encrypt": 1, "key": "r9924ab5", "obfuscated_name": "ca"},
}

DES_TARGET = {
    "protocol": 102,
    "organization": "UWXspnCCJN4sfYlNfqps",
    "appId": "default",
    "os": "web",
    "version": "3.0.0",
    "sdkver": "3.0.0",
    "box": "",
    "rtype": "all",
    "subVersion": "1.0.0",
    "time": 0,
}

BROWSER_ENV = {
    "plugins": (
        "MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,"
        "MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1"
    ),
    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "canvas": "259ffe69",
    "timezone": -480,
    "platform": "Win32",
    "url": "https://www.skland.com/",
    "referer": "",
    "res": "1920_1080_24_1.25",
    "clientSize": "0_0_1080_1920_1920_1080_1920_1080",
    "status": "0011",
}

RSA_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCmxMNr7n8ZeT0tE1R9j/mPixoinPkeM+k4VGIn/"
    "s0k7N5rJAfnZ0eMER+QhwFvshzo0LNmeUkpR8uIlU/GEVr8mN28sKmwd2gpygqj0ePnBmOW4v0ZVwbSYK"
    "+izkhVFk2V/doLoMbWy6b+UnA8mkjvg0iYWRByfRsK2gdl7llqCwIDAQAB"
)


def _des_encrypt(key: bytes, data: bytes) -> bytes:
    """DES 加密 (ECB, 8 字节对齐, null padding)"""
    padding_len = 8 - (len(data) % 8)
    padded_data = data + (b"\x00" * padding_len)
    key_8 = key[:8].ljust(8, b"\x00")
    cipher = DES.new(key_8, DES.MODE_ECB)
    result = b""
    for i in range(0, len(padded_data), 8):
        block = padded_data[i : i + 8]
        result += cipher.encrypt(block)
    return result


def _apply_des_rules(data: dict) -> dict:
    """按 DES_RULE 对字段加密, 输出 obfuscated_name 键"""
    result = {}
    for key, value in data.items():
        str_value = str(value) if not isinstance(value, str) else value
        rule = DES_RULE.get(key)
        if rule:
            if rule.get("is_encrypt") == 1:
                des_key = rule["key"].encode("utf-8")
                encrypted = _des_encrypt(des_key, str_value.encode("utf-8"))
                result[rule["obfuscated_name"]] = base64.b64encode(encrypted).decode()
            else:
                result[rule["obfuscated_name"]] = value
        else:
            result[key] = value
    return result


def _get_tn(data: dict) -> str:
    """生成 tn hash 输入: 排序字段拼接, int 值 *10000"""
    sorted_keys = sorted(data.keys())
    result = ""
    for key in sorted_keys:
        value = data[key]
        if isinstance(value, int):
            result += str(value * 10000)
        elif isinstance(value, dict):
            result += _get_tn(value)
        else:
            result += str(value) if value else ""
    return result


def _aes_encrypt(data: bytes, key: bytes) -> str:
    """AES-128-CBC 加密 (iv 固定 0102030405060708), 输出 hex"""
    encoded_b64 = base64.b64encode(data)
    pad_len = 16 - (len(encoded_b64) % 16)
    if pad_len < 16:
        encoded_b64 += b"\x00" * pad_len
    iv = b"0102030405060708"
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(encoded_b64, 16)
    encrypted = cipher.encrypt(padded)
    return encrypted.hex()


def _get_smid() -> str:
    """生成 smid (设备指纹标识)"""
    time_str = time.strftime("%Y%m%d%H%M%S")
    uid = str(uuid.uuid4())
    v = f"{time_str}{hashlib.md5(uid.encode()).hexdigest()}00"
    smsk_web = hashlib.md5(f"smsk_web_{v}".encode()).digest()
    suffix = smsk_web[:7].hex()
    return f"{v}{suffix}0"


async def get_device_id(timeout: float = 15.0) -> str:
    """向官方设备中心申请设备 ID, 返回 B{deviceId} 格式。

    幂等逻辑由调用方负责(登录时生成一次, 持久化到 auth.json)。

    异常: 设备中心不可达/返回异常时抛 RuntimeError(调用方决定是否回退)。
    """
    import httpx

    # 生成 UUID 和 priId
    uid = str(uuid.uuid4())
    pri_id_hash = hashlib.md5(uid.encode()).digest()[:8]
    pri_id_hex = pri_id_hash.hex()

    # RSA 加密 UUID -> ep
    public_key_der = base64.b64decode(RSA_PUBLIC_KEY)
    rsa_key = RSA.import_key(public_key_der)
    cipher_rsa = PKCS1_v1_5.new(rsa_key)
    encrypted_uid = cipher_rsa.encrypt(uid.encode())
    ep_base64 = base64.b64encode(encrypted_uid).decode()

    # 组装浏览器指纹
    in_ms = int(time.time() * 1000)
    browser = dict(BROWSER_ENV)
    browser["vpw"] = str(uuid.uuid4())
    browser["trees"] = str(uuid.uuid4())
    browser["svm"] = in_ms
    browser["pmf"] = in_ms

    # 组装 target 数据
    des_target = dict(DES_TARGET)
    des_target["smid"] = _get_smid()
    des_target.update(browser)

    # 生成 tn
    tn_input = _get_tn(des_target)
    des_target["tn"] = hashlib.md5(tn_input.encode()).hexdigest()

    # DES 加密 + 压缩
    des_result = _apply_des_rules(des_target)
    json_str = json.dumps(des_result, separators=(",", ":"))
    compressed = gzip.compress(json_str.encode(), compresslevel=2)

    # AES 加密
    encrypted = _aes_encrypt(compressed, pri_id_hex.encode())

    # 请求设备中心
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://fp-it.portal101.cn/deviceprofile/v4",
            json={
                "appId": "default",
                "compress": 2,
                "data": encrypted,
                "encode": 5,
                "ep": ep_base64,
                "organization": "UWXspnCCJN4sfYlNfqps",
                "os": "web",
            },
        )
        response = resp.json()

    if response.get("code") != 1100:
        raise RuntimeError(f"设备 ID 生成失败: {response}")

    return f"B{response['detail']['deviceId']}"
