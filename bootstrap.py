#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceType 首次运行引导 (bootstrap)

职责：
  1. 检测本地运行依赖 (~/.voicetype/pylibs) 与 AI 模型是否就绪；
  2. 未就绪时弹出原生 AppKit 进度窗口，自动完成：
       - 通过 pip 把运行依赖安装到 ~/.voicetype/pylibs (路径与应用位置解耦)
       - 从 ModelScope 下载语音识别与标点模型到 ~/.voicetype/models
  3. 就绪后直接在当前进程内启动 voice_type.py 主程序。

退出码约定：
  42  安装已完成，需要重新启动本进程（避免在同一进程内二次调用 NSApp.run()）
  0   正常退出
  1   安装失败（窗口内已提示重试）
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

# 双保险：即使启动器没有注入 PYTHONDONTWRITEBYTECODE，也不允许写入 .pyc。
# 向 App 包内写入任何新文件都会破坏代码签名密封。
sys.dont_write_bytecode = True

HOME = os.path.expanduser("~")
VOICE_DIR = os.path.join(HOME, ".voicetype")
PYLIBS = os.path.join(VOICE_DIR, "pylibs")
MODELS_DIR = os.path.join(VOICE_DIR, "models")
APP_DIR = os.environ.get("VOICETYPE_APP_DIR") or os.path.dirname(os.path.abspath(__file__))

# 嵌入式解释器中 sys.executable 可能为空，优先使用启动器注入的内置解释器路径。
PYTHON_BIN = os.environ.get("VOICETYPE_PYTHON") or sys.executable

PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 模型统一从 ModelScope 下载：国内 CDN 直连，实测 25MB/s 以上，无需代理。
#
# 只需要一个模型。SenseVoice 自带标点与逆文本规整，
# 曾经额外挂过一个 Qwen2.5-0.5B 做标点修复，实测 67.7% 的输出
# 被保真校验丢弃、且会把英文翻译成中文，已移除。
# 详见 docs/标点方案.md。
# (repo_id, 本地目录名, 预估体积MB)
MODEL_SPECS = [
    ("iic/SenseVoiceSmall", "SenseVoiceSmall", 940),
]

STALL_TIMEOUT = 120  # 秒，下载体积长时间无增长即判定为停滞

# 下载完成后写入的标记文件。
# 仅凭“目录非空”无法区分“下载完成”与“下载中断”，
# 会把半成品误判为就绪，导致主程序加载失败且无法自动修复。
MODEL_MARKER = ".voicetype_complete"

EXIT_RESTART = 42


