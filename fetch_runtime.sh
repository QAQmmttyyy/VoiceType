#!/bin/bash
# ========================================================
# 下载并准备 App 内置 Python 运行时
#
# 产物：runtime/python/  —— 精简版 CPython 3.12 + 预装 PyObjC
# 说明：运行时完全独立，不依赖 Homebrew 或系统 Python，
#       以便终端用户机器上无需任何前置环境即可运行。
# ========================================================
set -e

cd "$(dirname "$0")"

PBS_TAG="20260901"
PBS_FILE="cpython-3.12.14+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-3.12.14%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"

PYPI_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"

if [ -x "runtime/python/bin/python3.12" ]; then
    echo "==> runtime/ 已存在，跳过下载。如需重建请先删除 runtime/ 目录。"
    exit 0
fi

echo "==> 1. 下载 Python ${PBS_TAG} 运行时..."
rm -rf runtime
mkdir -p runtime
curl -L --fail --max-time 900 -o /tmp/voicetype_python.tar.gz "$PBS_URL"

echo "==> 2. 解压运行时..."
tar -xzf /tmp/voicetype_python.tar.gz -C runtime
rm -f /tmp/voicetype_python.tar.gz

echo "==> 3. 裁剪无关组件 (Tk/Tcl/IDLE/测试)..."
cd runtime/python
rm -rf lib/python3.12/idlelib lib/python3.12/tkinter lib/python3.12/turtledemo \
       lib/tcl9 lib/tcl9.0 lib/tk9.0 lib/itcl4.3.8 lib/thread3.0.6 \
       lib/libtcl9.0.dylib lib/libtcl9tk9.0.dylib \
       bin/2to3 bin/2to3-3.12 bin/idle3 bin/idle3.12 bin/pydoc3 bin/pydoc3.12 \
       bin/python3.12-config bin/python3-config share
rm -f lib/python3.12/lib-dynload/_tkinter*.so
cd ../..

echo "==> 4. 预装 PyObjC (供引导界面与主程序直接使用)..."
runtime/python/bin/python3.12 -m pip install -q \
    -i "$PYPI_INDEX" \
    --target runtime/python/lib/python3.12/site-packages \
    pyobjc-core pyobjc-framework-Cocoa

echo "==> 5. 清理测试与缓存文件..."
rm -rf runtime/python/lib/python3.12/site-packages/PyObjCTest
find runtime -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> 6. 自检..."
env -u ALL_PROXY -u all_proxy runtime/python/bin/python3.12 -c "
import AppKit, venv, ensurepip
print('    运行时版本 :', __import__('sys').version.split()[0])
print('    AppKit     : OK')
print('    venv       : OK')
"

echo "✅ 内置运行时准备完成，占用 $(du -sh runtime | cut -f1)"
