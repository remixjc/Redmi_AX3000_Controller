/* Redmi X3000 路由器控制台 —— 前端逻辑 */
"use strict";

let api = null;
let browserMode = true;   // 检测到桌面桥接后切为 false
const $ = (id) => document.getElementById(id);

/* pywebview 的 js_api 在页面加载完成后注入：先定义 window.pywebview.api 空对象，
   再由 finish.js 执行 _createApi 填充方法并派发 pywebviewready 事件。
   不能同步判定，需等待事件或轮询（且 api 必须已填充方法），超时则视为浏览器预览模式。 */
function detectBridge(timeoutMs) {
  return new Promise((resolve) => {
    const started = Date.now();
    const ready = () =>
      window.pywebview && window.pywebview.api &&
      Object.keys(window.pywebview.api).length > 0;
    const check = () => {
      if (ready()) {
        api = window.pywebview.api;
        resolve(true);
        return;
      }
      if (Date.now() - started >= timeoutMs) {
        resolve(false);
        return;
      }
      setTimeout(check, 80);
    };
    window.addEventListener("pywebviewready", check);
    check();
  });
}

let chart = null;
let snapshot = { devices: [], history: [], connected: false };
let pollTimer = null;
let renameTarget = null;

/* ---------------- 工具函数 ---------------- */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return Math.round(n) + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}
function fmtSpeed(n) { return fmtBytes(n) + "/s"; }
function fmtDuration(secs) {
  secs = Number(secs) || 0;
  if (secs <= 0) return "-";
  const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600),
        m = Math.floor((secs % 3600) / 60);
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  return `${m}分钟`;
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let toastTimer = null;
function toast(msg, kind) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

/* ---------------- 状态栏 ---------------- */
function setStatus(text, cls) {
  const pill = $("status-pill");
  pill.className = "status-pill " + (cls || "");
  $("status-text").textContent = text;
}

