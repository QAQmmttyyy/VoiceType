#!/bin/bash
# ========================================================
# VoiceType 商业级 DMG 镜像制作与 Apple 官方公证脚本
#
# 流程：
#   1. 组装 DMG 镜像并排版经典的拖拽安装视图；
#   2. 使用 Developer ID 证书签署 DMG；
#   3. 向 Apple 官方公证服务器提交并等待审核（自动临时避开代理防止挂起）；
#   4. 装订（Staple）公证票据，实现全球 Mac 用户无网、离线零警告双击安装。
# ========================================================
set -euo pipefail

cd "$(dirname "$0")"

VOL_NAME="VoiceType 安装与引导"
DMG_NAME="VoiceType-Installer.dmg"
STAGE_DIR="/tmp/voicetype_dmg_stage"
TEMP_DMG="/tmp/voicetype_temp.dmg"
PROFILE="${NOTARY_PROFILE:-voicetype-profile}"
SERVICE="${RELEASE_NETWORK_SERVICE:-Wi-Fi}"

# 动态提取本机钥匙串中的 Developer ID 证书（严禁硬编码任何个人姓名与 Team ID）
SIGN_ID="${SIGN_ID:-}"
if [ -z "$SIGN_ID" ]; then
    SIGN_ID=$(security find-identity -v -p codesigning 2>/dev/null | grep "Developer ID Application" | head -n 1 | awk -F'"' '{print $2}' || true)
fi

if [ -z "$SIGN_ID" ]; then
    echo "❌ 制作正式发布 DMG 必须拥有 Developer ID 证书！"
    exit 1
fi

echo "==> 1. 准备 DMG 载荷..."
rm -rf "$STAGE_DIR" "$TEMP_DMG" "$DMG_NAME"
mkdir -p "$STAGE_DIR"

if [ ! -d "/Applications/VoiceType.app" ]; then
    ./build.sh
fi

# 防呆：若源码比已构建的二进制新，说明忘记重新构建，直接中止，
# 避免把缺少最新修复的旧包公证并发布出去。
for src in voice_type.py bootstrap.py native_launcher.m requirements.txt; do
    if [ "$src" -nt "/Applications/VoiceType.app/Contents/MacOS/VoiceType" ]; then
        echo "❌ $src 比已构建的应用新，请先执行 ./build.sh 再打包。"
        exit 1
    fi
done

cp -R /Applications/VoiceType.app "$STAGE_DIR/VoiceType.app"
ln -s /Applications "$STAGE_DIR/Applications"

cat << 'EOF' > "$STAGE_DIR/安装指引.txt"
【VoiceType 安装与首次使用】

一、安装
  1. 将左侧的 VoiceType 拖拽到右侧的 Applications 文件夹；
  2. 打开「访达 -> 应用程序」，双击启动 VoiceType。

二、首次启动（仅需一次）
  应用会自动弹出配置窗口，下载本地 AI 引擎（约 500MB）与语音模型（约 1.9GB）。
  请保持网络连接并耐心等待，完成后自动进入主界面。
  此步骤只需执行一次，之后完全离线运行，不再需要联网。

三、开启系统权限
  按控制中心面板提示，依次开启以下三项权限，然后点击「重启生效」：
    - 辅助功能（模拟键入）
    - 输入监控（快捷键监听）
    - 麦克风访问（音频录音）

四、开始使用
  在任意输入框中长按键盘「右侧 Option (⌥)」说话，松开即自动打字。
  也可在控制中心切换为「单击切换」模式。

五、遇到问题
  若模型下载中断，可点击控制中心内的「重新下载 / 修复组件」重试。
EOF

echo "==> 2. 创建临时读写镜像..."
SIZE=$(du -sm "$STAGE_DIR" | awk '{print $1}')
SIZE=$((SIZE + 50))
hdiutil create -srcfolder "$STAGE_DIR" -volname "$VOL_NAME" -fs HFS+ \
        -fsargs "-c c=64,a=16,e=16" -format UDRW -size ${SIZE}m "$TEMP_DMG" >/dev/null

echo "==> 3. 挂载镜像并排版引导窗口..."
DEV_LINE=$(hdiutil attach "$TEMP_DMG" -readwrite -nobrowse | head -n 1)
DEV=$(echo "$DEV_LINE" | awk '{print $1}')
sleep 1

osascript << APPLESCRIPT || true
tell application "Finder"
    tell disk "$VOL_NAME"
        open
        delay 1
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 150, 800, 520}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 128
        set text size of viewOptions to 13
        
        try
            set position of item "VoiceType.app" of container window to {160, 160}
            set position of item "Applications" of container window to {440, 160}
            set position of item "安装指引.txt" of container window to {300, 290}
        end try
        
        update without registering applications
        delay 1
        close
    end tell
end tell
APPLESCRIPT

sync
sleep 1
hdiutil detach "$DEV" -force >/dev/null 2>&1 || true
sleep 1

echo "==> 4. 压缩并生成最终分发 DMG..."
hdiutil convert "$TEMP_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG_NAME" >/dev/null
rm -f "$TEMP_DMG"
rm -rf "$STAGE_DIR"

echo "==> 5. 使用 Developer ID 对 DMG 签名..."
codesign --sign "$SIGN_ID" --timestamp "$DMG_NAME"

echo "==> 6. 提交 Apple 官方公证 (Notarization)..."
# 记录当前系统代理状态，公证上传走 CFNetwork 时需避开本地 Clash 代理挂起
WEB_WAS_ON="$(networksetup -getwebproxy "$SERVICE" 2>/dev/null | awk '{if ($1=="Enabled:") print $2}')"
SECURE_WAS_ON="$(networksetup -getsecurewebproxy "$SERVICE" 2>/dev/null | awk '{if ($1=="Enabled:") print $2}')"

restore_system_proxy() {
    [ "$WEB_WAS_ON" = "Yes" ] && networksetup -setwebproxystate "$SERVICE" on 2>/dev/null || true
    [ "$SECURE_WAS_ON" = "Yes" ] && networksetup -setsecurewebproxystate "$SERVICE" on 2>/dev/null || true
}
trap restore_system_proxy EXIT

networksetup -setwebproxystate "$SERVICE" off 2>/dev/null || true
networksetup -setsecurewebproxystate "$SERVICE" off 2>/dev/null || true

echo "    正在上传至 Apple 公证服务器，请稍候..."
xcrun notarytool submit "$DMG_NAME" --keychain-profile "$PROFILE" --wait

restore_system_proxy

echo "==> 7. 装订 (Staple) 公证票据..."
xcrun stapler staple "$DMG_NAME"
xcrun stapler staple /Applications/VoiceType.app 2>/dev/null || true

echo "==> 8. 验证 Gatekeeper 通过状态..."
spctl -a -vv --type install "$DMG_NAME" 2>&1 | head -n 5

echo ""
echo "🎉 商业发布级公证完毕！"
echo "   产物路径 : $(pwd)/$DMG_NAME"
echo "   文件大小 : $(ls -lh "$DMG_NAME" | awk '{print $5}')"
echo "   安全状态 : 100% 苹果官方安全认证，任何 Mac 用户双击零警告直接运行"
