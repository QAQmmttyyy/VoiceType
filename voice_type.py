#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceType — macOS 桌面语音输入 App
- 架构：Dock 栏常驻 + 菜单栏双入口 + 原生权限与偏好控制中心面板
- 状态栏：苹果原生 SF Symbol 模板图标 (mic.fill)
- 浮窗：屏幕下方黑曜石超薄自适应胶囊 (严格单行，无折行，0误差垂直居中)
- 权限机制：
    1. 原生 Mach-O 二进制请求权限，TCC 授权窗口 100% 显示 "VoiceType"
    2. 首次启动或权限缺失时，自动唤起原生设置面板逐项引导
    3. 支持一键安全重启生效 (open -n 独立实例接力)
    4. 麦克风、辅助功能、输入监控三大权限实时动态检测与自愈
- 音频处理：纯内存 scipy 极速高保真重采样 (彻底解耦外部 ffmpeg)
- 触发：按住键盘右侧 Option (⌥) 说话，松开自动识别并键入当前输入框
- 引擎：阿里 SenseVoice（自带标点与逆文本规整，不依赖额外的标点模型）
"""
import os, time, math, threading, tempfile, subprocess, re, gc, fcntl, sys, atexit
import numpy as np
import scipy.signal
import soundfile as sf
import AppKit as ak
import Quartz
import ApplicationServices as app_srv
import AVFoundation as av_foundation
import objc
from Foundation import NSObject, NSOperationQueue

# ==================== 0. 系统级三大独立权限底层接口 ====================
import ctypes
try:
    _cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _cg.CGPreflightListenEventAccess.restype = ctypes.c_bool
    _cg.CGRequestListenEventAccess.restype = ctypes.c_bool
    def check_input_monitoring():
        return bool(_cg.CGPreflightListenEventAccess())
    def request_input_monitoring():
        return bool(_cg.CGRequestListenEventAccess())
except Exception:
    def check_input_monitoring():
        return False
    def request_input_monitoring():
        return False

def check_accessibility():
    return bool(app_srv.AXIsProcessTrusted())

def check_microphone():
    return bool(av_foundation.AVCaptureDevice.authorizationStatusForMediaType_(
        av_foundation.AVMediaTypeAudio) == 3)


# ========================================================
# 核心防线：Python 层双保险物理互斥锁 (杜绝多开)
# ========================================================
_py_lock_fd = None
def ensure_single_instance():
    global _py_lock_fd
    try:
        _py_lock_fd = open("/tmp/voicetype_single.lock", "w")
        fcntl.flock(_py_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        sys.exit(0)

ensure_single_instance()

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

# 模型由 bootstrap.py 下载到固定本地目录，主程序直接按路径加载，不走任何联网缓存逻辑。
VOICE_DIR = os.path.expanduser("~/.voicetype")
PYLIBS_DIR = os.path.join(VOICE_DIR, "pylibs")
MODELS_DIR = os.path.join(VOICE_DIR, "models")
SENSEVOICE_DIR = os.path.join(MODELS_DIR, "SenseVoiceSmall")

# 与 bootstrap.py 保持一致：以完成标记判定就绪，区分“下载完成”与“下载中断”。
MODEL_MARKER = ".voicetype_complete"

try:
    import torch
    torch.set_num_threads(2)
except Exception:
    pass

TRIGGER_HOLD = 0    # 长按说话 (按住录音，松开键入)
TRIGGER_TOGGLE = 1  # 单击切换 (点按开始录音，再次点按键入)


def _dir_size_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / 1048576.0


def _format_size(mb):
    if mb >= 1024:
        return "%.1f GB" % (mb / 1024.0)
    return "%d MB" % int(mb)


def check_deps_ready():
    return os.path.isdir(os.path.join(PYLIBS_DIR, "torch"))


def check_sensevoice_ready():
    return os.path.exists(os.path.join(SENSEVOICE_DIR, MODEL_MARKER))


# ==================== 1. 语音识别引擎 ====================
def _pick_device():
    """优先使用 Apple Silicon 的 Metal GPU，不可用时回退 CPU。

    实测（M2）：SenseVoice 提速约 9 倍，且输出完全一致。
    """
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


DEVICE = _pick_device()

# 启动预热结果：MPS 内核首次编译较慢，需在后台提前触发，
# 否则用户第一次说话会多等几秒。
WARMUP_STATE = {"ok": False, "device": DEVICE}


class SenseEngine:
    _model = None
    _lock = threading.Lock()

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._model = None

    @classmethod
    def get(cls):
        if cls._model is None:
            with cls._lock:
                if cls._model is None:
                    from funasr import AutoModel
                    cls._model = AutoModel(
                        model=SENSEVOICE_DIR,
                        disable_update=True,
                        device=WARMUP_STATE["device"],
                    )
        return cls._model

    @staticmethod
    def transcribe(wav_path):
        model = SenseEngine.get()
        res = model.generate(input=wav_path, language="auto", use_itn=True)
        text = ""
        for r in res:
            t = re.sub(r"<\|.*?\|>", "", r.get("text", ""))
            text += t
        return text.strip()


def warmup_engines(notify=None):
    """后台预热识别模型。

    MPS 首次推理需要编译内核（约数秒），若不在启动时触发，
    用户第一次说话就会把这部分开销算进去。若所选设备预热失败，
    自动回退到 CPU 并重载模型。
    """
    for device in (DEVICE, "cpu"):
        try:
            WARMUP_STATE["device"] = device
            SenseEngine.reset()

            # 语音识别预热：一段 1 秒的静音音频
            sense = SenseEngine.get()
            sr = 16000
            silence = np.zeros(sr, dtype=np.float32)
            tmp = tempfile.mktemp(suffix=".wav")
            sf.write(tmp, silence, sr)
            try:
                sense.generate(input=tmp, language="auto", use_itn=True)
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

            WARMUP_STATE["ok"] = True
            print("[VoiceType] 计算设备: %s" % device, file=sys.stderr, flush=True)
            if notify:
                notify(device)
            return device
        except Exception as exc:  # noqa: BLE001
            print("[VoiceType] 设备 %s 预热失败，尝试回退: %s" % (device, exc),
                  file=sys.stderr, flush=True)
            continue
    print("[VoiceType] 所有计算设备预热失败", file=sys.stderr, flush=True)
    return None


_t2s = None
def to_simplified(text):
    global _t2s
    if _t2s is None:
        import opencc
        _t2s = opencc.OpenCC("t2s")
    return _t2s.convert(text)


# ==================== 2. 麦克风录音器 ====================
class SystemVolume:
    """录音期间自动压低系统输出音量，避免外放的声音被一起录进去。

    直接通过 CoreAudio 读写默认输出设备的虚拟主音量，不走 osascript 子进程
    （单次 osascript 约 100ms，会拖慢录音启动）。
    任何一步失败都静默忽略，绝不因为音量控制问题影响录音本身。
    """

    DUCK_LEVEL = 0.10  # 压低到的音量（0.0 ~ 1.0）

    _lib = None
    _addr_cls = None

    def __init__(self):
        self._saved = None
        self._lock = threading.Lock()

    # ---------- 底层 CoreAudio 访问 ----------
    @classmethod
    def _load(cls):
        if cls._lib is not None:
            return cls._lib
        from ctypes import (CDLL, Structure, c_uint32, c_int32,
                            c_void_p, POINTER)

        class _Addr(Structure):
            _fields_ = [("sel", c_uint32), ("scope", c_uint32), ("elem", c_uint32)]

        lib = CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        lib.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(_Addr), c_uint32,
                                                  c_void_p, POINTER(c_uint32), c_void_p]
        lib.AudioObjectGetPropertyData.restype = c_int32
        lib.AudioObjectSetPropertyData.argtypes = [c_uint32, POINTER(_Addr), c_uint32,
                                                  c_void_p, c_uint32, c_void_p]
        lib.AudioObjectSetPropertyData.restype = c_int32

        cls._lib = lib
        cls._addr_cls = _Addr
        return lib

    @staticmethod
    def _fourcc(s):
        return int.from_bytes(s.encode(), "big")

    @classmethod
    def _volume_addr(cls):
        # kAudioHardwareServiceDeviceProperty_VirtualMainVolume，作用域为输出
        return cls._addr_cls(cls._fourcc("vmvc"), cls._fourcc("outp"), 0)

    @classmethod
    def _default_output(cls):
        from ctypes import c_uint32, byref
        lib = cls._load()
        addr = cls._addr_cls(cls._fourcc("dOut"), cls._fourcc("glob"), 0)
        size, dev = c_uint32(4), c_uint32(0)
        st = lib.AudioObjectGetPropertyData(1, byref(addr), 0, None,
                                            byref(size), byref(dev))
        return dev.value if st == 0 and dev.value else None

    @classmethod
    def _get(cls):
        from ctypes import c_uint32, c_float, byref
        try:
            dev = cls._default_output()
            if dev is None:
                return None
            addr = cls._volume_addr()
            size, val = c_uint32(4), c_float(0.0)
            st = cls._load().AudioObjectGetPropertyData(dev, byref(addr), 0, None,
                                                        byref(size), byref(val))
            return float(val.value) if st == 0 else None
        except Exception:
            return None

    @classmethod
    def _set(cls, value):
        from ctypes import c_uint32, c_float, byref
        try:
            dev = cls._default_output()
            if dev is None:
                return False
            addr = cls._volume_addr()
            val = c_float(float(value))
            st = cls._load().AudioObjectSetPropertyData(dev, byref(addr), 0, None,
                                                        c_uint32(4), byref(val))
            return st == 0
        except Exception:
            return False

    # ---------- 对外接口 ----------
    def duck(self):
        """录音开始：记住当前音量并压低。"""
        with self._lock:
            if self._saved is not None:
                return
            current = self._get()
            if current is None or current <= self.DUCK_LEVEL:
                return
            if self._set(self.DUCK_LEVEL):
                self._saved = current

    def restore(self):
        """录音结束：恢复原音量。

        只在音量仍等于我们压低后的值时恢复，避免覆盖用户期间手动调的音量。
        同时保证无论录音成功与否都不会把音量永久留在低位。
        """
        with self._lock:
            if self._saved is None:
                return
            current = self._get()
            if current is not None and abs(current - self.DUCK_LEVEL) < 0.005:
                self._set(self._saved)
            self._saved = None


class Recorder:
    def __init__(self, device=None):
        self.device = device
        self.buf = []
        self.stream = None
        self.sr = 44100

    def start(self):
        import sounddevice as sd
        try:
            info = sd.query_devices(self.device, "input") if self.device is not None \
                else sd.query_devices(kind="input")
            self.sr = int(info["default_samplerate"])
        except Exception:
            self.sr = 44100
        self.buf = []
        self.stream = sd.InputStream(
            device=self.device, samplerate=self.sr, channels=1, dtype="float32",
            callback=lambda indata, frames, t, status: self.buf.append(indata.copy()))
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if not self.buf:
            return None
        return np.concatenate(self.buf, axis=0).flatten()

    def to_wav(self, arr, path):
        sf.write(path, arr, self.sr)


# ==================== 3. 苹果原生自适应黑曜石胶囊 (HUD) ====================
class AppleStyleHUD:
    def __init__(self):
        self.panel = None
        self.container = None
        self.icon_view = None
        self.spin_layer = None
        self.label = None
        self._hide_timer = None
        self.font = ak.NSFont.systemFontOfSize_weight_(13.5, ak.NSFontWeightMedium)
        self._init_ui()

    def _init_ui(self):
        scr = ak.NSScreen.mainScreen()
        sf = scr.frame() if scr else ak.NSMakeRect(0, 0, 1470, 956)

        w, h = 130, 36
        x = (sf.size.width - w) / 2
        y = 120

        self.panel = ak.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ak.NSMakeRect(x, y, w, h),
            ak.NSWindowStyleMaskBorderless,
            ak.NSBackingStoreBuffered,
            False
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(ak.NSColor.clearColor())
        self.panel.setLevel_(ak.NSScreenSaverWindowLevel)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setHasShadow_(True)
        self.panel.setIgnoresMouseEvents_(True)

        self.panel.setCollectionBehavior_(
            ak.NSWindowCollectionBehaviorCanJoinAllSpaces | 
            ak.NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        self.container = ak.NSView.alloc().initWithFrame_(ak.NSMakeRect(0, 0, w, h))
        self.container.setWantsLayer_(True)
        layer = self.container.layer()
        layer.setBackgroundColor_(ak.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.08, 0.10, 0.92).CGColor())
        layer.setCornerRadius_(h / 2)
        layer.setBorderWidth_(0.5)
        layer.setBorderColor_(ak.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.16).CGColor())

        self.icon_view = ak.NSImageView.alloc().initWithFrame_(ak.NSMakeRect(14, 10, 16, 16))
        self.icon_view.setImageScaling_(ak.NSImageScaleProportionallyUpOrDown)
        self.icon_view.setWantsLayer_(True)

        self.spin_layer = Quartz.CALayer.layer()
        self.spin_layer.setFrame_(ak.NSMakeRect(0, 0, 16, 16))
        self.spin_layer.setAnchorPoint_(ak.NSMakePoint(0.5, 0.5))
        self.spin_layer.setPosition_(ak.NSMakePoint(8, 8))
        self.icon_view.layer().addSublayer_(self.spin_layer)

        self.label = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(38, 7, 80, 22))
        self.label.setBezeled_(False)
        self.label.setDrawsBackground_(False)
        self.label.setEditable_(False)
        self.label.setSelectable_(False)
        self.label.setTextColor_(ak.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.96))
        self.label.setFont_(self.font)
        self.label.setUsesSingleLineMode_(True)
        self.label.cell().setLineBreakMode_(ak.NSLineBreakByClipping)
        self.label.cell().setWraps_(False)
        self.label.cell().setScrollable_(False)

        self.container.addSubview_(self.icon_view)
        self.container.addSubview_(self.label)
        self.panel.contentView().addSubview_(self.container)

    def show(self, state, text, auto_hide=0):
        def _apply():
            if self._hide_timer:
                self._hide_timer.cancel()
                self._hide_timer = None

            self.spin_layer.removeAllAnimations()
            self.spin_layer.setContents_(None)

            if state == "recording":
                img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("waveform", None)
                self.icon_view.setImage_(img)
                self.icon_view.setContentTintColor_(ak.NSColor.systemGreenColor())
            elif state == "transcribing":
                self.icon_view.setImage_(None)
                img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("arrow.triangle.2.circlepath", None)
                tinted = img.copy()
                tinted.lockFocus()
                ak.NSColor.systemYellowColor().set()
                ak.NSRectFillUsingOperation(ak.NSMakeRect(0, 0, 16, 16), ak.NSCompositingOperationSourceAtop)
                tinted.unlockFocus()
                cg = tinted.CGImageForProposedRect_context_hints_(None, None, None)[0]
                self.spin_layer.setContents_(cg)

                anim = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
                anim.setFromValue_(0)
                anim.setToValue_(-2 * math.pi)
                anim.setDuration_(0.9)
                anim.setRepeatCount_(float('inf'))
                self.spin_layer.addAnimation_forKey_(anim, "spin")
            elif state == "alert":
                img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("exclamationmark.triangle.fill", None)
                self.icon_view.setImage_(img)
                self.icon_view.setContentTintColor_(ak.NSColor.systemYellowColor())
            else:  # "done"
                img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("checkmark.circle.fill", None)
                self.icon_view.setImage_(img)
                self.icon_view.setContentTintColor_(ak.NSColor.systemGreenColor())

            display_text = text if len(text) <= 32 else text[:30] + "…"
            self.label.setStringValue_(display_text)
            self.label.sizeToFit()

            tr = self.label.cell().titleRectForBounds_(self.label.bounds())
            tw = self.label.frame().size.width

            h = 36.0
            glyph_center = tr.origin.y + (tr.size.height / 2.0)
            label_y = (h / 2.0) - glyph_center

            w = int(14 + 16 + 8 + tw + 16)
            icon_x = 14
            icon_y = (h - 16) / 2.0
            label_x = 14 + 16 + 8

            self.icon_view.setFrame_(ak.NSMakeRect(icon_x, icon_y, 16, 16))
            self.label.setFrame_(ak.NSMakeRect(label_x, label_y, tw + 2, self.label.frame().size.height))

            scr = ak.NSScreen.mainScreen()
            sf = scr.frame() if scr else ak.NSMakeRect(0, 0, 1470, 956)
            x = (sf.size.width - w) / 2
            y = 120

            self.panel.setFrame_display_(ak.NSMakeRect(x, y, w, h), True)
            self.container.setFrame_(ak.NSMakeRect(0, 0, w, h))
            self.container.layer().setCornerRadius_(h / 2)

            self.panel.orderFrontRegardless()

            if auto_hide > 0:
                self._hide_timer = threading.Timer(auto_hide, self.hide)
                self._hide_timer.daemon = True
                self._hide_timer.start()

        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)

    def hide(self):
        def _apply():
            if self.panel:
                self.panel.orderOut_(None)
        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)


# ==================== 4. 原生设置与权限控制中心面板 ====================
class SettingsWindowDelegate(NSObject):
    def windowShouldClose_(self, sender):
        sender.orderOut_(None)
        return False


class SettingsWindow:
    def __init__(self, app_controller):
        self.app = app_controller
        self.win = None
        self.delegate = SettingsWindowDelegate.alloc().init()
        self.perm_items = {}
        self.engine_items = {}
        self.engine_state = {}
        self._init_ui()

    def _init_ui(self):
        w, h = 460, 700
        scr = ak.NSScreen.mainScreen()
        sf = scr.frame() if scr else ak.NSMakeRect(0, 0, 1440, 900)
        x = (sf.size.width - w) / 2
        y = (sf.size.height - h) / 2 + 50

        self.win = ak.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ak.NSMakeRect(x, y, w, h),
            ak.NSWindowStyleMaskTitled | ak.NSWindowStyleMaskClosable | ak.NSWindowStyleMaskMiniaturizable,
            ak.NSBackingStoreBuffered,
            False
        )
        self.win.setTitle_("VoiceType 设置与控制中心")
        self.win.setDelegate_(self.delegate)
        self.win.setReleasedWhenClosed_(False)

        cv = self.win.contentView()

        # 1. 顶部 Header (应用图标 + 标题 + 简介)
        app_icon = ak.NSImageView.alloc().initWithFrame_(ak.NSMakeRect(24, h - 68, 44, 44))
        icon_img = ak.NSApp.applicationIconImage()
        if not icon_img:
            bundle_icon = ak.NSBundle.mainBundle().pathForImageResource_("icon.icns")
            if bundle_icon and os.path.exists(bundle_icon):
                icon_img = ak.NSImage.alloc().initWithContentsOfFile_(bundle_icon)
        if icon_img:
            app_icon.setImage_(icon_img)
        cv.addSubview_(app_icon)

        title_lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(80, h - 52, 280, 24))
        title_lbl.setBezeled_(False)
        title_lbl.setDrawsBackground_(False)
        title_lbl.setEditable_(False)
        title_lbl.setFont_(ak.NSFont.systemFontOfSize_weight_(17, ak.NSFontWeightBold))
        title_lbl.setStringValue_("VoiceType 语音输入")
        cv.addSubview_(title_lbl)

        sub_lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(80, h - 72, 364, 18))
        sub_lbl.setBezeled_(False)
        sub_lbl.setDrawsBackground_(False)
        sub_lbl.setEditable_(False)
        sub_lbl.setFont_(ak.NSFont.systemFontOfSize_(12))
        sub_lbl.setTextColor_(ak.NSColor.secondaryLabelColor())
        sub_lbl.setStringValue_("本地离线识别 · 长按右侧 Option (⌥) 即时打字")
        cv.addSubview_(sub_lbl)

        # 2. 核心系统权限卡片 (高度 195：内聚包含三大权限 + 清理残留与重启生效)
        pbox = ak.NSBox.alloc().initWithFrame_(ak.NSMakeRect(20, h - 275, w - 40, 195))
        pbox.setTitle_("核心系统权限")
        pbox.setTitleFont_(ak.NSFont.systemFontOfSize_weight_(12.5, ak.NSFontWeightMedium))

        perms = [
            ("accessibility", "辅助功能 (模拟键入)", "openAccessibilitySettings:", 135),
            ("input_mon", "输入监控 (快捷键监听)", "openInputMonitoringSettings:", 98),
            ("microphone", "麦克风访问 (音频录音)", "openMicrophoneSettings:", 61)
        ]
        for key, name, action, py in perms:
            lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(16, py, 190, 20))
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setFont_(ak.NSFont.systemFontOfSize_(13))
            lbl.setStringValue_(name)
            pbox.contentView().addSubview_(lbl)

            iv = ak.NSImageView.alloc().initWithFrame_(ak.NSMakeRect(235, py + 2, 16, 16))
            iv.setImageScaling_(ak.NSImageScaleProportionallyUpOrDown)
            pbox.contentView().addSubview_(iv)

            stat_txt = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(255, py, 60, 20))
            stat_txt.setBezeled_(False)
            stat_txt.setDrawsBackground_(False)
            stat_txt.setEditable_(False)
            stat_txt.setFont_(ak.NSFont.systemFontOfSize_(12))
            pbox.contentView().addSubview_(stat_txt)

            btn = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(325, py - 4, 75, 26))
            btn.setTitle_("去设置")
            btn.setBezelStyle_(ak.NSBezelStyleRounded)
            btn.setTarget_(self.app)
            btn.setAction_(action)
            pbox.contentView().addSubview_(btn)

            self.perm_items[key] = (iv, stat_txt, btn)

        # 权限操作内聚在权限卡片内部
        clean_btn = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(16, 12, 115, 28))
        clean_btn.setTitle_("清理权限残留")
        clean_btn.setBezelStyle_(ak.NSBezelStyleRounded)
        clean_btn.setTarget_(self.app)
        clean_btn.setAction_("resetPermissionsCache:")
        pbox.contentView().addSubview_(clean_btn)

        restart_btn = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(w - 40 - 135, 12, 120, 28))
        restart_btn.setTitle_("重启生效")
        restart_btn.setBezelStyle_(ak.NSBezelStyleRounded)
        restart_btn.setKeyEquivalent_("\r") # 回车键主操作
        restart_btn.setTarget_(self.app)
        restart_btn.setAction_("restartApp:")
        pbox.contentView().addSubview_(restart_btn)

        cv.addSubview_(pbox)

        # 3. 动态提示 Banner
        self.tip_lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(25, h - 306, w - 50, 24))
        self.tip_lbl.setBezeled_(False)
        self.tip_lbl.setDrawsBackground_(False)
        self.tip_lbl.setEditable_(False)
        self.tip_lbl.setFont_(ak.NSFont.systemFontOfSize_(11))
        self.tip_lbl.setTextColor_(ak.NSColor.secondaryLabelColor())
        self.tip_lbl.setStringValue_("提示：更改系统权限后，点击上方「重启生效」以立即载入新权限。")
        cv.addSubview_(self.tip_lbl)

        # 4. 本地离线 AI 引擎与模型状态卡片 (组件状态 + 磁盘占用 + 修复入口)
        engine_box = ak.NSBox.alloc().initWithFrame_(ak.NSMakeRect(20, h - 520, w - 40, 205))
        engine_box.setTitle_("本地离线 AI 引擎组件")
        engine_box.setTitleFont_(ak.NSFont.systemFontOfSize_weight_(12.5, ak.NSFontWeightMedium))

        engine_meta = [
            ("deps", "运行依赖 (torch 等)", 143),
            ("sensevoice", "SenseVoice 语音识别", 98),
        ]
        for key, name, py in engine_meta:
            lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(16, py, 190, 20))
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setFont_(ak.NSFont.systemFontOfSize_(13))
            lbl.setStringValue_(name)
            engine_box.contentView().addSubview_(lbl)

            iv = ak.NSImageView.alloc().initWithFrame_(ak.NSMakeRect(235, py + 2, 16, 16))
            iv.setImageScaling_(ak.NSImageScaleProportionallyUpOrDown)
            engine_box.contentView().addSubview_(iv)

            stat_txt = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(255, py, 150, 20))
            stat_txt.setBezeled_(False)
            stat_txt.setDrawsBackground_(False)
            stat_txt.setEditable_(False)
            stat_txt.setFont_(ak.NSFont.systemFontOfSize_(12))
            engine_box.contentView().addSubview_(stat_txt)

            self.engine_items[key] = (iv, stat_txt)

        self.usage_lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(16, 46, w - 72, 18))
        self.usage_lbl.setBezeled_(False)
        self.usage_lbl.setDrawsBackground_(False)
        self.usage_lbl.setEditable_(False)
        self.usage_lbl.setFont_(ak.NSFont.systemFontOfSize_(11.5))
        self.usage_lbl.setTextColor_(ak.NSColor.secondaryLabelColor())
        self.usage_lbl.setStringValue_("")
        engine_box.contentView().addSubview_(self.usage_lbl)

        reinstall_btn = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(16, 12, 150, 28))
        reinstall_btn.setTitle_("重新下载 / 修复组件")
        reinstall_btn.setBezelStyle_(ak.NSBezelStyleRounded)
        reinstall_btn.setTarget_(self.app)
        reinstall_btn.setAction_("reinstallModels:")
        engine_box.contentView().addSubview_(reinstall_btn)

        clean_cache_btn = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(w - 40 - 135, 12, 120, 28))
        clean_cache_btn.setTitle_("清除模型文件")
        clean_cache_btn.setBezelStyle_(ak.NSBezelStyleRounded)
        clean_cache_btn.setTarget_(self.app)
        clean_cache_btn.setAction_("cleanModelCache:")
        engine_box.contentView().addSubview_(clean_cache_btn)

        cv.addSubview_(engine_box)

        # 5. 运行偏好卡片 (标明即时生效，无需重启)
        pref_box = ak.NSBox.alloc().initWithFrame_(ak.NSMakeRect(20, 58, w - 40, 110))
        pref_box.setTitle_("运行偏好 (即时生效 · 无需重启)")
        pref_box.setTitleFont_(ak.NSFont.systemFontOfSize_weight_(12.5, ak.NSFontWeightMedium))

        t_lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(16, 48, 120, 20))
        t_lbl.setBezeled_(False)
        t_lbl.setDrawsBackground_(False)
        t_lbl.setEditable_(False)
        t_lbl.setFont_(ak.NSFont.systemFontOfSize_(13))
        t_lbl.setStringValue_("按键交互方式：")
        pref_box.contentView().addSubview_(t_lbl)

        self.trigger_pop = ak.NSPopUpButton.alloc().initWithFrame_pullsDown_(ak.NSMakeRect(140, 44, 220, 26), False)
        self.trigger_pop.menu().setAutoenablesItems_(False)
        self.trigger_pop.addItemWithTitle_("长按说话 (按住说，松开打字)")
        self.trigger_pop.addItemWithTitle_("单击切换 (点按开始，再按打字)")
        self.trigger_pop.setTarget_(self.app)
        self.trigger_pop.setAction_("onTriggerPopupChanged:")
        pref_box.contentView().addSubview_(self.trigger_pop)

        l_lbl = ak.NSTextField.alloc().initWithFrame_(ak.NSMakeRect(16, 14, 120, 20))
        l_lbl.setBezeled_(False)
        l_lbl.setDrawsBackground_(False)
        l_lbl.setEditable_(False)
        l_lbl.setFont_(ak.NSFont.systemFontOfSize_(13))
        l_lbl.setStringValue_("文本输出格式：")
        pref_box.contentView().addSubview_(l_lbl)

        self.lang_pop = ak.NSPopUpButton.alloc().initWithFrame_pullsDown_(ak.NSMakeRect(140, 10, 220, 26), False)
        self.lang_pop.menu().setAutoenablesItems_(False)
        self.lang_pop.addItemWithTitle_("简体中文")
        self.lang_pop.addItemWithTitle_("繁体中文")
        self.lang_pop.setTarget_(self.app)
        self.lang_pop.setAction_("onLangPopupChanged:")
        pref_box.contentView().addSubview_(self.lang_pop)

        cv.addSubview_(pref_box)

        # 6. 底部工具栏 (简洁纯粹)
        close_btn = ak.NSButton.alloc().initWithFrame_(ak.NSMakeRect(w - 120, 15, 100, 32))
        close_btn.setTitle_("隐藏面板")
        close_btn.setBezelStyle_(ak.NSBezelStyleRounded)
        close_btn.setTarget_(self)
        close_btn.setAction_("closePanel:")
        cv.addSubview_(close_btn)

        # 启动定时器更新权限视图 (每秒刷新一次)
        threading.Thread(target=self._perm_refresh_loop, daemon=True).start()

    def show(self):
        def _do():
            ak.NSApp.activateIgnoringOtherApps_(True)
            self.win.makeKeyAndOrderFront_(None)
        NSOperationQueue.mainQueue().addOperationWithBlock_(_do)

    def closePanel_(self, sender):
        self.win.orderOut_(None)

    def _perm_refresh_loop(self):
        while True:
            time.sleep(1.0)

            # 检测与体积计算全部在后台线程完成：遍历数 GB 文件若放在主线程会卡住界面。
            acc_ok = check_accessibility()
            input_ok = check_input_monitoring()
            mic_ok = check_microphone()
            deps_ok = check_deps_ready()
            sv_ok = check_sensevoice_ready()

            sizes = {}
            for key, ok, path in (
                ("deps", deps_ok, PYLIBS_DIR),
                ("sensevoice", sv_ok, SENSEVOICE_DIR),
            ):
                prev = self.engine_state.get(key)
                if prev is not None and prev[0] == ok:
                    size = prev[1]
                else:
                    size = _dir_size_mb(path) if ok else 0.0
                    self.engine_state[key] = (ok, size)
                sizes[key] = size

            total = sum(sizes.values())

            def _update():
                self._update_badge("accessibility", acc_ok)
                self._update_badge("input_mon", input_ok)
                self._update_badge("microphone", mic_ok)

                self._update_engine_badge("deps", deps_ok, sizes["deps"])
                self._update_engine_badge("sensevoice", sv_ok, sizes["sensevoice"])
                self._update_usage(total)

                if acc_ok and input_ok and mic_ok:
                    if deps_ok and sv_ok:
                        self.tip_lbl.setStringValue_("核心权限与本地引擎均已就绪：长按右侧 Option (⌥) 即可开始语音输入。")
                        self.tip_lbl.setTextColor_(ak.NSColor.systemGreenColor())
                    else:
                        self.tip_lbl.setStringValue_("提示：本地引擎组件未就绪，请点击下方「重新下载 / 修复组件」。")
                        self.tip_lbl.setTextColor_(ak.NSColor.systemYellowColor())
                else:
                    self.tip_lbl.setStringValue_("提示：在系统设置中完成授权后，必须点击上方「重启生效」以立即载入新权限。")
                    self.tip_lbl.setTextColor_(ak.NSColor.secondaryLabelColor())

            NSOperationQueue.mainQueue().addOperationWithBlock_(_update)

    def _update_engine_badge(self, key, is_ok, size):
        if key not in self.engine_items:
            return
        iv, stat = self.engine_items[key]

        if is_ok:
            img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("checkmark.circle.fill", None)
            iv.setImage_(img)
            iv.setContentTintColor_(ak.NSColor.systemGreenColor())
            stat.setStringValue_("已就绪 · " + _format_size(size))
            stat.setTextColor_(ak.NSColor.systemGreenColor())
        else:
            img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("exclamationmark.triangle.fill", None)
            iv.setImage_(img)
            iv.setContentTintColor_(ak.NSColor.systemYellowColor())
            stat.setStringValue_("未就绪")
            stat.setTextColor_(ak.NSColor.systemYellowColor())

    def _update_usage(self, total_mb):
        if total_mb <= 0:
            self.usage_lbl.setStringValue_("")
        else:
            self.usage_lbl.setStringValue_(
                "本地磁盘占用合计 %s（存放于 ~/.voicetype）" % _format_size(total_mb))

    def _update_badge(self, key, is_ok):
        if key not in self.perm_items:
            return
        iv, stat, btn = self.perm_items[key]
        if is_ok:
            img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("checkmark.circle.fill", None)
            iv.setImage_(img)
            iv.setContentTintColor_(ak.NSColor.systemGreenColor())
            stat.setStringValue_("已授权")
            stat.setTextColor_(ak.NSColor.systemGreenColor())
            btn.setEnabled_(False)
            btn.setTitle_("已授权")
        else:
            img = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("exclamationmark.triangle.fill", None)
            iv.setImage_(img)
            iv.setContentTintColor_(ak.NSColor.systemYellowColor())
            stat.setStringValue_("未授权")
            stat.setTextColor_(ak.NSColor.systemYellowColor())
            btn.setEnabled_(True)
            if key == "microphone":
                mic_stat = av_foundation.AVCaptureDevice.authorizationStatusForMediaType_(av_foundation.AVMediaTypeAudio)
                btn.setTitle_("授权" if mic_stat == 0 else "去设置")
            else:
                btn.setTitle_("去设置")


# ==================== 5. 菜单栏应用主体 ====================
class VoiceTypeApp(NSObject):
    def init(self):
        self = objc.super(VoiceTypeApp, self).init()
        if self is None:
            return None
        self.recording = False
        self.listening = True
        self.simplified = True
        self.trigger_mode = TRIGGER_HOLD  # 默认长按说话
        self.alt_down = False
        self.tap_ready = False
        self.recorder = Recorder()
        self.hud = AppleStyleHUD()
        self.volume = SystemVolume()
        # 兵底保护：任何情况下退出都必须把音量恢复回去
        atexit.register(self.volume.restore)

        # 状态栏图标：原生 SF Symbol 模板图标
        self.status_item = ak.NSStatusBar.systemStatusBar().statusItemWithLength_(ak.NSVariableStatusItemLength)
        btn = self.status_item.button()

        sys_icon = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic.fill", None)
        if sys_icon:
            sys_icon.setTemplate_(True)
            btn.setImage_(sys_icon)
        else:
            bundle_icon = ak.NSBundle.mainBundle().pathForImageResource_("menu_icon@2x.png")
            if bundle_icon and os.path.exists(bundle_icon):
                img = ak.NSImage.alloc().initWithContentsOfFile_(bundle_icon)
                img.setSize_(ak.NSMakeSize(18, 18))
                img.setTemplate_(True)
                btn.setImage_(img)

        # 下拉菜单 (纯文字，零 Emoji)
        self.menu = ak.NSMenu.alloc().init()
        self.status_item.setMenu_(self.menu)

        self._add("打开设置与权限中心...", "openSettingsWindow:")
        self._add(None, None, separator=True)

        self.mi_trigger = self._add("交互：长按说话 (点击切换单击)", "switchTriggerMode:")
        self.mi_lang = self._add("文本：简体 (点击切换繁体)", "switchLang:")
        self._add(None, None, separator=True)
        self._add("清理权限残留记录", "resetPermissionsCache:")
        self._add("重置运行依赖 (重启后重装)", "resetDeps:")
        self._add("重启应用生效", "restartApp:")
        self.mi_quit = self._add("退出 VoiceType", "quit:")

        # 初始化控制中心面板
        self.settings_win = SettingsWindow(self)

        # 依赖与模型在进入主程序前已由 bootstrap.py 配置完毕，这里仅做本地静默预热
        threading.Thread(target=self._preload_models, daemon=True).start()

        # 权限检测与引导
        self._check_and_request_permissions()

        return self

    @objc.python_method
    def _run_bootstrap(self, repair=True):
        """调用 App 内置的 bootstrap.py 完成依赖与模型的安装/修复。

        以独立进程运行，避免在常驻主进程里加载安装逻辑；bootstrap 会自行
        弹出进度窗口并展示下载进度。
        """
        python_bin = os.environ.get("VOICETYPE_PYTHON")
        app_dir = os.environ.get("VOICETYPE_APP_DIR")
        if not python_bin or not app_dir:
            self.hud.show("alert", "缺少内置运行环境信息", auto_hide=2.0)
            return

        script = os.path.join(app_dir, "bootstrap.py")
        if not os.path.exists(script):
            self.hud.show("alert", "未找到引导脚本", auto_hide=2.0)
            return

        cmd = [python_bin, script]
        if repair:
            cmd.append("--repair")
        try:
            subprocess.Popen(cmd, env=os.environ.copy())
            self.hud.show("done", "已开始修复本地引擎组件", auto_hide=1.8)
        except Exception:
            self.hud.show("alert", "无法启动修复流程", auto_hide=2.0)

    @objc.python_method
    def _preload_models(self):
        """后台预热两个模型，并在所用的计算设备上跑一次（MPS 内核需提前编译）。"""
        device = warmup_engines()
        if device == "mps":
            self.hud.show("done", "本地引擎已就绪 · 已启用 GPU 加速", auto_hide=1.6)
        elif device is None:
            self.hud.show("alert", "本地引擎加载异常，请在控制中心修复组件", auto_hide=2.5)

    @objc.python_method
    def _check_and_request_permissions(self):
        acc_ok = check_accessibility()
        input_ok = check_input_monitoring()
        mic_ok = check_microphone()
        
        # 辅助功能与输入监控均就绪时才挂载监听，杜绝未授权时底层自发强弹
        if acc_ok or input_ok:
            self._start_event_tap()

        # 如果缺少核心权限，优雅展示控制面板；全部就绪则轻量常驻
        if not acc_ok or not input_ok or not mic_ok or not self.tap_ready:
            self.settings_win.show()
            self.hud.show("alert", "请在控制中心完成权限配置", auto_hide=3.0)
        else:
            self.hud.show("done", "VoiceType 已就绪 · 长按右侧 Option 说话", auto_hide=2.0)



    @objc.python_method
    def _add(self, title, action, separator=False):
        if separator:
            item = ak.NSMenuItem.separatorItem()
            self.menu.addItem_(item)
            return item
        item = ak.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action or "", "")
        item.setTarget_(self)
        self.menu.addItem_(item)
        return item

    # ---------- 菜单与面板动作 ----------
    def openSettingsWindow_(self, sender):
        self.settings_win.show()

    def onTriggerPopupChanged_(self, sender):
        idx = sender.indexOfSelectedItem()
        self.trigger_mode = TRIGGER_HOLD if idx == 0 else TRIGGER_TOGGLE
        txt = "交互：单击切换 (点击切换长按)" if self.trigger_mode == TRIGGER_TOGGLE else "交互：长按说话 (点击切换单击)"
        self.mi_trigger.setTitle_(txt)
        info = "已切换为单击切换模式" if self.trigger_mode == TRIGGER_TOGGLE else "已切换为长按说话模式"
        self.hud.show("done", info, auto_hide=1.2)

    def switchTriggerMode_(self, sender):
        self.trigger_mode = TRIGGER_TOGGLE if self.trigger_mode == TRIGGER_HOLD else TRIGGER_HOLD
        txt = "交互：单击切换 (点击切换长按)" if self.trigger_mode == TRIGGER_TOGGLE else "交互：长按说话 (点击切换单击)"
        self.mi_trigger.setTitle_(txt)
        if hasattr(self, "settings_win"):
            self.settings_win.trigger_pop.selectItemAtIndex_(0 if self.trigger_mode == TRIGGER_HOLD else 1)
        info = "已切换为单击切换模式" if self.trigger_mode == TRIGGER_TOGGLE else "已切换为长按说话模式"
        self.hud.show("done", info, auto_hide=1.2)

    def onLangPopupChanged_(self, sender):
        idx = sender.indexOfSelectedItem()
        self.simplified = (idx == 0)
        txt = "文本：简体 (点击切换繁体)" if self.simplified else "文本：繁体 (点击切换简体)"
        self.mi_lang.setTitle_(txt)
        info = "已切换为简体中文" if self.simplified else "已切换为繁体中文"
        self.hud.show("done", info, auto_hide=1.2)

    def openAccessibilitySettings_(self, sender):
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"], check=False)

    def openInputMonitoringSettings_(self, sender):
        request_input_monitoring()
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"], check=False)

    def openMicrophoneSettings_(self, sender):
        status = av_foundation.AVCaptureDevice.authorizationStatusForMediaType_(av_foundation.AVMediaTypeAudio)
        if status == 0:  # 未请求过：直接原地唤起系统权限弹窗
            def _handler(granted):
                pass
            av_foundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                av_foundation.AVMediaTypeAudio, _handler
            )
        else:  # 已被拒绝或已配置过：打开系统设置对应页面
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"], check=False)

    def resetPermissionsCache_(self, sender):
        bundle_id = "com.local.voicetype"
        for svc in ["Microphone", "Accessibility", "ListenEvent"]:
            subprocess.run(["tccutil", "reset", svc, bundle_id], check=False)
        self.hud.show("done", "已清空三大权限残留记录", auto_hide=2.0)

    def reinstallModels_(self, sender):
        self._run_bootstrap(repair=True)

    def resetDeps_(self, sender):
        """依赖损坏时的恢复出口：删除 pylibs，下次启动由 bootstrap 重装。"""
        import shutil
        shutil.rmtree(PYLIBS_DIR, ignore_errors=True)
        self.hud.show("done", "已重置运行依赖，重启后自动重装", auto_hide=2.5)

    def cleanModelCache_(self, sender):
        import shutil
        if os.path.exists(MODELS_DIR):
            shutil.rmtree(MODELS_DIR, ignore_errors=True)
        self.hud.show("done", "已清空本地模型文件", auto_hide=1.5)

    def restartApp_(self, sender):
        # 0. 确保音量不会留在压低状态
        self.volume.restore()

        # 1. 释放所有本地文件锁
        try:
            if _py_lock_fd:
                fcntl.flock(_py_lock_fd, fcntl.LOCK_UN)
                _py_lock_fd.close()
        except Exception:
            pass
        for lf in ["/tmp/voicetype_app.lock", "/tmp/voicetype_single.lock"]:
            try:
                os.remove(lf)
            except Exception:
                pass

        # 2. 动态获取当前 App 的真实 Bundle 路径
        bundle_path = ak.NSBundle.mainBundle().bundlePath()
        if not bundle_path or not bundle_path.endswith(".app"):
            bundle_path = "/Applications/VoiceType.app"

        # 3. 核心：原子等待老进程彻底消亡，再启动全新独立进程 (彻底根除重启竞态失效 Bug)
        pid = os.getpid()
        script = f"""
