#!/bin/bash
# ========================================================
# VoiceType 一键构建、签名与安装
#
# 使用 Apple 开发者官方 "Developer ID Application" 证书与
# Hardened Runtime 进行安全加固，彻底消除 Gatekeeper 未签名警告。
# ========================================================
set -e

cd "$(dirname "$0")"

STAGE_APP="build/VoiceType.app"
ENTITLEMENTS="entitlements.plist"

# 动态提取本机钥匙串中的 Developer ID 证书（严禁硬编码任何个人姓名与 Team ID）
SIGN_ID="${SIGN_ID:-}"
if [ -z "$SIGN_ID" ]; then
    SIGN_ID=$(security find-identity -v -p codesigning 2>/dev/null | grep "Developer ID Application" | head -n 1 | awk -F'"' '{print $2}' || true)
fi

if [ -z "$SIGN_ID" ]; then
    echo "⚠️  未检测到正式 Developer ID 证书，回退使用本地开发自签名 (-)"
    SIGN_ID="-"
fi

echo "==> 1. 准备内置 Python 运行时..."
if [ ! -x "runtime/python/bin/python3.12" ]; then
    ./fetch_runtime.sh
fi

PYROOT="$(pwd)/runtime/python"
PYTHON_INCLUDE="$PYROOT/include/python3.12"

if [ ! -d "$PYTHON_INCLUDE" ]; then
    echo "    错误：缺少 Python 头文件 $PYTHON_INCLUDE"
    exit 1
fi

echo "==> 2. 组装 Application Bundle 结构..."
rm -rf build
mkdir -p "${STAGE_APP}/Contents/MacOS"
mkdir -p "${STAGE_APP}/Contents/Resources"

cp Info.plist "${STAGE_APP}/Contents/Info.plist"
cp icon.icns "${STAGE_APP}/Contents/Resources/icon.icns"
[ -f menu_icon@2x.png ] && cp menu_icon@2x.png "${STAGE_APP}/Contents/Resources/menu_icon@2x.png"
cp voice_type.py "${STAGE_APP}/Contents/Resources/voice_type.py"
cp bootstrap.py "${STAGE_APP}/Contents/Resources/bootstrap.py"
cp requirements.txt "${STAGE_APP}/Contents/Resources/requirements.txt"

echo "==> 3. 复制 Python 运行时到 App 内部..."
cp -R "$PYROOT" "${STAGE_APP}/Contents/Resources/python"
find "${STAGE_APP}/Contents/Resources/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> 4. 编译原生启动器..."
clang -fobjc-arc \
      -I"$PYTHON_INCLUDE" \
      -L"${PYROOT}/lib" \
      -lpython3.12 \
      -Wl,-rpath,@executable_path/../Resources/python/lib \
      -framework Foundation \
      -o "${STAGE_APP}/Contents/MacOS/VoiceType" native_launcher.m

echo "==> 5. 逐层执行安全加固签名 (Hardened Runtime)..."
xattr -cr "${STAGE_APP}"

# 遍历 Resources 内所有 Mach-O 二进制（包含 python3.12 可执行程序、dylib 及所有 .so 扩展）并签名
find "${STAGE_APP}/Contents/Resources" -type f | while read -r f; do
    if file -b "$f" 2>/dev/null | grep -q "Mach-O"; then
        codesign --force --options runtime --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" --timestamp "$f"
    fi
done

# 对主可执行程序签名
codesign --force --options runtime --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" --timestamp "${STAGE_APP}/Contents/MacOS/VoiceType"

# 对整个 Application Bundle 深度签名
codesign --force --deep --options runtime \
         --entitlements "$ENTITLEMENTS" \
         --sign "$SIGN_ID" \
         --timestamp \
         "${STAGE_APP}"

echo "==> 6. 验证签名完整性..."
codesign -vvv --deep --strict "${STAGE_APP}" 2>&1 | tail -n 3

echo "==> 7. 安装到 /Applications/VoiceType.app..."
rm -rf /Applications/VoiceType.app
cp -R "${STAGE_APP}" /Applications/VoiceType.app
xattr -cr /Applications/VoiceType.app
codesign --force --deep --options runtime --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" --timestamp /Applications/VoiceType.app

echo "==> 8. 刷新 LaunchServices 注册..."
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f /Applications/VoiceType.app 2>/dev/null || true

echo ""
echo "✅ 构建完成"
echo "   签名身份   : $SIGN_ID"
echo "   应用包大小 : $(du -sh "${STAGE_APP}" | cut -f1)"
echo "   安装位置   : /Applications/VoiceType.app"
