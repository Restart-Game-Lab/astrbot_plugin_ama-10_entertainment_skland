# AMA-10 Entertainment Skland

<div align="center">

<img src="https://count.getloli.com/@astrbot_plugin_ama_10_entertainment_skland?theme=rule34&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt="Moe Counter">

**Skland Daily Sign-in Plugin** — Log in with a phone verification code, auto sign in for all bound Hypergryph games.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
[![Repo](https://img.shields.io/badge/repo-Restart--Game--Lab-blue)](https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland)

[中文](README.md) | [English](README_EN.md) | [日本語](README_JA.md)

</div>

---

## Introduction

`astrbot_plugin_ama-10_entertainment_skland` is an [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin for Skland daily sign-in.
It logs into Skland via SMS verification code to obtain `cred`, then signs in for every bound game (Arknights, Endfield, etc.).

## Features

- **SMS-code login** — Log in with an SMS verification code; `cred` is persisted and reused, one phone number per user
- **Daily auto sign-in** — Game + forum sign-in with random delay to avoid risk control; results are logged silently
- **Persistent device fingerprint** — On login, a device is picked from a 40k+ real-device pool and reused afterward
- **Duplicate-sign-in aware** — "Already signed in / duplicate" states are not treated as failures

## Installation

Place this folder into AstrBot's `plugins/` directory, or install via Git in the AstrBot WebUI:

```
https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland
```

Then reload the plugin (`requests` required).

## Usage

Command group `/skland`:

| Command | Description |
| --- | --- |
| `/skland login <phone>` | Send an SMS verification code, then wait for the user to reply with the code within 60s to finish login (times out automatically). Invalid codes prompt a resend |
| `/skland checkin` | Sign in for all bound games + forum sections for the current user using cached credentials (shows the login device at top) |
| `/skland status` | Show the current user's bound phone, login device and auto sign-in / game / forum config |
| `/skland logout` | Remove all credentials of the current user (and release the phone number) |

All commands are per-user and only affect the current user. Then just run `/skland checkin` daily (or enable auto sign-in); when `cred` expires the plugin will ask you to log in again.

Login interaction: after `/skland login 18600000000`, the plugin sends an SMS code — **reply with the 6-digit code within 60 seconds** (no `/skland` prefix needed).

## Auto Sign-in Config

| Config | Description | Default |
| --- | --- | --- |
| `auto_checkin_enabled` | Master switch for auto sign-in. If off, no auto sign-in even with a time set | `true` |
| `auto_checkin_time` | Daily auto sign-in time (`HH:MM`, 24h). Empty or invalid disables. | `08:00` |
| `random_delay_seconds` | Random delay 0~N seconds after the target time to avoid risk control | `1200` |
| `request_timeout` | HTTP request timeout (seconds) | `15` |
| `game_checkin_enabled` | Enable game sign-in | `true` |
| `forum_checkin_enabled` | Enable forum section sign-in | `true` |

Auto sign-in is **silent**: results are logged only, no messages are pushed. Reload the plugin or restart AstrBot to apply config changes.

## Directory Structure

```
astrbot_plugin_ama-10_entertainment_skland/
├── main.py            # Plugin entry: /skland command group + auto sign-in orchestration
├── metadata.yaml      # Plugin metadata
├── _conf_schema.json  # Plugin config (auto sign-in / timeout / toggles)
├── requirements.txt   # Dependencies
├── data/
│   └── devices.json   # Android device database (40k+ devices, bsthen/device-models)
└── src/               # Business logic layer (decoupled from AstrBot)
    ├── client.py      # Skland API client (signature / login flow / sign-in / fingerprint)
    ├── device_pool.py # device-pool loading & random selection
    ├── storage.py     # credential & phone-occupancy storage (incl. fingerprint)
    └── service.py     # login/sign-in service + auto sign-in scheduler
```

（Credentials are generated at runtime under `data/plugin_data/astrbot_plugin_ama_10_entertainment_skland/`）

## License

This project is open-sourced under the [GNU AGPL-3.0](LICENSE) license.

> **Acknowledgments**: Device data from [bsthen/device-models](https://github.com/bsthen/device-models) (Apache-2.0, auto-synced daily from Google Play's supported-devices list).