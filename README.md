# AMA-10 Entertainment Skland

<div align="center">

<img src="logo.png" alt="logo" width="120">

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
[![Author](https://img.shields.io/badge/Author-preca--hoshino-blue)](https://github.com/preca-hoshino)
[![Repo](https://img.shields.io/badge/repo-Restart--Game--Lab-blue)](https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland)

[中文](README.md) | [English](README_EN.md) | [日本語](README_JA.md)

</div>

---

## 简介

`astrbot_plugin_ama-10_entertainment_skland` 是一个基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的森空岛签到插件。
通过短信验证码登录森空岛获取 `cred`，为所有绑定的游戏（明日方舟、终末地等）逐一执行每日签到。

## 特性

- **验证码登录** — 短信验证码完成登录，cred 持久化复用，一人一号
- **每日自动签到** — 游戏 + 论坛双签到，随机延迟防风控，结果静默仅记日志
- **官方设备中心指纹** — 设备 ID (dId) 由官方设备中心签发并持久化复用；浏览器 UA 从真实 UA 池随机选取（登录时一次，之后恒定）
- **Web 版稳定 API** — 森空岛业务接口走 Web 版签名 (platform=3)，支持明日方舟逐角色/终末地逐角色签到；内置重试与错误分类（登录过期自动提示重登）
- **防重复识别** — "已签到/请勿重复"等状态自动识别，不视为失败

## 安装

将本插件目录放置到 AstrBot 的 `plugins/` 目录，或在 AstrBot WebUI 中通过 Git 仓库安装：

```
https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland
```

然后重载插件即可（需安装 `httpx`、`pycryptodome`、`APScheduler`，AstrBot 会自动安装）。

## 使用方法

命令组 `/skland`（均为单人指令，只作用于当前用户自己）：

| 命令 | 说明 |
| --- | --- |
| `/skland login <手机号>` | 发送短信验证码，并在 60 秒内等待用户回复验证码完成登录（超时自动结束流程）。验证码格式不合法则静默忽略（不提示），合法则自动登录并展示登录用户 |
| `/skland checkin` | 用缓存凭据为当前用户签到所有绑定游戏 + 论坛版块（显示游戏/论坛结果分组） |
| `/skland status` | 查看当前用户绑定用户与自动签到/游戏/论坛配置 |
| `/skland logout` | 清除当前用户的全部凭据（并释放手机号占用） |

之后每天执行 `/skland checkin`（或开启自动签到）即可完成当日签到；cred 失效时插件会提示重新登录。

登录交互：`/skland login 18600000000` 后，插件发送验证码，**60 秒内直接回复 6 位数字**即可完成登录（无需再输入 `/skland` 前缀）。

## 自动签到配置

在插件配置中可设置：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `auto_checkin_enabled` | 自动签到总开关，关闭后即使设置了签到时间也不会自动签到 | `true` |
| `auto_checkin_time` | 每日自动签到 cron 表达式（5 段：分 时 日 月 周），留空或非法则关闭 | `0 6 * * *` |
| `random_delay_seconds` | 在目标时刻后随机延迟 0~N 秒再签到，避免固定时刻触发风控 | `1200` |
| `request_timeout` | HTTP 请求超时（秒） | `15` |
| `game_checkin_enabled` | 是否启用游戏签到 | `true` |
| `forum_checkin_enabled` | 是否启用论坛版块签到 | `true` |

自动签到为**静默模式**：结果不推送任何消息，仅记录日志；重载插件或重启 AstrBot 后按新配置生效。

## 目录结构

```
astrbot_plugin_ama-10_entertainment_skland/
├── main.py            # 插件入口: /skland 命令组 + 自动签到编排
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # 插件配置(自动签到/超时/开关)
├── requirements.txt   # 依赖
├── data/
│   ├── browsers.jsonl # 真实浏览器 UA 池(fake-useragent, Apache-2.0)
│   └── devices.json   # 安卓机型库(4 万+ 台, bsthen/device-models, 展示备用)
└── src/               # 业务逻辑层(与 AstrBot 解耦, 按层划分)
    ├── api/           # API 通信层(纯 Python, 可独立复用)
    │   ├── client.py      # 森空岛 API 客户端(Web 版签名/登录链路/签到)
    │   ├── did.py         # 官方设备中心 dId 生成(DES/AES/RSA)
    │   ├── ua_pool.py     # 真实浏览器 UA 池加载与随机选取
    │   └── device_pool.py # 安卓机型池(展示备用)
    ├── services/      # 业务层(纯 Python)
    │   └── service.py     # 登录/签到/登录态校验 + 自动签到调度器
    ├── storage/       # 存储层
    │   └── storage.py     # 凭据与手机号占用存储(按用户隔离)
    └── utils/         # 工具层
        └── recall.py      # 消息自动撤回 + LLM 阻断辅助
```

（凭据数据运行时生成于 `data/plugin_data/astrbot_plugin_ama_10_entertainment_skland/`）

## 许可证

本项目源代码基于 [GNU AGPL-3.0](LICENSE) 许可证开源。

> **致谢**：机型数据来自 [bsthen/device-models](https://github.com/bsthen/device-models)（Apache-2.0，每日从 Google Play 设备列表自动同步）。