#!/bin/bash
# ========================================================
# 开发调试启动脚本
#
# 使用 App 内置的 Python 运行时与首次运行安装的依赖目录，
# 与打包后的运行环境保持一致，避免"开发能跑、打包不能跑"。
# ========================================================
set -e

cd "$(dirname "$0")"

PYROOT="$(pwd)/runtime/python"
PYTHON_BIN="$PYROOT/bin/python3.12"
PYLIBS="$HOME/.voicetype/pylibs"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "未找到内置 Python 运行时，正在准备..."
    ./fetch_runtime.sh
fi

if [ ! -d "$PYLIBS/torch" ]; then
    echo "运行依赖尚未安装，请先执行 ./build.sh 并启动一次应用完成初始化。"
    echo "或手动安装："
    echo "  $PYTHON_BIN -m pip install --target $PYLIBS \\"
    echo "      -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt"
    exit 1
fi

export PYTHONHOME="$PYROOT"
export PYTHONPATH="$PYLIBS"
export PYTHONNOUSERSITE=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export VOICETYPE_PYTHON="$PYTHON_BIN"
export VOICETYPE_APP_DIR="$(pwd)"
export HF_ENDPOINT="https://hf-mirror.com"
export MODELSCOPE_CACHE="$HOME/.cache/modelscope"

exec "$PYTHON_BIN" voice_type.py
