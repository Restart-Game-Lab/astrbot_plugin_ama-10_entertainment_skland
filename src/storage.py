"""src/storage.py - 凭据与推送目标存储 (按用户隔离 + 手机号占用)

- auth.json  : 多用户多账号凭据, 按用户 -> 手机号 两级保存
              {
                "<用户key>": { "18600000000": {cred...}, ... },
                ...
              }
              用户 key = "平台id:发送者id"(如 aiocqhttp:10001),
              保证群内多人互不干扰(见 main._uid)。
- owners.json: 手机号占用索引 { "18600000000": "<用户key>" }，保证一个手机号
              同一时间只被一个用户占用；logout 后释放
- sub.json   : 用户 -> 推送目标(用户 key) 映射, 供自动签到推送

旧版兼容: 早期版本曾用 unified_msg_origin(如 aiocqhttp:GroupMessage:群号)
作为用户 key, 群维度无法区分人。__init__ 时会把「私聊」旧 key
(aiocqhttp:PrivateMessage:QQ号) 无损迁移到 aiocqhttp:QQ号;
「群」旧 key 无法还原到人, 保留原样, 由 load_auth 按提示重登。
"""

import json
import re
import time
from pathlib import Path

from astrbot.api import logger


class Storage:
    """基于 JSON 文件的纯同步存储（少量数据, 无需数据库）"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._auth_file = data_dir / "auth.json"
        self._owners_file = data_dir / "owners.json"
        self._sub_file = data_dir / "sub.json"
        self._migrate_legacy_keys()

    # ---------- 旧版 key 迁移 ----------
    def _migrate_legacy_keys(self):
        """一次性迁移旧版 unified_msg_origin 用户 key -> 平台id:发送者id。

        - 私聊 key (platform:PrivateMessage:QQ号): 无损迁移为 platform:QQ号
        - 群 key (platform:GroupMessage:群号): 多人共享无法还原到人, 保留原样
          (load_auth 时若命中会提示重新登录)
        """
        auth = self._load_json(self._auth_file, {})
        owners = self._load_json(self._owners_file, {})
        subs = self._load_json(self._sub_file, {})
        changed = False

        for src in list(auth):
            m = re.fullmatch(r"(.+):PrivateMessage:(\d+)", src)
            if not m:
                continue
            dst = f"{m.group(1)}:{m.group(2)}"
            if dst != src and dst not in auth:
                auth[dst] = auth.pop(src)
                # 同步 owners: phone -> src 改成 dst
                for phone, owner in list(owners.items()):
                    if owner == src:
                        owners[phone] = dst
                # 同步 sub: src -> dst
                if src in subs:
                    subs[dst] = subs.pop(src)
                changed = True
                logger.info(f"AMA-10 Skland: 凭据 key {src} -> {dst} 已迁移")

        # 群共享旧 key 无法还原到人: 释放其手机号占用(避免新用户被死占用挡住)
        for src in list(auth):
            if ":GroupMessage:" in src:
                for phone, owner in list(owners.items()):
                    if owner == src:
                        owners.pop(phone, None)
                        changed = True
                        logger.info(
                            f"AMA-10 Skland: 释放旧群共享凭据 {src} 的手机号 {phone} 占用"
                        )

        if changed:
            self._save_json(self._auth_file, auth)
            self._save_json(self._owners_file, owners)
            self._save_json(self._sub_file, subs)

    # ---------- 文件读写 ----------
    def _load_json(self, path: Path, default):
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"AMA-10 Skland: 读取 {path.name} 失败: {e}")
            return default

    def _save_json(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 凭据 (按用户隔离, 一人一手机号) ----------
    def load_auth(self, uid: str | None = None) -> dict:
        """读取凭据。uid 为空返回全部(多用户), 否则返回该用户手机号->凭据映射。

        一人一手机号: 每个 uid 下最多一个手机号。
        """
        all_auth = self._load_json(self._auth_file, {})
        if uid is None:
            return all_auth
        return all_auth.get(uid, {})

    def get_user_phone(self, uid: str) -> str | None:
        """返回该用户已绑定的手机号(一人一手机号), 未登录返回 None"""
        user_auth = self.load_auth(uid)
        return next(iter(user_auth), None)

    def get_auth(self, uid: str, phone: str) -> dict | None:
        return self.load_auth(uid).get(phone)

    def set_auth(self, uid: str, phone: str, cred: str, token: str,
                 login_token: str = "", device_token: str = "",
                 hg_id: str = "", fingerprint: dict | None = None):
        """保存指定用户的一个账号凭据, 同时登记手机号占用。

        ⚠️ 一人一手机号: 若该用户已有其他手机号, 先释放旧手机号占用再存新的。
        fingerprint: 登录时使用的完整设备指纹(持久化复用, 防风控)。
        """
        all_auth = self.load_auth()
        user_auth = all_auth.setdefault(uid, {})
        old_phones = [p for p in user_auth if p != phone]
        # 释放旧手机号占用
        owners = self._load_json(self._owners_file, {})
        for old in old_phones:
            owners.pop(old, None)
        user_auth.clear()
        user_auth[phone] = {
            "cred": cred,
            "token": token,
            "login_token": login_token,
            "device_token": device_token,
            "hgId": hg_id,
            "fingerprint": fingerprint or {},
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_json(self._auth_file, all_auth)
        # 登记新占用
        owners[phone] = uid
        self._save_json(self._owners_file, owners)

    def clear_user(self, uid: str) -> int:
        """清除指定用户全部凭据, 并释放其手机号占用, 返回删除数量"""
        all_auth = self.load_auth()
        user_auth = all_auth.get(uid, {})
        n = len(user_auth)
        if n:
            # 释放该用户所有手机号占用
            owners = self._load_json(self._owners_file, {})
            for phone in user_auth:
                owners.pop(phone, None)
            self._save_json(self._owners_file, owners)

            del all_auth[uid]
            self._save_json(self._auth_file, all_auth)
        return n

    def del_user_auth(self, uid: str, phone: str) -> bool:
        """删除指定用户指定手机号凭据, 并释放该手机号占用"""
        all_auth = self.load_auth()
        user_auth = all_auth.get(uid, {})
        if phone in user_auth:
            del user_auth[phone]
            if not user_auth:
                del all_auth[uid]
            self._save_json(self._auth_file, all_auth)

            owners = self._load_json(self._owners_file, {})
            owners.pop(phone, None)
            self._save_json(self._owners_file, owners)
            return True
        return False

    # ---------- 手机号占用 ----------
    def get_owner(self, phone: str) -> str | None:
        """返回占用该手机号的 uid, 未占用返回 None"""
        owners = self._load_json(self._owners_file, {})
        return owners.get(phone)

    def is_phone_occupied(self, phone: str, by_uid: str | None = None) -> bool:
        """该手机号是否被占用。

        by_uid 指定时, 若占用者正是自己则视为未占用(自己的可复用)。
        """
        owner = self.get_owner(phone)
        if owner is None:
            return False
        if by_uid is not None and owner == by_uid:
            return False
        return True

    # ---------- 推送目标 (按用户隔离) ----------
    def load_sub(self, uid: str) -> str:
        subs = self._load_json(self._sub_file, {})
        return subs.get(uid, "")

    def save_sub(self, uid: str, umo: str):
        subs = self._load_json(self._sub_file, {})
        subs[uid] = umo
        self._save_json(self._sub_file, subs)