"""src - AMA-10 Entertainment Skland 业务逻辑包 (v0.5.1)

分层结构 (API 通信层与业务/存储解耦, 便于复用):
- api/      : 森空岛 API 通信层 (纯 Python)
    client.py     森空岛 API 客户端 (Web 版签名/登录链路/签到)
    did.py        官方设备中心 dId 生成 (DES/AES/RSA 指纹上报)
    ua_pool.py    真实浏览器 UA 池加载与随机选取 (browsers.jsonl)
    device_pool.py 安卓机型池 (展示备用, 已不参与请求头)
- services/ : 业务层 (纯 Python)
    service.py    登录/签到/登录态校验 + 自动签到调度器
- storage/  : 存储层
    storage.py    凭据与推送目标存储
- utils/    : 工具层
    recall.py     消息自动撤回 + LLM 阻断辅助
"""

from .api.client import (
    CredExpiredError,
    SklandClient,
    gen_sign,
    is_duplicate_message,
)
from .services.service import SklandService
from .storage.storage import Storage

__all__ = [
    "CredExpiredError",
    "SklandClient",
    "gen_sign",
    "is_duplicate_message",
    "SklandService",
    "Storage",
]