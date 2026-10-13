# -*- coding: utf-8 -*-
"""把已打包好的 .app 里的 Python 代码/资源升级到当前源码（不用重跑 py2app）。

为什么需要这个脚本：
  py2app 每次构建都会先批量删除 build/ 下的临时文件，在某些受限环境里
  会被「批量删除保护」直接拦掉，导致打包中断（exit=1，却看不到 Traceback）。
  但 .app 里 99% 的体积是 Python 运行时 + PyObjC，每次都重打很浪费。
  这个脚本只替换「我们自己写的那几个模块 + 资源文件」，秒级完成。

用法：
    python tools/upgrade_bundle.py dist/液态岛.app
"""
import os
import py_compile
import shutil
import sys
import tempfile
import zipfile

# 我们自己写的模块（py2app 会把它们编成 .pyc 平铺进 python313.zip 根目录）
OUR_MODULES = [
    "config", "core", "hotkey", "island", "switcher", "prefs", "autostart",
    "onboarding", "panels", "resources", "version", "main",
]
# 需要原样覆盖进 Contents/Resources 的资源
RESOURCES = ["prefs.html"]
RESOURCE_DIRS = ["assets"]


def compile_all(src_dir, tmp_dir):
    """把源码编译成 .pyc，返回 {模块名: pyc字节}。"""
    out = {}
    for mod in OUR_MODULES:
        py = os.path.join(src_dir, mod + ".py")
        if not os.path.exists(py):
            print(f"  （跳过不存在的模块 {mod}.py）")
            continue
        cfile = os.path.join(tmp_dir, mod + ".pyc")
        py_compile.compile(py, cfile=cfile, doraise=True)
        with open(cfile, "rb") as f:
            out[mod] = f.read()
    return out


def is_ours(name):
    """判断 zip 内条目是不是我们自己的模块（只认根目录那层）。"""
    if "/" in name:
        return None
    base = name[:-4] if name.endswith(".pyc") else (name[:-3] if name.endswith(".py") else None)
    return base if base in OUR_MODULES else None


def rewrite_zip(zip_path, compiled):
    """重建 zip：替换我们的 .pyc，并丢掉我们的 .py（避免和 pyc 时间戳打架）。"""
    tmp_out = zip_path + ".new"
    replaced, dropped, kept = [], [], 0
    with zipfile.ZipFile(zip_path) as zin, \
            zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            name = item.filename
            mod = is_ours(name)
            if mod is None:
                zout.writestr(item, zin.read(name))
                kept += 1
                continue
            if name.endswith(".py"):
                dropped.append(name)
                continue
            if mod in compiled:
                new_item = zipfile.ZipInfo(name, date_time=item.date_time)
                new_item.compress_type = zipfile.ZIP_DEFLATED
                new_item.external_attr = item.external_attr
                zout.writestr(new_item, compiled[mod])
                replaced.append(name)
            else:
                zout.writestr(item, zin.read(name))
                kept += 1
    os.replace(tmp_out, zip_path)
    print(f"  zip：替换 {len(replaced)} 个 pyc，丢弃 {len(dropped)} 个旧 py，保留 {kept} 个")
    for n in replaced:
        print(f"    ✓ {n}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    bundle = sys.argv[1]
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    res_dir = os.path.join(bundle, "Contents", "Resources")
    if not os.path.isdir(res_dir):
        print(f"❌ 不是合法的 .app：{bundle}")
        sys.exit(1)

    print(f"升级包：{bundle}")
    print(f"源码：{src_dir}")

    tmp_dir = tempfile.mkdtemp(prefix="li-upgrade-")
    try:
        print("── 1/4 编译源码 ──")
        compiled = compile_all(src_dir, tmp_dir)
        print(f"  编译出 {len(compiled)} 个模块")

        print("── 2/4 写回 python313.zip ──")
        zip_path = os.path.join(res_dir, "lib", "python313.zip")
        if not os.path.exists(zip_path):
            print("❌ 找不到 lib/python313.zip")
            sys.exit(1)
        rewrite_zip(zip_path, compiled)

        print("── 3/4 覆盖资源 ──")
        # main.py：py2app 的启动器直接跑 Resources/main.py，必须同步
        shutil.copy2(os.path.join(src_dir, "main.py"), os.path.join(res_dir, "main.py"))
        print("    ✓ main.py")
        for r in RESOURCES:
            src = os.path.join(src_dir, r)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(res_dir, r))
                print(f"    ✓ {r}")
        for d in RESOURCE_DIRS:
            src = os.path.join(src_dir, d)
            dst = os.path.join(res_dir, d)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                print(f"    ✓ {d}/")

        print("── 4/4 同步版本号 ──")
        sys.path.insert(0, src_dir)
        from version import APP_VERSION, APP_BUILD  # noqa: E402
        plist = os.path.join(bundle, "Contents", "Info.plist")
        for key, val in (("CFBundleShortVersionString", APP_VERSION),
                         ("CFBundleVersion", APP_BUILD)):
            os.system(f'/usr/libexec/PlistBuddy -c "Set :{key} {val}" "{plist}" 2>/dev/null')
        print(f"    版本 → {APP_VERSION} (build {APP_BUILD})")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("\n✅ 升级完成（记得重新签名：codesign --force --deep --sign - 包路径）")


if __name__ == "__main__":
    main()
