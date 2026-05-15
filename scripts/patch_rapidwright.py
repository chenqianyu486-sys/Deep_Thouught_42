#!/usr/bin/env python3
"""修补已安装的 rapidwright 包, 修复 JPype classpath 传递问题。

问题: rapidwright 2025.2.1 在同时设置 RAPIDWRIGHT_PATH 和 CLASSPATH 时,
      不会将 classpath 传递给 JPype 的 startJVM(), 且 JPype 不展开 jars/* 通配符。

用法: make setup 会自动调用; 也可手动运行:
      .venv/bin/python3 scripts/patch_rapidwright.py
"""
import os
import sys
import shutil
import site


def find_rapidwright_py():
    """在 venv 的 site-packages 中定位 rapidwright/rapidwright.py"""
    for sp in site.getsitepackages():
        rw_file = os.path.join(sp, "rapidwright", "rapidwright.py")
        if os.path.isfile(rw_file):
            return rw_file

    # pip install 到用户目录的情况
    for sp in site.getusersitepackages():
        rw_file = os.path.join(sp, "rapidwright", "rapidwright.py")
        if os.path.isfile(rw_file):
            return rw_file

    return None


def patch_file(filepath):
    """应用修补: 读取文件, 替换 start_jvm 中 classpath 逻辑"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 检查是否已修补
    if "glob.glob" in content and "基于 RAPIDWRIGHT_PATH 重新构造" in content:
        print(f"[patch] Already patched: {filepath}")
        return True

    # 检查是否包含原始 bug 代码
    old_logic = (
        '    if not os.environ.get(\'RAPIDWRIGHT_PATH\'):\n'
        '        dir_path = os.path.dirname(os.path.realpath(__file__))\n'
    )
    if old_logic not in content:
        print(f"[patch] WARNING: rapidwright.py has unexpected content, skipping")
        return False

    new_logic = (
        '    if not os.environ.get(\'RAPIDWRIGHT_PATH\'):\n'
        '        # 无 RAPIDWRIGHT_PATH: 使用 standalone JAR\n'
        '        dir_path = os.path.dirname(os.path.realpath(__file__))\n'
        '        file_name = "rapidwright-"+version+"-standalone-"+os_str+".jar"\n'
        '        classpath = os.path.join(dir_path,file_name)\n'
        '        if not os.path.isfile(classpath):\n'
        '            url = "http://github.com/Xilinx/RapidWright/releases/download/v"+version+"-beta/" + file_name\n'
        '            urllib.request.urlretrieve(url,classpath)\n'
        '        kwargs[\'classpath\'] = classpath\n'
        '    else:\n'
        '        # 有 RAPIDWRIGHT_PATH: 使用本地编译版本\n'
        '        rwPath = os.environ.get(\'RAPIDWRIGHT_PATH\')\n'
        '        bin_dir = rwPath + "/bin"\n'
        '        jars_glob = rwPath + "/jars/*"\n'
        '        \n'
        '        # 优先使用 CLASSPATH 环境变量(若包含正确前缀)\n'
        '        classpath_str = os.environ.get(\'CLASSPATH\', \'\')\n'
        '        if rwPath in classpath_str:\n'
        '            paths = []\n'
        '            for p in classpath_str.split(\':\'):\n'
        '                if \'*\' in p:\n'
        '                    expanded = glob.glob(p)\n'
        '                    if expanded:\n'
        '                        paths.extend(expanded)\n'
        '                else:\n'
        '                    paths.append(p)\n'
        '            kwargs[\'classpath\'] = \':\'.join(paths)\n'
        '        else:\n'
        '            # CLASSPATH 缺失或路径不正确, 基于 RAPIDWRIGHT_PATH 重新构造\n'
        '            paths = [bin_dir]\n'
        '            expanded = glob.glob(jars_glob)\n'
        '            if expanded:\n'
        '                paths.extend(expanded)\n'
        '            kwargs[\'classpath\'] = \':\'.join(paths)\n'
    )

    content = content.replace(old_logic, new_logic)

    # 添加 glob import
    if "import os, urllib.request, platform, shutil" in content:
        content = content.replace(
            "import os, urllib.request, platform, shutil",
            "import os, urllib.request, platform, shutil, glob"
        )

    # 备份原文件
    backup = filepath + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(filepath, backup)
        print(f"[patch] Backup: {backup}")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    # 清除 .pyc 缓存
    pycache = os.path.join(os.path.dirname(filepath), "__pycache__")
    if os.path.isdir(pycache):
        shutil.rmtree(pycache)

    print(f"[patch] Patched: {filepath}")
    return True


def main():
    rw_file = find_rapidwright_py()
    if rw_file is None:
        print("[patch] ERROR: rapidwright package not found. Run 'pip install rapidwright' first.")
        sys.exit(1)

    success = patch_file(rw_file)
    if success:
        print("[patch] rapidwright.py 修补完成 - JPype classpath 传递已修复")
    else:
        print("[patch] WARNING: 修补未完全应用, 请手动检查")


if __name__ == "__main__":
    main()
