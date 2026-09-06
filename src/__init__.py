"""src - AMA-10 Entertainment Skland 业务逻辑包

- client.py     : 森空岛 API 客户端 (签名/登录链路/签到)
- device_pool.py: 安卓机型池加载与随机选取
- storage.py    : 凭据与推送目标存储
- service.py    : 认证/签到业务服务 (登录、签到汇总、自动签到)
"""

from .client import (
    SklandClient,
    dict_to_fingerprint,
    fingerprint_to_dict,
    gen_sign,
)
from .service import SklandService
from .storage import Storage

__all__ = [
    "SklandClient",
    "gen_sign",
    "fingerprint_to_dict",
    "dict_to_fingerprint",
    "SklandService",
    "Storage",
]