while kill -0 {pid} 2>/dev/null; do
    sleep 0.05
done
rm -f /tmp/voicetype_app.lock /tmp/voicetype_single.lock
open -n '{bundle_path}'
"""
        subprocess.Popen(["/bin/bash", "-c", script])

        # 4. 老实例优雅退出
        ak.NSApplication.sharedApplication().terminate_(None)

    # ---------- 右侧 Option (⌥) 键精准监听 (会话级 EventTap) ----------
    @objc.python_method
    def _start_event_tap(self):
        if self.tap_ready:
            return
        from Quartz import (
            CGEventTapCreate, CGEventTapEnable, CGEventMaskBit,
            kCGSessionEventTap, kCGEventTapOptionListenOnly,
            kCGEventSourceStateHIDSystemState, CGEventGetFlags,
            CGEventGetIntegerValueField, kCGKeyboardEventKeycode,
            kCGEventFlagMaskAlternate, kCGEventFlagsChanged,
        )
        from CoreFoundation import (
            CFRunLoopAddSource, CFRunLoopGetCurrent, CFRunLoopRun,
            CFMachPortCreateRunLoopSource, kCFRunLoopDefaultMode,
        )
        app_ref = self
        KEY_RIGHT_OPTION = 61  # macOS 右侧 Option 物理键码

        def callback(proxy, type_, event, refcon):
            try:
                # 仅响应右侧 Option 键 (keycode: 61)
                kc = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                if kc == KEY_RIGHT_OPTION:
                    flags = CGEventGetFlags(event)
                    alt_down = bool(flags & kCGEventFlagMaskAlternate)
                    app_ref.right_option_changed(alt_down)
            except Exception:
                pass
            return event

        tap = CGEventTapCreate(kCGSessionEventTap, kCGEventTapOptionListenOnly,
                               kCGEventSourceStateHIDSystemState,
                               CGEventMaskBit(kCGEventFlagsChanged), callback, None)
        if not tap:
            return

        self.tap_ready = True

        def looper():
            src = CFMachPortCreateRunLoopSource(None, tap, 0)
            CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopDefaultMode)
            CGEventTapEnable(tap, True)
            CFRunLoopRun()

        threading.Thread(target=looper, daemon=True).start()

    @objc.python_method
    def right_option_changed(self, is_down):
        if not self.listening:
            return

        if self.trigger_mode == TRIGGER_HOLD:
            # 模式 1：长按说话 (按住录音，松开识别并打字)
            if is_down and not self.recording:
                self._start_recording()
            elif not is_down and self.recording:
                self._stop_recording()
        else:
            # 模式 2：单击切换 (按一下开始说话，再按一下结束并打字)
            if not is_down:  # 在松开瞬时触发切换
                if not self.recording:
                    self._start_recording()
                    self.hud.show("recording", "正在聆听 · 再按右⌥结束")
                else:
                    self._stop_recording()

    # ---------- 菜单动作 ----------
    def switchLang_(self, sender):
        self.simplified = not self.simplified
        txt = "文本：简体 (点击切换繁体)" if self.simplified else "文本：繁体 (点击切换简体)"
        self.mi_lang.setTitle_(txt)
        if hasattr(self, "settings_win"):
            self.settings_win.lang_pop.selectItemAtIndex_(0 if self.simplified else 1)
        info = "已切换为简体中文" if self.simplified else "已切换为繁体中文"
        self.hud.show("done", info, auto_hide=1.4)

    def quit_(self, sender):
        if self.recording:
            self.recording = False
        self.volume.restore()
        self.hud.hide()
        ak.NSApplication.sharedApplication().terminate_(None)

    # ---------- 录音与识别流程 ----------
    @objc.python_method
    def _start_recording(self):
        self.recording = True
        self.hud.show("recording", "正在聆听...")
        # 先压低系统音量再开始录音：外放的声音会从麦克风回来，被一起识别进去。
        # 顺序不能反，否则开头那一段仍会录到外放声。
        self.volume.duck()
        try:
            self.recorder.start()
        except Exception:
            self.hud.show("alert", "麦克风未就绪 · 检查权限", auto_hide=2.0)
            self.recording = False
            self.volume.restore()

    @objc.python_method
    def _stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        arr = self.recorder.stop()
        # 录音已结束，不再有回声风险，立即恢复音量
        self.volume.restore()
        self.hud.show("transcribing", "正在识别...")
        threading.Thread(target=self._transcribe, args=(arr,), daemon=True).start()

    @objc.python_method
    def _transcribe(self, arr):
        if arr is None or len(arr) < 1600:
            self.hud.hide()
            return
        try:
            text = self._recognize(arr)
            self._finish(text)
        except Exception:
            self.hud.show("alert", "未识别到内容", auto_hide=1.4)

    @objc.python_method
    def _finish(self, text):
        if not text:
            self.hud.hide()
            return

        # 1. 简繁转换
        if self.simplified:
            text = to_simplified(text)

        # 2. 键入目标输入框
        self._insert_text(text)

        # 3. 浮窗呈现原生对勾徽章与严密垂直居中对齐文本
        self.hud.show("done", text, auto_hide=1.5)
        gc.collect()

    @objc.python_method
    def _recognize(self, arr):
        sr = self.recorder.sr or 44100
        if sr != 16000:
            num_samples = int(len(arr) * 16000 / sr)
            arr16 = scipy.signal.resample(arr, num_samples).astype(np.float32)
        else:
            arr16 = arr

        tmp16 = tempfile.mktemp(suffix=".wav")
        try:
            sf.write(tmp16, arr16, 16000)
            return SenseEngine.transcribe(tmp16)
        finally:
            try:
                os.remove(tmp16)
            except Exception:
                pass

    @objc.python_method
    def _insert_text(self, text):
        pb = ak.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, ak.NSPasteboardTypeString)
        time.sleep(0.04)

        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags,
            kCGSessionEventTap, kCGEventFlagMaskCommand,
        )
        V, CMD = 9, 55

        # 关键：V 键事件必须自带 Command 修饰符。
        # 仅靠前面单独发一个 Cmd 按下事件是不够的：输入法（如中文拼音的 v 模式）
        # 是根据事件自身的 flags 判断修饰键的，读不到 Command 就会把它当成普通字母 v，
        # 于是弹出「v+数字 / v+日期」的输入法候选面板。
        cmd_down = CGEventCreateKeyboardEvent(None, CMD, True)
        CGEventSetFlags(cmd_down, kCGEventFlagMaskCommand)

        v_down = CGEventCreateKeyboardEvent(None, V, True)
        CGEventSetFlags(v_down, kCGEventFlagMaskCommand)

        v_up = CGEventCreateKeyboardEvent(None, V, False)
        CGEventSetFlags(v_up, kCGEventFlagMaskCommand)

        cmd_up = CGEventCreateKeyboardEvent(None, CMD, False)

        CGEventPost(kCGSessionEventTap, cmd_down)
        time.sleep(0.03)
        CGEventPost(kCGSessionEventTap, v_down)
        time.sleep(0.02)
        CGEventPost(kCGSessionEventTap, v_up)
        time.sleep(0.02)
        CGEventPost(kCGSessionEventTap, cmd_up)

    # ---------- Dock 图标点击响应 ----------
    def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
        self.settings_win.show()
        return True

    # ---------- 进程退出保护 ----------
    def applicationWillTerminate_(self, sender):
        # 任何退出路径（含 Cmd+Q）都必须把音量恢复回去，
        # 否则用户系统音量会永久卡在压低后的低位。
        try:
            self.volume.restore()
        except Exception:
            pass


def main():
    try:
        app = ak.NSApplication.sharedApplication()
        # 核心：设置为常规桌面 App (Dock 栏常驻图标，解决菜单栏被刘海挤掉失联的问题)
        app.setActivationPolicy_(ak.NSApplicationActivationPolicyRegular)
        delegate = VoiceTypeApp.alloc().init()
        app.setDelegate_(delegate)
        app.run()
    except Exception:
        import traceback
        err = traceback.format_exc()
        log_dir = os.path.expanduser("~/.voicetype")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "crash.log"), "a") as f:
            f.write(f"\n--- {time.ctime()} ---\n{err}\n")
        print(err, file=sys.stderr)

if __name__ == "__main__":
    main()
