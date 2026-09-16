# AGENTS.md — VoiceType 开发者与智能体工程规范

本文档为参与 VoiceType 开发维护的 AI Agent 与工程师提供核心架构规范、安全防线及开发准则。

---

## 1. 核心架构与设计哲学

- **100% 离线与隐私至上**：所有音频采集、重采样、语音识别（SenseVoice）与标点处理（Qwen）必须且只能在本地芯片运行，严禁任何数据上传云端。
- **纯正 Apple HIG 规范**：
  - **绝无 Emoji**：UI 界面、状态栏与浮窗中严禁出现任何 Emoji 表情符，必须使用苹果官方 SF Symbols（如 `mic.fill`, `waveform`, `checkmark.circle.fill`）。
  - **双入口设计**：标准常规桌面应用，Dock 栏常驻图标 + 顶部状态栏图标双入口，防止刘海屏挤压导致失联。
  - **沉底黑曜石 HUD**：自适应单行紧凑药丸，鼠标事件穿透，绝对像素级垂直居中对齐。

---

## 2. 关键铁律与安全防线（不可违反）

### 2.1 严禁硬编码任何个人路径与敏感信息
- **禁止硬编码用户目录**：严禁在代码、构建脚本或配置中出现类似 `/Users/xxx` 的硬编码用户名或机器特异性路径。
- **获取用户目录的标准方式**：
  - C/Objective-C: `getenv("HOME")` 或 `getpwuid(getuid())->pw_dir`
  - Python: `os.path.expanduser("~")`
  - macOS Bundle 资源: `[NSBundle mainBundle]`、`[NSApp applicationIconImage]`
- **禁止访问保护目录**：禁止从 `/Applications` 越界读取 `~/Downloads`、`~/Desktop` 或 `~/Documents` 等受 macOS TCC 保护的目录，避免触发非必要的系统隐私告警。

### 2.2 进程互斥与生命周期规范
- **双重排他锁**：
  - 原生 C 启动器层：`/tmp/voicetype_app.lock` (`flock`)
  - Python 运行时层：`/tmp/voicetype_single.lock` (`fcntl.flock`)
  - 启动阶段秒级校验，若已被占用则立即静默退出，防止多实例并发抢占麦克风与内存暴涨。
- **PyObjC 退出防崩铁律**：
  - 在嵌入式 PyObjC 应用中，**严禁在进程退出前调用 `Py_Finalize()`**。
  - 原因：在 outer autoreleasepool 释放时，Objective-C 对象的 dealloc 回调会试图通过 `PyGILState_Ensure()` 回收 Python 内存，若 Python 虚拟机已提前释放将直接引发 SIGSEGV (Crash 11) 崩溃。统一采用系统级 `exit(0)` 或 `[NSApp terminate:nil]`。
- **PyObjC block 回调必须返回 None**：
  - 传给 `addOperationWithBlock_` 等方法的函数或 lambda 若返回值，PyObjC 会抛出
    `did not return None, expecting void return value` 并导致进程 abort。
    不要使用 `lambda: (a(), b())` 这种隐式返回元组的写法。

### 2.3 标点恢复与文本保真（Fidelity Check）
- 在使用本地语言模型（如 Qwen2.5-0.5B）进行标点还原时，必须保留**严格字符保真校验**：
  ```python
  strip_punct = lambda s: re.sub(r"[，。！？、：；,.!?;: ]", "", s)
  if strip_punct(fixed_text) == strip_punct(raw_text):
      return fixed_text
  return raw_text # 字符有任何增删改改动，立即无条件回退
  ```
  绝对禁止让 LLM 自行发挥、润色或篡改用户的原始口述内容。

### 2.4 音频处理零外部进程依赖
- 音频降采样必须使用纯内存算法（如 `scipy.signal.resample`），耗时控制在 3ms 以内。
- 严禁依赖通过外部子进程执行 `ffmpeg`，避免因沙盒、Finder 启动或环境变量缺失导致识别链路中断。