def _load_deps():
    """从 App 内置的 requirements.txt 读取待安装依赖，保证单一事实来源。"""
    req = os.path.join(APP_DIR, "requirements.txt")
    items = []
    with open(req, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    if not items:
        raise RuntimeError("requirements.txt 为空")
    return items


def _dir_size_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / 1048576.0


def model_dir(local_name):
    return os.path.join(MODELS_DIR, local_name)


def _model_ready(local_name):
    return os.path.exists(os.path.join(model_dir(local_name), MODEL_MARKER))


def deps_ready():
    return os.path.exists(os.path.join(PYLIBS, MODEL_MARKER))


def models_ready():
    return all(_model_ready(name) for _repo, name, _mb in MODEL_SPECS)


# ========================================================
# 原生设置窗口
# ========================================================
def _build_window():
    import AppKit as ak

    w, h = 500, 240
    screen = ak.NSScreen.mainScreen()
    sf = screen.frame() if screen else ak.NSMakeRect(0, 0, 1440, 900)
    x = (sf.size.width - w) / 2
    y = (sf.size.height - h) / 2 + 60

    win = ak.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ak.NSMakeRect(x, y, w, h),
        ak.NSWindowStyleMaskTitled | ak.NSWindowStyleMaskClosable,
        ak.NSBackingStoreBuffered,
        False,
    )
    win.setTitle_("VoiceType 引擎配置")
    win.setReleasedWhenClosed_(False)

    cv = win.contentView()

    icon_view = ak.NSImageView.alloc().initWithFrame_(ak.NSMakeRect(28, h - 74, 48, 48))
    icon = ak.NSApp.applicationIconImage()
    if icon:
        icon_view.setImage_(icon)
    cv.addSubview_(icon_view)

    title = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(90, h - 54, 390, 24))
    title.setBezeled_(False)
    title.setDrawsBackground_(False)
    title.setEditable_(False)
    title.setSelectable_(False)
    title.setFont_(ak.NSFont.systemFontOfSize_weight_(16, ak.NSFontWeightBold))
    title.setStringValue_("正在配置本地离线引擎")
    cv.addSubview_(title)

    subtitle = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(90, h - 76, 390, 18))
    subtitle.setBezeled_(False)
    subtitle.setDrawsBackground_(False)
    subtitle.setEditable_(False)
    subtitle.setSelectable_(False)
    subtitle.setFont_(ak.NSFont.systemFontOfSize_(12))
    subtitle.setTextColor_(ak.NSColor.secondaryLabelColor())
    subtitle.setStringValue_("首次运行需配置一次，完成后可完全离线使用")
    cv.addSubview_(subtitle)

    bar = ak.NSProgressIndicator.alloc().initWithFrame_(ak.NSMakeRect(28, 96, 444, 16))
    bar.setIndeterminate_(False)
    bar.setMinValue_(0.0)
    bar.setMaxValue_(100.0)
    bar.setDoubleValue_(0.0)
    cv.addSubview_(bar)

    status = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(28, 62, 444, 20))
    status.setBezeled_(False)
    status.setDrawsBackground_(False)
    status.setEditable_(False)
    status.setSelectable_(False)
    status.setFont_(ak.NSFont.systemFontOfSize_(12))
    status.setStringValue_("正在准备...")
    cv.addSubview_(status)

    retry = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(w - 150, 18, 122, 30))
    retry.setTitle_("重试")
    retry.setBezelStyle_(ak.NSBezelStyleRounded)
    retry.setHidden_(True)
    cv.addSubview_(retry)

    return win, bar, status, title, retry


