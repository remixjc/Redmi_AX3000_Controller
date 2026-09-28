# -*- coding: utf-8 -*-
"""App 后端无头端到端测试：连接 -> 轮询 -> 快照数据 -> 控制操作 -> 错误场景。

运行：cd 项目目录 && .venv/bin/python tests/test_app_backend.py
不依赖 GUI；不打钥匙串（monkeypatch 掉 save_config）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_mod                       # noqa: E402
from tests.test_mock_router import (        # noqa: E402
    MockRouter, PASSWORD, TOKEN, start_mock,
)


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def main() -> int:
    app_mod.save_config = lambda *a, **k: None  # 测试不打钥匙串、不写配置
    server = start_mock()
    host = f"127.0.0.1:{server.server_address[1]}"
    print(f"Mock 路由器: http://{host}")

    # ---- 正常连接与轮询 ----
    inst = app_mod.App()
    r = inst.connect(host, "admin", PASSWORD)
    expect(r["ok"], "connect() 连接 Mock 成功")

    time.sleep(3.2)  # 等待至少一轮轮询
    snap = inst.get_snapshot()
    expect(snap["connected"], "快照 connected=True")
    expect(snap["router"]["model"] == "Redmi Router X3000", "快照识别机型 Redmi Router X3000")
    expect(snap["count"]["online"] == 2 and snap["count"]["all"] == 30, "在线/累计设备数正确")
    expect(len(snap["devices"]) == 3, "设备列表 3 台")
    expect(snap["devices"][0]["name"] == "我的iPhone", "设备名正确")
    expect(snap["devices"][0]["downspeed"] > 0, "设备下行速度 > 0")
    expect(snap["wan"]["down"] > 0 and snap["wan"]["up"] > 0, "WAN 上下行速度 > 0")
    expect(len(snap["history"]) >= 1, "历史曲线已开始累积")
    expect("aa:bb:cc:dd:ee:03" in snap["blocked"], "拉黑列表包含已拉黑设备（MAC 小写归一化）")
    expect(bool(snap["last_update"]), "快照带更新时间")

    time.sleep(2.2)
    snap2 = inst.get_snapshot()
    expect(len(snap2["history"]) >= len(snap["history"]), "轮询持续累积历史点")

    # ---- 控制操作 ----
    r = inst.block_device("AA:BB:CC:DD:EE:02")
    expect(r["ok"] and r["msg"] == "已拉黑该设备", "block_device 成功")
    r = inst.unblock_device("AA:BB:CC:DD:EE:02")
    expect(r["ok"] and r["msg"] == "已解除拉黑", "unblock_device 成功")
    r = inst.rename_device("AA:BB:CC:DD:EE:01", "客厅电视")
    expect(r["ok"] and r["msg"] == "设备名已更新", "rename_device 成功")
    r = inst.rename_device("AA:BB:CC:DD:EE:01", "   ")
    expect(not r["ok"], "空名称被拒绝")
    inst.quit()

    # ---- 错误密码 ----
    MockRouter.login_ok = False
    try:
        inst2 = app_mod.App()
        r = inst2.connect(host, "admin", "wrongpass")
        expect(not r["ok"], "错误密码 connect 返回失败")
        snap = inst2.get_snapshot()
        expect(not snap["connected"] and bool(snap["error"]), "错误密码后快照未连接并带错误信息")
        inst2.quit()
    finally:
        MockRouter.login_ok = True

    # ---- 不可达地址 ----
    inst3 = app_mod.App()
    r = inst3.connect("127.0.0.1:1", "admin", PASSWORD)
    expect(not r["ok"] and "无法访问" in r["msg"], "不可达地址返回明确错误")
    inst3.quit()

    server.shutdown()
    print("\nApp 后端端到端测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
