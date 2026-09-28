"""
ok-kes 加速补丁安装/卸载脚本。

用法（用 ok-kes 自带的 Python 运行，先关闭 ok-kes）：
    "D:\\Program Files\\ok-kes\\data\\apps\\ok-kes\\python\\python.exe" speedup_patch\install_speedup.py
    "D:\\Program Files\\ok-kes\\data\\apps\\ok-kes\\python\\python.exe" speedup_patch\install_speedup.py --uninstall
ok-kes 自动更新到新版本后会覆盖改动，重新运行一次安装即可。
"""
import argparse
import ast
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # 仓库根目录，speedup.py 在 ROOT/ok_tasks 下
DEFAULT_WORKING = r"D:\Program Files\ok-kes\data\apps\ok-kes\working"
MARK_BEGIN = "# --- ok-kes speedup patch begin ---"
MARK_END = "# --- ok-kes speedup patch end ---"
MODES = (("ok_tasks/ChaosMode.py", "ChaosMode"), ("ok_tasks/SortieMode.py", "SortieMode"))
TARGETS = tuple(rel for rel, _ in MODES) + ("src/config.py", "configs/OCR设置.json")

MODE_SNIPPET = f"""        {MARK_BEGIN}
        import speedup
        speedup.install(self)
        {MARK_END}

"""

CONFIG_SNIPPET = f'''

{MARK_BEGIN}
def _force_openvino_f32():
    """OpenVINO 在支持 BF16 的 CPU（如 Zen 4）上默认用 bf16 推理，识别结果与 ONNX Runtime 有差异；强制 f32。"""
    try:
        import openvino
    except (ImportError, OSError):
        return
    if getattr(openvino.Core.compile_model, "_speedup_f32", False):
        return
    original = openvino.Core.compile_model

    def compile_model(self, model, device_name=None, config=None, *args, **kwargs):
        if device_name in (None, "CPU"):
            config = dict(config or {{}})
            config.setdefault("INFERENCE_PRECISION_HINT", "f32")
        return original(self, model, device_name, config, *args, **kwargs)

    compile_model._speedup_f32 = True
    openvino.Core.compile_model = compile_model


if config["ocr"]["params"].get("use_openvino"):
    _force_openvino_f32()
{MARK_END}
'''


def ok_kes_running(working):
    main_py = os.path.normcase(os.path.join(working, "main.py"))
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if any(os.path.normcase(part) == main_py for part in proc.info["cmdline"] or []):
                return True
        except (psutil.Error, TypeError):
            continue
    return False


def backup(working):
    folder = os.path.join(working, "speedup_backup")
    os.makedirs(folder, exist_ok=True)
    for rel in TARGETS:
        src = os.path.join(working, rel)
        dst = os.path.join(folder, rel.replace("/", "__"))
        if not os.path.exists(src):
            continue
        if rel.endswith(".json"):
            if os.path.exists(dst):
                continue  # 用户配置不随版本更新，只保留第一次的原值
        else:
            with open(src, encoding="utf-8") as f:
                if MARK_BEGIN in f.read():
                    continue  # 已打过补丁，保留原始备份；版本更新覆盖后会重新备份新原版
        shutil.copy2(src, dst)


def patch_mode(path, class_name):
    with open(path, encoding="utf-8") as f:
        source = f.read()
    if MARK_BEGIN in source:
        return "已存在"
    anchor = "    def load_config(self):"
    if source.count(anchor) != 1:
        raise RuntimeError(f"{class_name}.py 结构与预期不符（找不到唯一的 load_config），未修改")
    patched = source.replace(anchor, MODE_SNIPPET + anchor)
    tree = ast.parse(patched)
    init = next(
        node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == class_name
        for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    if "speedup.install(self)" not in ast.get_source_segment(patched, init):
        raise RuntimeError(f"插入位置不在 {class_name}.__init__ 内，未修改")
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    return "已修改"


def patch_config(path):
    with open(path, encoding="utf-8") as f:
        source = f.read()
    if MARK_BEGIN in source:
        return "已存在"
    if "config = {" not in source or "'use_openvino': resolve_use_openvino()" not in source:
        raise RuntimeError("src/config.py 结构与预期不符，未修改")
    with open(path, "w", encoding="utf-8") as f:
        f.write(source.rstrip("\n") + "\n" + CONFIG_SNIPPET)
    return "已修改"


def set_ocr_backend(working, value):
    path = os.path.join(working, "configs", "OCR设置.json")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data["OCR后端"] = value
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def install(working):
    backup(working)
    shutil.copy2(os.path.join(ROOT, "ok_tasks", "speedup.py"), os.path.join(working, "ok_tasks", "speedup.py"))
    results = {rel: patch_mode(os.path.join(working, *rel.split("/")), class_name) for rel, class_name in MODES}
    results["src/config.py"] = patch_config(os.path.join(working, "src", "config.py"))
    for rel in ("ok_tasks/speedup.py",) + tuple(results):
        path = os.path.join(working, rel)
        with open(path, encoding="utf-8") as f:
            compile(f.read(), path, "exec")
    set_ocr_backend(working, "OpenVINO")
    for rel, result in results.items():
        print(f"{rel}: {result}")
    print("ok_tasks/speedup.py: 已复制")
    print("configs/OCR设置.json: OCR后端 = OpenVINO（f32）")
    print(f"原始文件备份在 {os.path.join(working, 'speedup_backup')}")


def uninstall(working):
    folder = os.path.join(working, "speedup_backup")
    for rel in TARGETS:
        saved = os.path.join(folder, rel.replace("/", "__"))
        if os.path.exists(saved):
            shutil.copy2(saved, os.path.join(working, rel))
            print(f"{rel}: 已还原")
    speedup_py = os.path.join(working, "ok_tasks", "speedup.py")
    if os.path.exists(speedup_py):
        os.remove(speedup_py)
        print("ok_tasks/speedup.py: 已删除")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--working", default=DEFAULT_WORKING)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    working = os.path.abspath(args.working)
    if not os.path.exists(os.path.join(working, "ok_tasks", "ChaosMode.py")):
        sys.exit(f"找不到 {working}\\ok_tasks\\ChaosMode.py，请用 --working 指定 ok-kes 的 working 目录")
    if ok_kes_running(working):
        sys.exit("ok-kes 正在运行：运行中修改任务文件会触发热重载打断当前任务，请先关闭 ok-kes 再执行")
    if args.uninstall:
        uninstall(working)
    else:
        install(working)
    print("完成，重新打开 ok-kes 生效")


if __name__ == "__main__":
    main()