# ========================================================
# 安装流程
# ========================================================
class Installer:
    def __init__(self, log):
        self.log = log

    # ---------- 依赖安装 ----------
    def _pip_total(self, deps):
        cmd = [PYTHON_BIN, "-m", "pip", "install", "--dry-run", "--quiet",
               "--report", "-", "-i", PYPI_INDEX, "--target", PYLIBS] + deps
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            data = json.loads(out.stdout)
            return max(1, len(data.get("install", [])))
        except Exception:
            return 30

    def install_deps(self):
        deps = _load_deps()
        self.log(5, "正在下载运行依赖，请保持网络连接...")

        if os.path.isdir(PYLIBS):
            shutil.rmtree(PYLIBS, ignore_errors=True)
        os.makedirs(PYLIBS, exist_ok=True)

        total = self._pip_total(deps)

        cmd = [PYTHON_BIN, "-m", "pip", "install", "--progress-bar", "off",
               "--no-warn-script-location", "-i", PYPI_INDEX, "--target", PYLIBS] + deps
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        done = 0
        for line in proc.stdout:
            if line.startswith("Downloading ") or line.startswith("Using cached "):
                done += 1
                pct = 5 + int(53 * min(done, total) / total)
                self.log(pct, "正在下载运行依赖组件 (%d/%d)..." % (min(done, total), total))
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("运行依赖安装失败")
        # 写入完成标记，与主程序的就绪判定保持一致
        with open(os.path.join(PYLIBS, MODEL_MARKER), "w"):
            pass
        if not deps_ready():
            raise RuntimeError("运行依赖安装不完整")

    # ---------- 模型下载 ----------
    def download_models(self):
        if not deps_ready():
            raise RuntimeError("运行依赖未就绪")
        if PYLIBS not in sys.path:
            sys.path.insert(0, PYLIBS)

        os.makedirs(MODELS_DIR, exist_ok=True)

        span = 42 // len(MODEL_SPECS)
        for index, (repo_id, local_name, est_mb) in enumerate(MODEL_SPECS):
            if _model_ready(local_name):
                continue
            start = 58 + index * span
            self._download_one(repo_id, local_name, est_mb, start, span)

    def _download_one(self, repo_id, local_name, est_mb, start_pct, span_pct):
        label = local_name
        target = model_dir(local_name)
        result = {}

        def worker():
            try:
                from modelscope import snapshot_download
                # 清理可能存在的半成品目录，避免新旧文件混杂
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                snapshot_download(repo_id, local_dir=target)
                with open(os.path.join(target, MODEL_MARKER), "w"):
                    pass
                result["ok"] = True
            except Exception as exc:  # noqa: BLE001
                result["err"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        last_size = 0.0
        last_change = time.time()

        while thread.is_alive():
            time.sleep(0.6)
            size = _dir_size_mb(target)
            if size > last_size + 0.5:
                last_size = size
                last_change = time.time()
            elif time.time() - last_change > STALL_TIMEOUT:
                raise RuntimeError("%s 下载停滞，请检查网络后重试" % label)

            ratio = min(1.0, last_size / float(est_mb))
            pct = start_pct + int(span_pct * ratio)
            self.log(min(pct, start_pct + span_pct - 1),
                     "正在下载 %s (%d / ~%d MB)" % (label, int(last_size), est_mb))

        thread.join()

        if "err" in result:
            raise RuntimeError("%s 下载失败：%s" % (label, result["err"]))
        if not _model_ready(local_name):
            raise RuntimeError("%s 下载不完整" % label)
        self.log(start_pct + span_pct, "%s 已就绪" % label)

    def run(self):
        # 仅在依赖确实缺失时才重装。否则“只缺一个模型”也会连带重下 1.2GB 依赖。
        if deps_ready():
            self.log(58, "运行依赖已就绪")
        else:
            self.install_deps()
        self.download_models()
        self.log(100, "配置完成，正在启动 VoiceType...")


# ========================================================
# 入口
# ========================================================
def launch_main_app():
    """依赖与模型均就绪，直接在当前进程内启动主程序。"""
    if PYLIBS not in sys.path:
        sys.path.insert(0, PYLIBS)
    # 模型已落盘为本地目录，禁止任何联网探测，确保启动速度与离线承诺。
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    import runpy
    target = os.path.join(APP_DIR, "voice_type.py")
    runpy.run_path(target, run_name="__main__")


def main():
    repair = "--repair" in sys.argv
    if not repair and deps_ready() and models_ready():
        launch_main_app()
        return 0

    import AppKit as ak
    from Foundation import NSObject, NSOperationQueue

    app = ak.NSApplication.sharedApplication()
    app.setActivationPolicy_(ak.NSApplicationActivationPolicyRegular)

    win, bar, status, title, retry = _build_window()
    state = {"running": False, "exit_code": 0}

    def finish(code):
        """关闭窗口并停止主循环，由 main() 返回退出码。

        此处不能用 os._exit()：那会直接终止整个进程，
        启动器无法拿到退出码并重新执行引导脚本。
        """
        state["exit_code"] = code

        def _apply():
            win.orderOut_(None)
            app.stop_(None)
            event = ak.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                ak.NSEventTypeApplicationDefined, ak.NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
            app.postEvent_atStart_(event, True)

        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)

    def set_progress(pct, msg):
        def _apply():
            bar.setDoubleValue_(float(pct))
            status.setStringValue_(msg)
        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)

    def set_title(text):
        def _apply():
            title.setStringValue_(text)
        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)

    def show_retry():
        def _apply():
            retry.setHidden_(False)
        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)

    def start_install():
        if state["running"]:
            return
        state["running"] = True
        retry.setHidden_(True)
        set_title("正在配置本地离线引擎")

        def worker():
            try:
                Installer(set_progress).run()
                time.sleep(0.8)
                # --repair 由主程序唤起，完成后直接退出，不再重启整个 App
                finish(0 if repair else EXIT_RESTART)
            except Exception as exc:  # noqa: BLE001
                state["running"] = False
                set_title("配置未完成")
                set_progress(0, "安装失败：%s" % exc)
                show_retry()

        threading.Thread(target=worker, daemon=True).start()

    class BootstrapController(NSObject):
        def windowShouldClose_(self, sender):
            os._exit(0)
            return False

        def doRetry_(self, sender):
            start_install()

    controller = BootstrapController.alloc().init()
    win.setDelegate_(controller)
    retry.setTarget_(controller)
    retry.setAction_("doRetry:")

    def _show_window():
        app.activateIgnoringOtherApps_(True)
        win.makeKeyAndOrderFront_(None)

    NSOperationQueue.mainQueue().addOperationWithBlock_(_show_window)

    start_install()
    app.run()
    return state["exit_code"]


if __name__ == "__main__":
    sys.exit(main())