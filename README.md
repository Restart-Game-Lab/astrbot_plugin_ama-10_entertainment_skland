# AMA-10 Entertainment Skland

<div align="center">

<img src="https://count.getloli.com/@preca-hoshino?name=ama-10_entertainment_skland&theme=rule34&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt="Moe Counter">

**森空岛（Skland）每日签到插件** — 手机验证码登录，自动为绑定的鹰角游戏签到。

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
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
- **设备指纹复用** — 登录时从 4 万+ 真实机型池随机取一台，之后全程复用同一指纹
- **防重复识别** — "已签到/请勿重复"等状态自动识别，不视为失败

## 安装

将本插件目录放置到 AstrBot 的 `plugins/` 目录，或在 AstrBot WebUI 中通过 Git 仓库安装：

```
https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland
```

然后重载插件即可（需安装 `requests`）。

## 使用方法

命令组 `/skland`（均为单人指令，只作用于当前用户自己）：

| 命令 | 说明 |
| --- | --- |
| `/skland login <手机号>` | 发送短信验证码，并在 60 秒内等待用户回复验证码完成登录（超时自动结束流程）。验证码格式错误会提示重发 |
| `/skland checkin` | 用缓存凭据为当前用户签到所有绑定游戏 + 论坛版块（顶部显示登录设备） |
| `/skland status` | 查看当前用户已绑定手机号、登录设备与自动签到/游戏/论坛配置 |
| `/skland logout` | 清除当前用户的全部凭据（并释放手机号占用） |

之后每天执行 `/skland checkin`（或开启自动签到）即可完成当日签到；cred 失效时插件会提示重新登录。

登录交互：`/skland login 18600000000` 后，插件发送验证码，**60 秒内直接回复 6 位数字**即可完成登录（无需再输入 `/skland` 前缀）。

## 自动签到配置

在插件配置中可设置：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `auto_checkin_enabled` | 自动签到总开关，关闭后即使设置了签到时间也不会自动签到 | `true` |
| `auto_checkin_time` | 每日自动签到时刻（`HH:MM`，24 小时制），留空或非法则关闭 | `08:00` |
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
│   └── devices.json   # 安卓机型库(4 万+ 台, bsthen/device-models)
└── src/               # 业务逻辑层(与 AstrBot 解耦)
    ├── client.py      # 森空岛 API 客户端(签名/登录链路/签到/指纹)
    ├── device_pool.py # 机型池加载与随机选取
    ├── storage.py     # 凭据与手机号占用存储(含指纹)
    └── service.py     # 登录/签到业务服务 + 自动签到调度器
```

（凭据数据运行时生成于 `data/plugin_data/astrbot_plugin_ama_10_entertainment_skland/`）

## 许可证

本项目源代码基于 [GNU AGPL-3.0](LICENSE) 许可证开源。

> **致谢**：机型数据来自 [bsthen/device-models](https://github.com/bsthen/device-models)（Apache-2.0，每日从 Google Play 设备列表自动同步）。