/* ---------------- ECharts 网速曲线 ---------------- */
function initChart() {
  chart = echarts.init($("speed-chart"));
  chart.setOption({
    animation: false,
    grid: { left: 64, right: 20, top: 26, bottom: 28 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#232837",
      borderColor: "#343b4e",
      textStyle: { color: "#e8eaf0", fontSize: 12 },
      formatter: (ps) => {
        const t = ps[0] ? ps[0].axisValue : "";
        let html = `<div style="font-weight:600;margin-bottom:4px">${t}</div>`;
        ps.forEach((p) => {
          html += `<div style="display:flex;align-items:center;gap:6px">
            <span style="width:8px;height:8px;border-radius:50%;background:${p.color}"></span>
            ${p.seriesName}：${fmtSpeed(p.value)}</div>`;
        });
        return html;
      },
    },
    legend: {
      data: ["下载", "上传"],
      textStyle: { color: "#8b93a7" },
      top: 0, right: 10,
    },
    xAxis: {
      type: "category",
      data: [],
      boundaryGap: false,
      axisLine: { lineStyle: { color: "#262b3a" } },
      axisLabel: { color: "#8b93a7", fontSize: 11 },
    },
    yAxis: {
      type: "value",
      axisLine: { show: false },
      axisLabel: {
        color: "#8b93a7", fontSize: 11,
        formatter: (v) => (v >= 1048576 ? (v / 1048576).toFixed(1) + " MB"
                           : v >= 1024 ? Math.round(v / 1024) + " KB" : v + " B"),
      },
      splitLine: { lineStyle: { color: "rgba(38,43,58,.6)" } },
    },
    series: [
      {
        name: "下载", type: "line", smooth: true, showSymbol: false,
        data: [], lineStyle: { width: 2, color: "#3b82f6" },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: "rgba(59,130,246,.30)" },
                        { offset: 1, color: "rgba(59,130,246,0)" }] } },
      },
      {
        name: "上传", type: "line", smooth: true, showSymbol: false,
        data: [], lineStyle: { width: 2, color: "#22c55e" },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: "rgba(34,197,94,.25)" },
                        { offset: 1, color: "rgba(34,197,94,0)" }] } },
      },
    ],
  });
}
function updateChart(history) {
  if (!chart) return;
  const times = history.map((h) => {
    const d = new Date(h[0] * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0")).join(":");
  });
  chart.setOption({
    xAxis: { data: times },
    series: [
      { data: history.map((h) => h[1]) },
      { data: history.map((h) => h[2]) },
    ],
  });
  const last = history[history.length - 1];
  $("chart-note").textContent = last
    ? `最近更新 ${last && times[times.length - 1] || ""} · 共 ${history.length} 个采样点`
    : "等待数据…";
}

/* ---------------- 设备表格 ---------------- */
function renderDevices() {
  const body = $("device-body");
  const devs = snapshot.devices || [];
  $("device-count").textContent = devs.filter((d) => d.online).length + " 在线";

  if (!snapshot.connected) {
    body.innerHTML = `<tr><td colspan="10" class="empty">未连接路由器，请先完成设置</td></tr>`;
    return;
  }
  if (!devs.length) {
    body.innerHTML = `<tr><td colspan="10" class="empty">暂无设备数据</td></tr>`;
    return;
  }
  const blocked = new Set(snapshot.blocked || []);
  const sorted = devs.slice().sort((a, b) => {
    if (a.online !== b.online) return a.online ? -1 : 1;
    return Math.max(b.downspeed, b.upspeed) - Math.max(a.downspeed, a.upspeed);
  });

  body.innerHTML = sorted.map((d) => {
    const isBlocked = blocked.has(d.mac);
    const online = d.online;
    const connectCls = d.type === "有线" ? "line" : "wifi";
    const connectLabel = (d.type === "有线" ? "有线" : "无线") + (d.band ? " " + d.band : "");
    const stateBadge = isBlocked ? `<span class="badge blocked">已拉黑</span>`
      : online ? `<span class="badge online">在线</span>` : `<span class="badge offline">离线</span>`;
    const actions = browserMode
      ? `<span style="font-size:11px;color:var(--text-dim)">预览只读</span>`
      : `
      <button class="btn mini ${isBlocked ? "primary" : "danger"}" data-act="toggle-block" data-mac="${esc(d.mac)}">
        ${isBlocked ? "解黑" : "拉黑"}</button>
      <button class="btn mini ghost" data-act="rename" data-mac="${esc(d.mac)}" data-name="${esc(d.name)}">改名</button>`;
    return `
      <tr class="${online ? "" : "offline"} ${isBlocked ? "blocked" : ""}">
        <td><div class="dev-name">${esc(d.name)}<span class="sub">${esc(d.origin_name) || "&nbsp;"}</span></div></td>
        <td><span class="badge ${connectCls}">${esc(connectLabel)}</span></td>
        <td class="mono">${esc(d.ip) || "-"}</td>
        <td class="mono">${esc(d.mac)}</td>
        <td class="speed down">${d.downspeed > 0 ? fmtSpeed(d.downspeed) : "-"}</td>
        <td class="speed up">${d.upspeed > 0 ? fmtSpeed(d.upspeed) : "-"}</td>
        <td class="mono">${fmtBytes(d.download)} / ${fmtBytes(d.upload)}</td>
        <td>${fmtDuration(d.online_secs)}</td>
        <td>${stateBadge}</td>
        <td><div class="row-actions">${actions}</div></td>
      </tr>`;
  }).join("");
}

/* ---------------- 快照渲染 ---------------- */
function renderSnapshot() {
  const s = snapshot;
  setStatus(s.connected ? "已连接" : "未连接", s.connected ? "ok" : "err");
  $("btn-reconnect").style.display = s.connected ? "none" : "inline-block";

  if (s.error) {
    const banner = $("error-banner");
    banner.textContent = "⚠ " + s.error;
    banner.style.display = "block";
  } else if (!browserMode) {
    $("error-banner").style.display = "none";
  }

  const r = s.router || {};
  $("router-info").textContent = s.connected
    ? `${r.name || "路由器"} · ${r.model || ""}${r.rom ? " · 固件 " + r.rom : ""}`
    : "未连接";

  $("stat-down").textContent = s.connected && s.wan ? fmtSpeed(s.wan.down) : "--";
  $("stat-up").textContent = s.connected && s.wan ? fmtSpeed(s.wan.up) : "--";
  $("stat-down-total").textContent = s.connected && s.wan ? fmtBytes(s.wan.downTotal) : "--";
  $("stat-up-total").textContent = s.connected && s.wan ? fmtBytes(s.wan.upTotal) : "--";
  $("stat-online").textContent = s.connected ? (s.count ? s.count.online : 0) : "--";
  $("stat-all").textContent = s.connected ? (s.count ? s.count.all : 0) : "--";
  $("stat-blocked").textContent = s.connected ? (s.blocked ? s.blocked.length : 0) : "--";
  $("stat-uptime").textContent = s.connected ? fmtDuration(s.uptime) : "--";
  $("stat-cpu").textContent = s.connected ? Math.round((s.cpu || 0) * 100) + "%" : "--";
  $("stat-mem").textContent = s.connected ? Math.round((s.mem || 0) * 100) + "%" : "--";

  if (s.connected) updateChart(s.history || []);
  renderDevices();
}

/* ---------------- 轮询 ---------------- */
async function poll() {
  try {
    if (browserMode) {
      const resp = await fetch("/api/snapshot", { cache: "no-store" });
      snapshot = await resp.json();
    } else {
      snapshot = await api.get_snapshot();
    }
    renderSnapshot();
  } catch (err) {
    setStatus("通信异常", "err");
  }
}
function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1000);
}

/* ---------------- 设置弹窗 ---------------- */
function openSettings() {
  $("settings-modal").style.display = "flex";
  $("test-result").textContent = "";
  $("test-result").className = "test-result";
  if (api) {
    api.get_config().then((cfg) => {
      $("set-host").value = cfg.host || "192.168.31.1";
      $("set-username").value = cfg.username || "admin";
      $("set-interval").value = cfg.poll_interval || 2;
      $("set-password").value = "";
      $("set-password").placeholder = cfg.has_password ? "已保存（留空则沿用）" : "路由器后台密码";
    });
  }
}
function closeSettings() { $("settings-modal").style.display = "none"; }

