"""src/ua_pool.py - 真实浏览器 User-Agent 池加载与随机选取

数据源: 插件根目录 data/browsers.jsonl (fake-useragent v2.2.0, Apache-2.0,
        数据来自 user-agents.net 实时统计)
  格式: JSONL, 每行一个 UA:
    {"useragent": "...", "percent": 0.15, "type": "mobile",
     "device_brand": "Apple", "browser": "Mobile Safari",
     "browser_version": "18.3.1", "os": "iOS", "os_version": "18.3.2", ...}

说明:
  - 惰性加载 + 模块级缓存: 首次调用时读取并过滤一次, 之后复用内存结果。
  - 只取 Android 系浏览器白名单, 因为森空岛 Web 版 UA 需带移动端浏览器特征
    (App 版证书校验更严, Web 版 platform=3 配移动浏览器 UA 通过率最高)。
  - 按 percent 加权随机(random.choices): 用户占比高的 UA 更真实(更像真人)。
  - min_version 过滤: 去掉过旧版本, 防 UA 太老被风控(旧的 Chrome Mobile
    UA 出现在网络日志中容易被识别为低质量流量)。
  - 数据文件缺失或解析失败 -> 回退 Azincc 硬编码 UA(内置兜底, 保证可用)。

与 device_pool.py 的关系: 机型池(devices.json)在 Web 版方案中只用于展示,
UA 池(browsers.jsonl)才是请求头 user-agent 的真实来源; 两者互相独立。
"""

import json
import random
from pathlib import Path

# 数据文件位于插件根目录 data/browsers.jsonl
_UA_JSON = Path(__file__).resolve().parent.parent / "data" / "browsers.jsonl"

# 浏览器白名单(仅 Android 系; 桌面系 UA 与森空岛 App 行为不符, 弃用)
BROWSER_WHITELIST = (
    "Chrome Mobile",       # 主流: 全球 Android 浏览器份额 90%+
    "Samsung Internet",    # 三星自家浏览器, 真实存在
    "Edge Mobile",         # 微软移动浏览器
    "Opera Mobile",        # 欧朋移动浏览器
    "Firefox Mobile",      # 火狐移动
    "DuckDuckGo Mobile",
)

# 最小浏览器版本(去掉过旧 UA, 避免风控)。Chrome Mobile 112+ / 其余 20+。
# 依据 user-agents.net 数据: Chrome Mobile 120+ 份额占绝对主流。
MIN_BROWSER_VERSION = 112.0


def _version_key(browser_version: str) -> float:
    """浏览器版本字符串 -> 可比较 float (解析失败返回 0)"""
    try:
        return float(str(browser_version).split(".")[0])
    except Exception:
        return 0.0


def _is_allowed(entry: dict) -> bool:
    """判断一条 UA 是否符合白名单 + 版本过滤"""
    if entry.get("type") != "mobile":
        return False  # 只取 mobile (森空岛是移动游戏社区)
    if "Android" not in (entry.get("useragent") or ""):
        return False  # 必须带 Android 标识
    browser = entry.get("browser") or ""
    if browser not in BROWSER_WHITELIST:
        return False
    if _version_key(entry.get("browser_version", "")) < MIN_BROWSER_VERSION:
        return False
    return True


def _load_pool() -> list[dict]:
    """从 data/browsers.jsonl 读取并过滤出可用 UA 池。

    返回: [{"useragent", "percent", "browser", "browser_version", "os", ...}, ...]
    """
    if not _UA_JSON.is_file():
        return []
    pool = []
    try:
        with open(_UA_JSON, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if _is_allowed(entry):
                    pool.append(entry)
    except Exception:
        return []
    return pool


# 兜底 UA 池(文件缺失或过滤后为空时使用, 与 Azincc 原版一致)
FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 12; SM-A5560 Build/V417IR; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Safari/537.36 SKLand/1.52.1",
]

# 惰性加载缓存
_pool_cache: list[dict] | None = None


def random_user_agent() -> str:
    """从 UA 池按 percent 加权随机取一条, 返回 useragent 字符串。

    兜底: 池为空(文件缺失/解析失败/过滤为空)时返回 FALLBACK_USER_AGENTS[0]。
    """
    global _pool_cache
    if _pool_cache is None:
        _pool_cache = _load_pool()
    if not _pool_cache:
        return FALLBACK_USER_AGENTS[0]

    weights = [max(float(e.get("percent") or 0.0), 0.01) for e in _pool_cache]
    chosen = random.choices(_pool_cache, weights=weights, k=1)[0]
    ua = chosen.get("useragent", "")
    # 末尾补 SKLand 客户端标识(与 Azincc 一致, 服务端可识别为森空岛客户端)
    if "SKLand" not in ua:
        ua = f"{ua} SKLand/1.52.1"
    return ua