### 2.4.1 模拟按键必须自带修饰符标志
- 合成 `Cmd+V` 时，**必须用 `CGEventSetFlags` 在 V 键的按下与抬起事件上直接写入
  `kCGEventFlagMaskCommand`**，不能只依赖前面单独补发的那个 Cmd 按下事件。
- 原因：输入法（如搜狗拼音）是按**事件自身的 flags** 判断修饰键的。
  V 事件不带 Command 时，输入法会把它当成普通字母 `v`，
  触发「v+数字 / v+日期」的候选面板，用户看到的就是凭空多出一个 `v`。
- 这类问题**间歇出现**，因为输入法有时能从前面那个合成的 Cmd 事件里读到修饰状态、
  有时读不到。无法通过单次测试稳定复现，也不能因为「本机测不出来」就认为没有问题。
- 同一原则适用于任何需要修饰键的合成快捷键（Cmd+A/C/X、Shift+方向键等）。

### 2.4.2 录音期间必须压低系统输出音量
- 录音会通过空气把**外放的声音**一起收进麦克风（声学回声）。因此在 `_start_recording()`
  里必须**先压低系统音量、再启动录音器**，顺序不能反；`_stop_recording()` 里录音一结束就恢复。
- 音量通过 CoreAudio 的 `kAudioHardwareServiceDeviceProperty_VirtualMainVolume`（`vmvc`/`outp`）
  直接读写，**不要用 `osascript` 子进程**（单次约 100ms，会拖慢录音启动并漏录开头的外放声）。
- 恢复音量时必须**先确认当前值仍是我们压低后的值**，否则会覆盖用户期间的手动调整。
- 必须在**所有退出路径**上恢复音量（菜单退出、`applicationWillTerminate_`、重启），
  否则用户的系统音量会永久卡在低位。注意嵌入式 Python 里 `atexit` 不一定执行，不能只依赖它。

### 2.5 运行环境必须自包含
- **禁止依赖 Homebrew 或系统 Python**：应用的 Python 运行时随包分发（`Contents/Resources/python/`），
  不得在代码或构建脚本中引用 `/opt/homebrew/...` 或 `/usr/bin/python3`。
- **禁止硬链接本机 Python**：编译启动器时使用 `-Wl,-rpath,@executable_path/../Resources/python/lib`，
  确保 `otool -L` 中只出现 `@rpath/libpython3.12.dylib` 与系统框架。
- **依赖用 `pip install --target` 安装**到 `~/.voicetype/pylibs`，而不是 venv：
  venv 中的解释器是符号链接，应用一旦被移动或重命名即会失效；`--target` 目录与应用路径完全解耦。
- **依赖清单以 `requirements.txt` 为单一事实来源**，`bootstrap.py` 在运行时读取，
  不得在代码中再维护一份硬编码清单。
- **torch 固定为 2.11.0**：更新版本要求 macOS 14+，会抬高一档系统要求。
  升级前先确认新版本仍提供 `macosx_11_0_arm64` wheel。
- 发布前用 `pip install --only-binary=:all:` 审核是否存在必须现场编译的包：
  终端用户机器上通常没有 Xcode 命令行工具。已知 `crcmod`、`jieba`、`oss2` 仅有源码包，
  但均为纯 Python 构建或无编译器时自动降级，不会阻断安装。

### 2.6 离线运行与首次引导
- 应用承诺「配置完成后 100% 离线」。模型应存放在固定目录 `~/.voicetype/models` 并按绝对路径加载，
  同时设置 `HF_HUB_OFFLINE=1` 与 `TRANSFORMERS_OFFLINE=1`，避免 transformers 联网探测拖慢启动。
- **模型与依赖统一从 ModelScope（阿里云 CDN）下载**：国内直连实测 25MB/s 以上。
  不要引入 `hf-mirror.com` 等境外镜像——实测会无超时地卡死，且线上无法定位。
- **组件就绪判定必须使用完成标记文件**（`~/.voicetype_complete`），不能只看"目录非空"：
  下载中断留下的半成品目录会被误判为就绪，导致模型加载失败且无法自动修复。
  `bootstrap.py` 与 `voice_type.py` 的判定口径必须完全一致。
