# -*- coding: utf-8 -*-
"""Redmi / 小米路由器（MiWiFi 固件）非官方 LuCI HTTP API 客户端。

协议参考（社区逆向整理 + python-xiaomi-miwifi 开源实现核对）：
  - GET  /cgi-bin/luci/web                    登录页，页面内包含 key 与 deviceId
  - POST /cgi-bin/luci/api/xqsystem/login     登录，返回 {"token": "..."}（即 stok）
  - GET  /cgi-bin/luci/;stok=<token>/api/...  携带 stok 的鉴权调用

新固件登录签名：password = sha1(nonce + sha1(password + key))

速度单位：接口返回的 upspeed / downspeed 为 字节/秒 (B/s)，流量单位为字节 (B)。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from typing import Any, Optional

import requests

KEY_RE = re.compile(r"key\s*[:=]\s*'([^']+)'")
DEVICEID_RE = re.compile(r"deviceId\s*[=:]\s*'([^']+)'")


class RouterError(Exception):
    """路由器 API 通用错误。"""


class RouterAuthError(RouterError):
    """登录失败（密码错误 / 登录页异常）。"""


class RouterUnreachable(RouterError):
    """路由器不可达。"""


class MiWiFiRouter:
    """连接单个小米 / 红米路由器（网关）。"""

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
        timeout: int = 10,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session = session or requests.Session()
        self._owns_session = session is None
        self.token: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 登录
    def _base(self) -> str:
        return f"http://{self.host}/cgi-bin/luci"

    def login(self) -> str:
        """登录并返回 stok token。失败时抛出 RouterAuthError / RouterUnreachable。"""
        try:
            resp = self._session.get(f"{self._base()}/web", timeout=self.timeout)
            page = resp.text
        except requests.RequestException as err:
            raise RouterUnreachable(f"无法访问路由器 {self.host}: {err}") from err

        key_match = KEY_RE.search(page)
        if not key_match:
            raise RouterAuthError(
                f"{self.host} 不是可识别的 MiWiFi 路由器（登录页缺少 key），"
                "请确认路由器管理地址正确"
            )
        key = key_match.group(1)
        dev_match = DEVICEID_RE.search(page)
        device_id = dev_match.group(1) if dev_match else "0"

        nonce = f"0_{device_id}_{int(time.time())}_{random.randint(0, 9999999)}"
        inner = hashlib.sha1((self.password + key).encode("utf-8")).hexdigest()
        pwd_hash = hashlib.sha1((nonce + inner).encode("utf-8")).hexdigest()

        form = {
            "username": self.username,
            "logtype": "2",
            "password": pwd_hash,
            "nonce": nonce,
        }
        try:
            resp = self._session.post(
                f"{self._base()}/api/xqsystem/login", data=form, timeout=self.timeout
            )
            payload = resp.json()
        except requests.RequestException as err:
            raise RouterUnreachable(f"登录请求失败: {err}") from err
        except ValueError as err:
            raise RouterAuthError(f"登录响应不是有效 JSON: {err}") from err

        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            msg = payload.get("msg", "") if isinstance(payload, dict) else ""
            hint = "密码错误" if payload.get("code") in (401, 403) else (msg or "未知原因")
            raise RouterAuthError(f"登录失败（{hint}），请检查路由器管理密码")
        with self._lock:
            self.token = token
        return token

    # ------------------------------------------------------------ 鉴权请求
    def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        retry: bool = True,
    ) -> dict:
        with self._lock:
            if self.token is None:
                self.login()
            token = self.token
        url = f"{self._base()}/;stok={token}/{path}"
        try:
            resp = self._session.request(
                method, url, data=data, timeout=self.timeout
            )
            payload = resp.json()
        except requests.RequestException as err:
            raise RouterUnreachable(f"请求 {path} 失败: {err}") from err
        except ValueError as err:
            raise RouterError(f"请求 {path} 返回非 JSON: {err}") from err

        if isinstance(payload, dict) and payload.get("code") == 401 and retry:
            with self._lock:
                self.token = None
            self.login()
            return self._request(method, path, data=data, retry=False)
        return payload

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, data=data)

    @staticmethod
    def _ok(payload: dict) -> bool:
        return isinstance(payload, dict) and payload.get("code") == 0

    # ------------------------------------------------------------ 读接口
    def get_init_info(self) -> dict:
        """路由器基本信息：model / isRedmi / romversion / routername 等。"""
        return self._get("api/xqsystem/init_info")

    def get_status(self) -> dict:
        """整体状态：wan 速度、dev 设备速度、count 设备数、cpu/mem、upTime。"""
        return self._get("api/misystem/status")

    def get_devices(self) -> list[dict]:
        """当前连接设备列表（含每设备速度与流量统计）。"""
        payload = self._get("api/misystem/devicelist")
        entries = payload.get("list", []) if isinstance(payload, dict) else []
        return [e for e in entries if isinstance(e, dict)]

    def get_wifi_connect_devices(self) -> list[dict]:
        """无线连接设备（含信号强度与频段）。失败时返回空列表。"""
        try:
            payload = self._get("api/xqnetwork/wifi_connect_devices")
            return payload.get("list", []) if isinstance(payload, dict) else []
        except (RouterError, RouterUnreachable):
            return []

    def get_blocked_devices(self) -> list[dict]:
        """已拉黑（禁止上网）设备列表。接口不支持时返回空列表。"""
        try:
            payload = self._get("api/xqnetwork/wifi_macfilter_info")
        except (RouterError, RouterUnreachable):
            return []
        if not isinstance(payload, dict):
            return []
        flist = payload.get("flist")
        if not isinstance(flist, list):
            lst = payload.get("list")
            if isinstance(lst, list):
                flist = [f for f in lst if isinstance(f, dict) and f.get("mac")]
        entries = [f for f in flist if isinstance(f, dict) and f.get("mac")]

        def _blocked(f: dict) -> bool:
            """拉黑判定：部分固件在顶层 wan/status，部分固件在嵌套 authority.wan。"""
            auth = f.get("authority") if isinstance(f.get("authority"), dict) else {}
            return (
                f.get("wan") in (0, "0")
                or auth.get("wan") in (0, "0")
                or f.get("status") in (0, "0")
                or f.get("isBlack") in (1, "1", True)
            )

        # 真实固件 flist 是「全部已知设备 + 上网权限」视图（authority 字段标记），
        # 需要过滤出被拉黑的；无任何状态字段时才认为 flist 本身就是拉黑列表。
        has_flag = any(
            isinstance(f.get("authority"), dict)
            or f.get("wan") is not None or f.get("status") is not None
            or f.get("isBlack") is not None for f in entries
        )
        if not has_flag:
            return entries
        return [f for f in entries if _blocked(f)]

    # ------------------------------------------------------------ 写操作
    def block_device(self, mac: str) -> bool:
        """拉黑设备（禁止其上网）。"""
        return self._ok(self._post("api/xqnetwork/set_mac_filter", {"mac": mac, "wan": "0"}))

    def unblock_device(self, mac: str) -> bool:
        """解除拉黑（恢复上网）。"""
        return self._ok(self._post("api/xqnetwork/set_mac_filter", {"mac": mac, "wan": "1"}))

    def rename_device(self, mac: str, name: str) -> bool:
        """修改设备备注名。"""
        return self._ok(
            self._post("api/xqsystem/set_device_nickname", {"mac": mac, "name": name})
        )

    def reboot(self) -> bool:
        """重启路由器（谨慎操作）。"""
        return self._ok(self._get("api/xqsystem/reboot"))

    # ------------------------------------------------------------ 工具
    def close(self) -> None:
        if self._owns_session:
            try:
                self._session.close()
            except Exception:
                pass

    @staticmethod
    def normalize_mac(mac: str) -> str:
        """统一 MAC 格式为小写、冒号分隔。"""
        mac = (mac or "").strip().lower()
        parts = re.findall(r"[0-9a-f]{2}", mac)
        return ":".join(parts) if len(parts) == 6 else mac

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        """把固件返回的任意数值形式（int/float/数字字符串）安全转为 int。

        真实固件部分字段返回浮点字符串（如在线时长 "14285.11"），
        直接 int() 会抛 ValueError，这里统一做浮点转换后取整。
        """
        if value is None or value == "":
            return default
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def merge_devices(
        device_entries: list[dict],
        status_devs: list[dict],
        wifi_devices: list[dict],
    ) -> list[dict]:
        """把 status 的速度/流量、wifi 的信号/频段合并进设备列表（按 MAC）。"""
        by_status = {
            MiWiFiRouter.normalize_mac(d.get("mac", "")): d for d in status_devs if d.get("mac")
        }
        by_wifi = {
            MiWiFiRouter.normalize_mac(d.get("mac", "")): d for d in wifi_devices if d.get("mac")
        }
        band_names = {1: "2.4G", 2: "5G", 3: "5G 游戏"}
        out = []
        for e in device_entries:
            mac = MiWiFiRouter.normalize_mac(e.get("mac", ""))
            st = by_status.get(mac, {})
            wf = by_wifi.get(mac, {})
            ip_list = e.get("ip") or []
            ip = ip_list[0].get("ip", "") if ip_list and isinstance(ip_list[0], dict) else ""
            stats = e.get("statistics") or {}
            out.append(
                {
                    "mac": mac,
                    "name": e.get("name") or e.get("oname") or mac,
                    "origin_name": e.get("oname", ""),
                    "ip": ip,
                    "online": bool(MiWiFiRouter._to_int(e.get("online", 0))),
                    "type": "有线" if e.get("type") == "line" else "无线",
                    "band": band_names.get(MiWiFiRouter._to_int(wf.get("wifiIndex", 0)), ""),
                    "signal": MiWiFiRouter._to_int(wf.get("signal", 0)),
                    "downspeed": MiWiFiRouter._to_int(st.get("downspeed") or stats.get("downspeed") or 0),
                    "upspeed": MiWiFiRouter._to_int(st.get("upspeed") or stats.get("upspeed") or 0),
                    "download": MiWiFiRouter._to_int(st.get("download") or stats.get("download") or 0),
                    "upload": MiWiFiRouter._to_int(st.get("upload") or stats.get("upload") or 0),
                    "online_secs": MiWiFiRouter._to_int(stats.get("online") or 0),
                }
            )
        return out