async function onTest() {
  const el = $("test-result");
  el.className = "test-result";
  el.textContent = "正在测试连接…";
  const r = await api.test_connection(
    $("set-host").value, $("set-username").value, $("set-password").value);
  el.textContent = r.msg;
  el.className = "test-result " + (r.ok ? "ok" : "err");
}

async function onSave() {
  const btn = $("btn-save");
  btn.disabled = true;
  btn.textContent = "连接中…";
  const r = await api.save_settings(
    $("set-host").value, $("set-username").value, $("set-password").value,
    $("set-interval").value);
  btn.disabled = false;
  btn.textContent = "保存并连接";
  if (r.ok) {
    closeSettings();
    toast(r.msg || "已连接", "ok");
    await poll();
  } else {
    toast(r.msg || "连接失败", "err");
    $("test-result").textContent = r.msg;
    $("test-result").className = "test-result err";
  }
}

/* ---------------- 设备操作 ---------------- */
async function onToggleBlock(mac) {
  const isBlocked = (snapshot.blocked || []).includes(mac);
  const r = await api[isBlocked ? "unblock_device" : "block_device"](mac);
  toast(r.msg, r.ok ? "ok" : "err");
  await poll();
}

function openRename(mac, name) {
  renameTarget = { mac, name };
  $("rename-mac").textContent = mac;
  $("rename-name").value = name === mac ? "" : name;
  $("rename-name").placeholder = name === mac ? "输入新名称" : name;
  $("rename-modal").style.display = "flex";
  $("rename-name").focus();
}
function closeRename() { $("rename-modal").style.display = "none"; }

async function confirmRename() {
  if (!renameTarget) return;
  const r = await api.rename_device(renameTarget.mac, $("rename-name").value);
  toast(r.msg, r.ok ? "ok" : "err");
  closeRename();
  await poll();
}

/* ---------------- 重启 ---------------- */
function openReboot() { $("reboot-modal").style.display = "flex"; }
function closeReboot() { $("reboot-modal").style.display = "none"; }
async function confirmReboot() {
  closeReboot();
  const r = await api.reboot_router();
  toast(r.msg, r.ok ? "ok" : "err");
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $("btn-settings").addEventListener("click", openSettings);
  $("btn-close-settings").addEventListener("click", closeSettings);
  $("btn-test").addEventListener("click", onTest);
  $("btn-save").addEventListener("click", onSave);
  $("btn-close-rename").addEventListener("click", closeRename);
  $("btn-cancel-rename").addEventListener("click", closeRename);
  $("btn-confirm-rename").addEventListener("click", confirmRename);
  $("btn-cancel-reboot").addEventListener("click", closeReboot);
  $("btn-confirm-reboot").addEventListener("click", confirmReboot);
  $("btn-refresh").addEventListener("click", poll);

  if (browserMode) return; // 预览模式不绑定需要桥接的控制事件

  $("btn-reconnect").addEventListener("click", async () => {
    const r = await api.reconnect();
    toast(r.msg, r.ok ? "ok" : "err");
  });
  $("btn-open-web").addEventListener("click", () => api.open_router_web());

  $("interval-select").addEventListener("change", async (e) => {
    const r = await api.set_poll_interval(e.target.value);
    if (!r.ok) toast(r.msg, "err");
  });

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const { act, mac } = btn.dataset;
    if (act === "toggle-block") onToggleBlock(mac);
    if (act === "rename") openRename(mac, btn.dataset.name);
  });

  document.querySelector(".app").addEventListener("click", (e) => {
    if (e.target.id === "btn-reboot-router") openReboot();
  });

  // 头部增加重启入口（放在打开后台按钮旁；仅桌面模式）
  if (!browserMode) {
    const wrap = document.querySelector(".device-actions");
    const rebootBtn = document.createElement("button");
    rebootBtn.className = "btn ghost small danger-outline";
    rebootBtn.id = "btn-reboot-router";
    rebootBtn.textContent = "重启路由器";
    rebootBtn.style.color = "#ef4444";
    rebootBtn.style.borderColor = "rgba(239,68,68,.4)";
    wrap.appendChild(rebootBtn);
  }
}

/* ---------------- 启动 ---------------- */
async function boot() {
  browserMode = !(await detectBridge(2500));

  initChart();
  bind();
  startPolling();
  await poll();

  if (browserMode) {
    const banner = $("error-banner");
    banner.style.display = "block";
    banner.textContent = "ℹ 浏览器预览模式（只读）：正在渲染实时数据，控制操作请在桌面应用窗口中进行。";
    $("btn-settings").disabled = true;
    $("btn-reconnect").style.display = "none";
    return;
  }

  // 首次运行（无密码配置）自动弹出设置
  const cfg = await api.get_config();
  if (!cfg.has_password) {
    openSettings();
  }
}

document.addEventListener("DOMContentLoaded", boot);
