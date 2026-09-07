"""src/api - 森空岛 API 通信层 (纯 Python, 可独立复用)

- client.py    : SklandClient (httpx 异步客户端, Web 版签名)
- did.py       : 官方设备中心 dId 生成 (DES/AES/RSA)
- ua_pool.py   : 真实浏览器 UA 池 (data/browsers.jsonl)
- device_pool.py: 安卓机型池 (展示备用)
"""
