#!/usr/bin/env python3
"""修补技能文件中 DesignTools 的错误导入路径。

问题: DesignTools 位于 com.xilinx.rapidwright.design 包,
      但部分技能文件错误地从 com.xilinx.rapidwright.design.tools 导入。
      design.tools 子包存在但不包含 DesignTools 类(只含 LUTTools 等)。

用法: make setup 会自动调用; 也可手动运行:
      python3 scripts/patch_skill_imports.py
"""
import os
import sys

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 需要修补的文件及其行号
PATCH_TARGETS = [
    ("skills/congestion_spreading_strategy.py", 244),
    ("skills/net_detour_optimization.py", 449),
]

WRONG_IMPORT = "from com.xilinx.rapidwright.design.tools import DesignTools"
CORRECT_IMPORT = "from com.xilinx.rapidwright.design import DesignTools"


def patch_file(rel_path, line_num):
    filepath = os.path.join(PROJECT_ROOT, rel_path)
    if not os.path.isfile(filepath):
        print(f"[patch] SKIP: {rel_path} not found")
        return False

    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if line_num < 1 or line_num > len(lines):
        print(f"[patch] SKIP: {rel_path} line {line_num} out of range")
        return False

    idx = line_num - 1
    current = lines[idx]

    if CORRECT_IMPORT in current:
        print(f"[patch] Already patched: {rel_path}:{line_num}")
        return True

    if WRONG_IMPORT not in current:
        print(f"[patch] WARNING: {rel_path}:{line_num} has unexpected content, skipping")
        return False

    lines[idx] = current.replace(WRONG_IMPORT, CORRECT_IMPORT)

    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"[patch] Patched: {rel_path}:{line_num}")
    return True


def main():
    print("[patch] Fixing DesignTools import paths in skill files...")
    success = True
    for rel_path, line_num in PATCH_TARGETS:
        if not patch_file(rel_path, line_num):
            success = False

    if success:
        print("[patch] All skill import patches applied successfully")
    else:
        print("[patch] WARNING: Some patches could not be applied")
        sys.exit(1)


if __name__ == "__main__":
    main()
