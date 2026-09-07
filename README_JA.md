# AMA-10 Entertainment Skland

<div align="center">

<img src="https://count.getloli.com/@astrbot_plugin_ama_10_entertainment_skland?theme=rule34&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt="Moe Counter">

**Skland（森空岛）デイリーサインイン プラグイン** — 携帯の SMS 認証コードでログインし、連携中のハイパーグリフゲームへ自動でサインインします。

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-green)
[![Repo](https://img.shields.io/badge/repo-Restart--Game--Lab-blue)](https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland)

[中文](README.md) | [English](README_EN.md) | [日本語](README_JA.md)

</div>

---

## 概要

`astrbot_plugin_ama-10_entertainment_skland` は [AstrBot](https://github.com/AstrBotDevs/AstrBot) 向けの Skland サインインプラグインです。
SMS 認証コードで Skland にログインして `cred` を取得し、連携中のすべてのゲーム（アークナイツ、エンドフィールドなど）へ毎日サインインします。

## 特徴

- **SMS 認証ログイン** — SMS 認証コードでログイン。`cred` を永続化して再利用、ユーザーごとに 1 つの電話番号
- **毎日自動サインイン** — ゲーム + フォーラムの両方にサインイン。ランダム遅延でリスク管理を回避し、結果はログのみ
- **デバイスフィンガープリントを永続化** — ログイン時に 4 万台以上の実機プールから選んだデバイスを以後も再利用
- **二重サインイン自動検出** — 「済み/重複」などの状態は失敗とみなさない

## インストール

このフォルダを AstrBot の `plugins/` ディレクトリに配置するか、AstrBot WebUI から Git リポジトリでインストールします：

```
https://github.com/Restart-Game-Lab/astrbot_plugin_ama-10_entertainment_skland
```

その後プラグインをリロードしてください（`requests` が必要）。

## 使い方

コマンドグループ `/skland`：

| コマンド | 説明 |
| --- | --- |
| `/skland login <電話番号>` | SMS 認証コードを送信し、60 秒以内にユーザーからのコード返信でログイン完了（タイムアウトで自動終了）。形式エラーは再送を促します |
| `/skland checkin` | キャッシュ済み `cred` で現在のユーザーの全ゲーム + フォーラム版へサインイン（先頭にログインデバイスを表示） |
| `/skland status` | 現在のユーザーの電話番号、ログインデバイス、自動サインイン/ゲーム/フォーラム設定を表示 |
| `/skland logout` | 現在のユーザーの認証情報を全て削除（電話番号の占有も解放） |

すべてのコマンドはユーザーごとで、現在のユーザーにのみ影響します。以降は毎日 `/skland checkin` を実行（または自動サインインを有効化）するだけです。`cred` の有効期限が切れると再ログインを求められます。

ログイン操作：`/skland login 18600000000` を実行すると SMS 認証コードが送信されるので、**60 秒以内に 6 桁のコードを返信**するだけでログイン完了です（`/skland` プレフィックス不要）。

## 自動サインイン設定

| 設定 | 説明 | デフォルト |
| --- | --- | --- |
| `auto_checkin_enabled` | 自動サインインのマスタースイッチ。オフの場合、時刻を設定しても実行されない | `true` |
| `auto_checkin_time` | 毎日自動サインイン時刻（`HH:MM`、24時間制）。空欄・不正なら無効 | `08:00` |
| `random_delay_seconds` | 目標時刻後に 0〜N 秒ランダム遅延して実行し、毎日固定時刻でのリスク管理を回避 | `1200` |
| `request_timeout` | HTTP リクエストのタイムアウト（秒） | `15` |
| `game_checkin_enabled` | ゲームサインインを有効にするか | `true` |
| `forum_checkin_enabled` | フォーラム版サインインを有効にするか | `true` |

自動サインインは**サイレントモード**です：結果はログのみに記録され、メッセージは配信されません。設定変更後はプラグインの再読み込みまたは AstrBot の再起動で反映されます。

## ディレクトリ構成

```
astrbot_plugin_ama-10_entertainment_skland/
├── main.py            # プラグインエントリ: /skland コマンドグループ + 自動サインイン調整
├── metadata.yaml      # プラグインメタデータ
├── _conf_schema.json  # プラグイン設定（自動サインイン/タイムアウト/スイッチ）
├── requirements.txt   # 依存関係
├── data/
│   └── devices.json   # Android 機種データベース（4 万台以上、bsthen/device-models）
└── src/               # ビジネスロジック層（AstrBot から分離）
    ├── client.py      # Skland API クライアント(署名/ログイン手順/サインイン/フィンガープリント)
    ├── device_pool.py # 機種プールの読み込みとランダム選択
    ├── storage.py     # 認証情報と電話番号占有の保存（フィンガープリント含む）
    └── service.py     # ログイン/サインインサービス + 自動サインインスケジューラ
```

（認証データは実行時に `data/plugin_data/astrbot_plugin_ama_10_entertainment_skland/` に生成されます）

## ライセンス

本プロジェクトのソースコードは [GNU AGPL-3.0](LICENSE) ライセンスで公開されています。

> **謝辞**：機種データは [bsthen/device-models](https://github.com/bsthen/device-models)（Apache-2.0、Google Play の対応デバイス一覧から毎日自動同期）より提供されています。