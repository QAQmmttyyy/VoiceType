# VoiceType

> **macOS 原生离线语音输入工具**  
> 100% 本地离线 · 阿里 SenseVoice 极速识别 · Qwen 标点保真纠偏 · 遵循 Apple HIG 黑曜石毛玻璃美学

---

## 核心特性

- **模型与依赖全部内聚到固定目录**：依赖装到 `~/.voicetype/pylibs`，模型下到 `~/.voicetype/models`；
  两者均从 ModelScope（阿里云 CDN）获取，国内实测 25MB/s 以上，不引入境外源，也无需自建 CDN。
- **100% 离线与隐私保护**：音频录制与识别全程在本地（Apple Silicon 芯片）完成，零网络请求，彻底杜绝数据外泄。
- **阿里 SenseVoice 极速引擎**：支持中英文混说与自动断句，平均识别耗时仅 **0.8 秒左右（RTF < 0.1）**，远快于传统 Whisper 模型。
- **本地 Qwen 标点保真还原**：内置本地 Qwen2.5 语言模型智能标点恢复，配备**严格字符级保真校验算法**，模型若修改原文字符立即自动回退，确保口述内容 100% 忠实还原。
- **纯正 Apple HIG 原生体验**：
  - **Dock 栏 + 菜单栏双入口**：Dock 栏常驻精致 AI 麦克风图标，彻底避免 MacBook 刘海屏挤压导致状态栏图标失联。
  - **设置与权限引导中心**：提供纯正 macOS 风格的原生控制面板，辅助功能、输入监控、麦克风三项系统权限独立陈列与实时状态检测，右下角「重启生效」主操作一键载入新权限。
  - **沉底黑曜石 HUD 胶囊**：屏幕下方居中悬浮，支持鼠标事件穿透，绝无 Emoji，全量采用苹果官方 SF Symbols 矢量图标。
- **自带运行环境，零前置依赖**：应用内置 Python 运行时，新用户无需安装 Homebrew、Python 或任何命令行工具，拖入「应用程序」即可运行。
- **轻量安装包**：DMG 仅约 24MB，本地 AI 引擎与模型在首次启动时由图形向导自动拉取，此后永久离线。
- **GPU 加速推理**：自动使用 Apple Silicon 的 Metal GPU（M 系列芯片），实测比纯 CPU 快数倍，并附带模型预热避免首次使用变慢。
- **纯内存音频链路**：采用内存级高保真数字信号重采样，彻底解耦外部 ffmpeg 子进程，启动极速且零依赖冲突。
- **全局快捷键与双交互模式**：
  - 默认绑定键盘**右侧 `Option (⌥)` 键**，不影响左侧常规修饰键习惯；
  - **长按说话模式**：按住右侧 `Option (⌥)` 自然口述，松开瞬间完成识别并即时键入；
  - **单击切换模式**：点按一下开始录音，再次点按结束录音并键入；
  - 在控制中心面板或顶部菜单栏中可即选即用、实时切换。

---

## 系统要求

- macOS 13.0 (Ventura) 及以上（已在 macOS 15 Sequoia 验证）
- Apple Silicon 芯片（M1 / M2 / M3 / M4 系列）
- 首次启动需要网络连接（下载本地 AI 引擎与模型），之后可完全离线使用

> 无需预先安装 Python、Homebrew 或 Xcode 命令行工具，运行环境随应用一起分发。

---

## 架构概览

```
[ 用户长按 Option (⌥) ]
         │
         ▼
[ Quartz CGEventTap 全局监听 ] ──► [ 原生黑曜石 HUD 胶囊弹出 (正在聆听) ]
         │
         ▼
[ SoundDevice 硬件原采样率录音 ]
         │ (用户松开 Option)
         ▼
[ SciPy 纯内存重采样至 16kHz ]  ──► [ 胶囊更新为平滑旋转指示器 (正在识别) ]
         │
         ▼
[ FunASR / SenseVoice-Small 本地推理 ]
         │
         ▼
[ Qwen2.5-0.5B 标点还原 + 严格字符保真验证 ]
         │
         ▼
[ NSPasteboard 同步写入 + 虚拟按键脉冲自动打字 ]
         │
         ▼
[ 胶囊展示对勾徽章与文字，1.5s 后优雅淡出 ]
```

---

## 运行环境布局

应用不依赖系统 Python，所有运行组件按以下位置分布：

```
VoiceType.app/
└── Contents/
    ├── MacOS/
    │   ├── VoiceType          原生启动器（嵌入 Python 运行时）
    │   ├── bootstrap.py       首次运行引导：装依赖 + 下载模型
    │   ├── voice_type.py      主程序
    │   └── requirements.txt   运行依赖清单（单一事实来源）
    └── Resources/
        └── python/            内置 CPython 3.12 + PyObjC（随包分发）

~/.voicetype/pylibs/           首次启动时安装的运行依赖（约 1.2GB）
~/.voicetype/models/           首次启动时下载的本地模型（约 1.9GB）
```

依赖通过 `pip install --target` 安装到独立目录，与应用自身路径解耦：
应用被移动或重命名后无需重新配置。

---

## 构建与打包

### 1. 获取源码
```bash
 git clone https://github.com/QAQmmttyyy/VoiceType.git
 cd VoiceType
```

### 2. 准备内置 Python 运行时（首次构建前执行一次）
```bash
./fetch_runtime.sh
```
该脚本会下载 python-build-standalone 精简版 CPython 3.12，裁剪无关组件并预装 PyObjC，
产物位于 `runtime/`（约 68MB，已被 git 忽略）。

### 3. 构建并安装
```bash
./build.sh
```
自动完成编译、组装 Bundle、代码签名，并安装至 `/Applications/VoiceType.app`。

### 4. 制作分发镜像
```bash
./make_dmg.sh
```
生成带拖拽安装引导的 `VoiceType-Installer.dmg`（约 24MB）。

### 5. 运行体验
在「访达 -> 应用程序」中双击启动 **VoiceType**：
1. 首次打开会弹出配置窗口，自动下载本地 AI 引擎（约 500MB）与语音模型（约 1.9GB），仅需一次；
2. 按控制面板提示依次开启辅助功能、输入监控、麦克风三项权限，并点击「重启生效」；
3. 在任意软件（微信、备忘录、浏览器搜索框等）中点击输入框，**长按右侧 `Option (⌥)`** 说话，松开即可极速打字！

若模型下载中断，可在控制中心点击「重新下载 / 修复模型」重试。

### 6. 开发调试
```bash
./start_voice_type.sh
```

---

## 开源许可证

本项目基于 [MIT License](LICENSE) 许可协议开源。
