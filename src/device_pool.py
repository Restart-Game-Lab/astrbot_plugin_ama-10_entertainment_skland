"""src/device_pool.py - 安卓机型池加载与随机选取

数据源: 插件根目录 data/devices.json (bsthen/device-models, Apache-2.0)
  结构: { "<ro.product.model>": {"brand": "...", "name": "..."} }
  本插件只取下列 11 个品牌的黑名单过滤后的机型, 用作设备指纹的 model。

说明:
  - 惰性加载 + 模块级缓存: 首次调用时读取并过滤一次, 之后复用内存结果,
    避免每次生成指纹都重读 2.9MB 文件。
  - 品牌过滤处理了数据中的拼写/杂质坑:
      * vivo 存在 "Vivo"/"vivo" 两种拼写 -> 按 brand.lower() 合并
      * realme 存在 "Realme"/"realme"/"Realme Techlife" -> 按 lower() 合并但
        剔除 "Realme Techlife"(智能家居, 非手机)
      * ZTE 品牌下混入 "Aaztec"/"PRAZteck"(仅含 "ZTE" 子串) -> 需精确匹配 "ZTE"
      * Motorola 剔除 "Motorola Solutions"
      * Google 剔除模拟器条目 (名称含 Emulator/AOSP/Chromebook)
  若 data/devices.json 缺失或解析失败, 回退到内置 FALLBACK_DEVICES 手工池,
  保证插件不因机型库问题而崩溃。
"""

import json
import random
from pathlib import Path

# 数据文件位于插件根目录 data/devices.json
_DEVICE_JSON = Path(__file__).resolve().parent.parent / "data" / "devices.json"

# 取用的品牌白名单(与用户指定一致)
BRAND_WHITELIST = (
    "xiaomi", "oppo", "vivo", "huawei", "samsung",
    "redmi", "realme", "motorola", "zte", "meizu", "google",
)

# 排除的 brand 全名(大小写不敏感精确匹配后剔除)
_EXCLUDE_BRANDS = {"realme techlife", "motorola solutions"}

# 名称中包含这些关键词的条目视为模拟器/非手机, 剔除
_EXCLUDE_NAME_KEYWORDS = ("emulator", "aosp", "chromebook", "gcar", "virtual")


def _is_allowed(brand: str, name: str) -> bool:
    """判断一条设备是否符合品牌白名单且非模拟器。"""
    b = (brand or "").strip().lower()
    if b not in BRAND_WHITELIST:
        return False
    if b in _EXCLUDE_BRANDS:
        return False
    n = (name or "").lower()
    if any(k in n for k in _EXCLUDE_NAME_KEYWORDS):
        return False
    return True


def _load_pool() -> list[dict]:
    """从 data/devices.json 读取并过滤出可用机型池。

    返回: [{"model", "brand", "name"}, ...]
    """
    try:
        raw = json.loads(_DEVICE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    pool = []
    for model, info in raw.items():
        brand = info.get("brand", "")
        name = info.get("name", "") or model
        if not _is_allowed(brand, name):
            continue
        pool.append({
            "model": str(model),
            "brand": brand,       # 展示用
            "name": name,         # 展示用(营销名)
        })
    return pool


# 兜底手工池(与旧 client.py 一致, 保证文件缺失时也能用)
FALLBACK_DEVICES = [
    {"model": "24129PN74C", "brand": "Xiaomi", "name": "Xiaomi 14 Pro"},
    {"model": "23127PN0CC", "brand": "Xiaomi", "name": "Xiaomi 14"},
    {"model": "2407FPN8DC", "brand": "Xiaomi", "name": "Redmi K70"},
    {"model": "Redmi K70 Pro", "brand": "Redmi", "name": "Redmi K70 Pro"},
    {"model": "CPH2585", "brand": "Oppo", "name": "OPPO Find X8"},
    {"model": "V2310A", "brand": "vivo", "name": "vivo X100"},
    {"model": "SM-S9180", "brand": "Samsung", "name": "Galaxy S23 Ultra"},
    {"model": "M2012K11AC", "brand": "Xiaomi", "name": "Xiaomi 10 Ultra"},
]

# 模块级缓存(惰性填充)
_pool_cache: list[dict] | None = None


def get_pool() -> list[dict]:
    """返回过滤后的机型池(缓存)。若 data 缺失返回空列表。"""
    global _pool_cache
    if _pool_cache is None:
        _pool_cache = _load_pool()
        if _pool_cache:
            print(f"[device_pool] 已加载机型池: {len(_pool_cache)} 台 (来自 {_DEVICE_JSON.name})")
        else:
            print("[device_pool] data/devices.json 缺失或为空, 使用内置兜底池")
    return _pool_cache


def random_device() -> dict | None:
    """随机返回一台机型 {model, brand, name}; 数据缺失时回退内置兜底池。"""
    pool = get_pool()
    if not pool:
        return random.choice(FALLBACK_DEVICES)
    return random.choice(pool)