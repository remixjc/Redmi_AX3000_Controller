#!/bin/bash
# Redmi X3000 路由器控制台 —— 启动器
# 双击本文件即可启动；首次运行会自动创建虚拟环境并安装依赖。
cd "$(dirname "$0")" || exit 1

if [ ! -d .venv ]; then
  echo "首次运行：正在创建虚拟环境并安装依赖（约 1 分钟）…"
  python3 -m venv .venv || { echo "创建虚拟环境失败，请确认已安装 Python 3.11+"; exit 1; }
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet pywebview requests || { echo "依赖安装失败，请检查网络后重试"; exit 1; }
fi

exec .venv/bin/python app.py