- **修复流程只补缺失项**：依赖已就绪时 `install_deps()` 必须跳过，
  否则用户只丢了一个模型却要连带重下 1.2GB 依赖。同理 `_download_one()` 只处理自己那一个组件。
- 控制面板中的体积必须**实时计算**，不得硬编码：硬编码值会随模型更新而失真。
  体积遍历耗时较长，必须在后台线程完成，不可放在主线程。
- 任何下载流程都必须有**停滞检测**：长时间无进度增长要抛错并在界面上给出重试入口，
  绝不能出现进度条永久卡住、用户无从判断的状态。
- 首次运行的环境与模型配置统一由 `bootstrap.py` 负责，主程序 `voice_type.py` 不再重复实现下载逻辑。
- `bootstrap.py` 完成后通过停止 run loop 返回退出码 42，由原生启动器重新执行一次引导脚本
  以进入主程序。**禁止在 worker 线程中使用 `os._exit()`**——它会直接终止整个进程，
  启动器拿不到退出码，导致应用静默退出。
- 依赖或模型缺失时，控制中心的「重新下载 / 修复模型」按钮通过独立进程唤起
  `bootstrap.py --repair`，不要在常驻主进程内执行安装逻辑。

### 2.6.1 推理设备与性能
- **优先使用 Apple Silicon 的 Metal GPU（`mps`）**，不可用时回退 `cpu`。
  实测（M2）启用 MPS 后：SenseVoice 提速约 9 倍，标点模型约 2.2 倍，端到端从约 2 秒降到约 0.5 秒。
- **切换设备后必须验证输出一致性**：MPS 与 CPU 的浮点差异可能改变识别结果，
  改动推理设备后要用同一段音频对比两者输出，确认完全一致才算通过。
- **必须在启动时预热**：MPS 首次推理需要编译内核（数秒）。若不在后台提前触发，
  这部分开销会计入用户第一次说话。预热失败要自动回退 CPU 并重载模型。
- **标点还原是主要耗时项**（约占端到端的三分之二）。优化延迟时优先看这一步。
- 性能数字写进文案前必须实测，并注明测试机型；不要写「一秒内」这类未经分档测量的说法。

### 2.6.2 严禁向 App 包内写入任何文件
- **必须设置 `PYTHONDONTWRITEBYTECODE=1`**（并在 Python 侧设 `sys.dont_write_bytecode = True`）。
  Python 默认会在 `__pycache__` 下生成 `.pyc`，而 `/Applications` 下的 App 包是可写的——
  这些新文件会破坏代码签名的资源密封，Gatekeeper 随即报
  `a sealed resource is missing or invalid`，**用户运行一次应用就损坏**。
- 实测影响可忽略：禁用字节码缓存后的导入耗时与有缓存时差异在噪声范围内（约 0.2s），
  因为耗时主要来自原生库加载，不需要在构建期预编译 `.pyc`。
- **构建流程必须能自动发现这类问题**：构建后用 `codesign --verify --deep --strict`
  校验，再实际运行一次应用并重新校验。只验证「签名了」不够，要验证「运行后签名仍然有效」。
- 任何新增的运行时写盘行为，都必须写到 `~/.voicetype` 或 `/tmp`，不得落在 App 包内。

### 2.6.3 发布前必须确认包是最新构建
- `make_dmg.sh` 已内置防呆：源码新于已构建二进制时直接中止。
- 修改任何代码后，必须重新执行 `./build.sh` **和** `./make_dmg.sh`；
  只重新构建而不重新公证，会把旧的、缺修复的二进制发出去（本项目已踩过一次）。

### 2.7 仓库清洁规范
- 严禁将 DMG 安装包、调试脚本、临时崩溃日志（`.ips`）或个人签名证书提交到 Git 仓库。
- `runtime/`（内置 Python 运行时）体积较大，由 `fetch_runtime.sh` 生成，已被 git 忽略，不得提交。
- 保持开源仓库仅包含纯净的源码、标准资源（`icon.icns`、`menu_icon@2x.png`）与编译配置。
