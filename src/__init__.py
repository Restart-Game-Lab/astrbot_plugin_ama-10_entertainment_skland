"""src - AMA-10 Entertainment Skland 业务逻辑包 (v0.5.0)

- client.py     : 森空岛 API 客户端 (Web 版签名/登录链路/签到)
- did.py        : 官方设备中心 dId 生成 (DES/AES/RSA 指纹上报)
- ua_pool.py    : 真实浏览器 UA 池加载与随机选取 (browsers.jsonl)
- device_pool.py: 安卓机型池(展示备用, 已不参与请求头)
- storage.py    : 凭据与推送目标存储
- service.py    : 认证/签到业务服务 (登录、签到汇总、自动签到)
"""

from .client import (
    CredExpiredError,
    SklandClient,
    gen_sign,
    is_duplicate_message,
)
from .service import SklandService
from .storage import Storage

__all__ = [
    "CredExpiredError",
    "SklandClient",
    "gen_sign",
    "is_duplicate_message",
    "SklandService",
    "Storage",
]