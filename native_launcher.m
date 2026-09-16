#define PY_SSIZE_T_CLEAN
#import <Python.h>
#import <Foundation/Foundation.h>
#include <mach-o/dyld.h>
#include <libgen.h>
#include <limits.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <fcntl.h>
#include <sys/file.h>
#include <pwd.h>

/*
 * VoiceType 原生启动器
 *
 * 职责：
 *   1. 进程级互斥，防止重复启动；
 *   2. 注入 App 内置 Python 运行时（不依赖 Homebrew / 系统 Python）；
 *   3. 驱动 bootstrap.py 完成首次依赖与模型配置，配置完成后自动重启本进程并进入主程序。
 *
 * 退出码：
 *   bootstrap.py 返回 42 表示"配置已完成，需要重启进程"，此时重新执行一次引导脚本，
 *   第二次进入即为正常启动主程序。这样可避免在同一进程内二次调用 NSApp.run()。
 */

static int run_script(const char *script_path, const char *marker_path) {
    char wrapper[8192];
    snprintf(wrapper, sizeof(wrapper),
        "import runpy\n"
        "code = 0\n"
        "try:\n"
        "    runpy.run_path(r'%s', run_name='__main__')\n"
        "except SystemExit as e:\n"
        "    code = e.code if isinstance(e.code, int) else 0\n"
        "except BaseException:\n"
        "    import traceback\n"
        "    traceback.print_exc()\n"
        "    code = 1\n"
        "try:\n"
        "    open(r'%s', 'w').write(str(code))\n"
        "except Exception:\n"
        "    pass\n",
        script_path, marker_path);

    if (PyRun_SimpleString(wrapper) != 0) {
        return 1;
    }

    int code = 0;
    FILE *mf = fopen(marker_path, "r");
    if (mf) {
        if (fscanf(mf, "%d", &code) != 1) {
            code = 1;
        }
        fclose(mf);
        unlink(marker_path);
    }
    return code;
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        // ---------- 1. 进程级互斥 ----------
        int lock_fd = open("/tmp/voicetype_app.lock", O_CREAT | O_RDWR, 0666);
        if (lock_fd < 0 || flock(lock_fd, LOCK_EX | LOCK_NB) != 0) {
            return 0;
        }

        // ---------- 2. 定位 App 内部路径 ----------
        char exec_path[PATH_MAX];
        uint32_t size = sizeof(exec_path);
        if (_NSGetExecutablePath(exec_path, &size) != 0) {
            return 1;
        }
        char resolved[PATH_MAX];
        if (realpath(exec_path, resolved) == NULL) {
            return 1;
        }

        char macos_dir[PATH_MAX];
        snprintf(macos_dir, sizeof(macos_dir), "%s", resolved);
        char *slash = strrchr(macos_dir, '/');
        if (slash) {
            *slash = '\0';  // .../Contents/MacOS
        }

        char contents[PATH_MAX];
        snprintf(contents, sizeof(contents), "%s/..", macos_dir);
        char contents_real[PATH_MAX];
        if (realpath(contents, contents_real) == NULL) {
            snprintf(contents_real, sizeof(contents_real), "%s/..", macos_dir);
        }

        char resources_dir[PATH_MAX];
        snprintf(resources_dir, sizeof(resources_dir), "%s/Resources", contents_real);

        char pyroot[PATH_MAX];
        snprintf(pyroot, sizeof(pyroot), "%s/Resources/python", contents_real);

        char python_bin[PATH_MAX];
        snprintf(python_bin, sizeof(python_bin), "%s/bin/python3.12", pyroot);

        char bootstrap_path[PATH_MAX];
        snprintf(bootstrap_path, sizeof(bootstrap_path), "%s/Resources/bootstrap.py", contents_real);

        if (access(python_bin, R_OK) != 0) {
            fprintf(stderr, "VoiceType: 内置 Python 运行时缺失: %s\n", python_bin);
            return 1;
        }
        if (access(bootstrap_path, R_OK) != 0) {
            fprintf(stderr, "VoiceType: 引导脚本缺失: %s\n", bootstrap_path);
            return 1;
        }

        // ---------- 3. 注入环境变量 ----------
        const char *home = getenv("HOME");
        if (!home) {
            struct passwd *pw = getpwuid(getuid());
            home = pw ? pw->pw_dir : "/tmp";
        }

        setenv("PYTHONHOME", pyroot, 1);
        setenv("PYTHONNOUSERSITE", "1", 1);
        setenv("VOICETYPE_PYTHON", python_bin, 1);
        setenv("VOICETYPE_APP_DIR", resources_dir, 1);
        setenv("HF_ENDPOINT", "https://hf-mirror.com", 1);

        char model_cache[PATH_MAX];
        snprintf(model_cache, sizeof(model_cache), "%s/.cache/modelscope", home);
        setenv("MODELSCOPE_CACHE", model_cache, 1);

        char path_env[PATH_MAX];
        snprintf(path_env, sizeof(path_env), "%s/bin:/usr/bin:/bin:/usr/sbin:/sbin", pyroot);
        setenv("PATH", path_env, 1);

        char marker_path[PATH_MAX];
        snprintf(marker_path, sizeof(marker_path), "/tmp/voicetype_exit_%d", getpid());

        // ---------- 4. 启动 Python 运行时 ----------
        Py_Initialize();

        // ---------- 5. 运行引导脚本 ----------
        // 首次运行：执行安装并返回 42，随后重启一次进入主程序。
        int code = run_script(bootstrap_path, marker_path);
        if (code == 42) {
            code = run_script(bootstrap_path, marker_path);
        }

        // 嵌入式 PyObjC 环境中禁止调用 Py_Finalize()，否则退出阶段会访问已销毁的
        // Python 运行时并触发 SIGSEGV。交由系统回收内存。
        close(lock_fd);
        if (code == 42) {
            code = 0;
        }
        exit(code);
    }
    return 0;
}