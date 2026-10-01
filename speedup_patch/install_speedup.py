"""
ok-kes 加速补丁安装/卸载脚本（含出击模式新出牌策略与详细战斗日志）。

用法（用 ok-kes 自带的 Python 运行，先关闭 ok-kes）：
    "D:\\Program Files\\ok-kes\\data\\apps\\ok-kes\\python\\python.exe" speedup_patch\\install_speedup.py
    "D:\\Program Files\\ok-kes\\data\\apps\\ok-kes\\python\\python.exe" speedup_patch\\install_speedup.py --uninstall
ok-kes 自动更新到新版本后会覆盖改动，重新运行一次安装即可。

安装内容：
- 加速补丁：复制 ok_tasks/speedup.py，让卡厄思/出击模式调用它；src/config.py 追加 OpenVINO f32 补丁。
- 出牌策略与详细日志：用本仓库的 utils.py、utils_sortie.py、utils_chaos.py、ChaosMode.py、SortieMode.py 和翻译文件整份替换，
  并新增 utils_battle.py、battle_log.py。只有安装目录里这些文件是 v1.4.3 原版时才替换；
  官方更新到新版本后对不上，就只装加速补丁并列出对不上的文件，需要先把本仓库合并到新版本。
"""
import argparse
import ast
import hashlib
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

# 整份替换的文件与 v1.4.3 原版的哈希（文本文件先去掉回车符再算），只有对得上才替换
BASE_VERSION = "v1.4.3"
BASE_SHA = {
    "ok_tasks/utils.py": "b910c8090c923972",
    "ok_tasks/utils_sortie.py": "c3673261f61fddaf",
    "ok_tasks/utils_chaos.py": "9d43515da98a42f9",
    "ok_tasks/SortieMode.py": "c28d4e488b03f7a0",
    "ok_tasks/ChaosMode.py": "c963711be1635940",
    "i18n/en_US/LC_MESSAGES/ok.po": "37081adbb099b6a1",
    "i18n/en_US/LC_MESSAGES/ok.mo": "c40cac64f35c91de",
    "i18n/zh_CN/LC_MESSAGES/ok.po": "6d9fd7ed5814a023",
    "i18n/zh_CN/LC_MESSAGES/ok.mo": "c46330149aecc43c",
}
REPLACE = tuple(BASE_SHA)
ADD = ("ok_tasks/speedup.py", "ok_tasks/utils_battle.py", "ok_tasks/battle_log.py")
ADD_ALWAYS = ("ok_tasks/speedup.py", "ok_tasks/battle_log.py")  # 官方版对不上、只装加速补丁时也复制
BACKUP_TARGETS = TARGETS + tuple(rel for rel in REPLACE if rel not in TARGETS)

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


def file_hash(path):
    with open(path, "rb") as f:
        data = f.read()
    if not path.endswith(".mo"):
        data = data.replace(b"\r", b"")
    return hashlib.sha256(data).hexdigest()[:16]


def backup_path(working, rel):
    return os.path.join(working, "speedup_backup", rel.replace("/", "__"))


def has_mark(working, rel):
    path = os.path.join(working, rel)
    if rel.endswith(".mo") or not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        return MARK_BEGIN in f.read()


def _manifest_path(working):
    return os.path.join(working, "speedup_backup", "installed.json")


def load_manifest(working):
    """历次安装装进去的文件哈希 {rel: [hash, ...]}。"""
    path = _manifest_path(working)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(working, rels):
    manifest = load_manifest(working)
    for rel in rels:
        path = os.path.join(working, rel)
        if os.path.exists(path):
            hashes = manifest.setdefault(rel, [])
            if file_hash(path) not in hashes:
                hashes.append(file_hash(path))
    with open(_manifest_path(working), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)


def is_ours(working, rel):
    """安装目录里的这个文件是不是我们装进去的：打过补丁、与本仓库的同名文件相同，或是以前某次安装装进去的。
    （只比对仓库当前版本时，仓库改过的文件会把上次装进去的旧版当成官方原版：覆盖原版备份，还误判成官方已更新。）"""
    path = os.path.join(working, rel)
    if not os.path.exists(path):
        return False
    if has_mark(working, rel):
        return True
    repo = os.path.join(ROOT, rel)
    if os.path.exists(repo) and file_hash(path) == file_hash(repo):
        return True
    return file_hash(path) in load_manifest(working).get(rel, []) and file_hash(path) != BASE_SHA.get(rel)


def backup(working):
    """备份原版文件。不是我们装进去的文件都当原版备份（覆盖旧备份）：
    官方更新后安装目录里是新版原文件，卸载时应还原成新版，而不是早先备份的旧版。"""
    os.makedirs(os.path.join(working, "speedup_backup"), exist_ok=True)
    for rel in BACKUP_TARGETS:
        src, dst = os.path.join(working, rel), backup_path(working, rel)
        if not os.path.exists(src):
            continue
        if rel.endswith(".json"):
            if os.path.exists(dst):
                continue  # 用户配置不随版本更新，只保留第一次的原值
        elif is_ours(working, rel):
            continue
        shutil.copy2(src, dst)


def battle_mismatches(working):
    """整份替换前核对：每个文件的原版都必须是 v1.4.3。安装目录里的文件本身是原版（本仓库没改的文件就是这样），
    或者已经换成我们的、而备份是原版，都算对得上。返回对不上的文件。"""
    mismatched = []
    for rel, sha in BASE_SHA.items():
        candidates = [os.path.join(working, rel)]
        if is_ours(working, rel):
            candidates.append(backup_path(working, rel))
        if not any(os.path.exists(path) and file_hash(path) == sha for path in candidates):
            mismatched.append(rel)
    return mismatched


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
    mismatched = battle_mismatches(working)
    results = {}
    if mismatched:
        # 官方版本已更新：只装加速补丁（往原版 ChaosMode/SortieMode 里插入调用），不替换其他文件
        print(f"以下文件不是 {BASE_VERSION} 原版，不安装出牌策略与战斗日志（需要先把本仓库合并到新版本）：")
        for rel in mismatched:
            print(f"  {rel}")
        added = ADD_ALWAYS  # speedup 会写详细日志，battle_log 也要带上
        for rel, class_name in MODES:
            if is_ours(working, rel) and not has_mark(working, rel):
                shutil.copy2(backup_path(working, rel), os.path.join(working, rel))  # 先还原成原版再插入
            results[rel] = patch_mode(os.path.join(working, *rel.split("/")), class_name)
    else:
        # 本仓库的 ChaosMode/SortieMode 本身就调用 speedup.install，整份替换即可
        added = ADD
        for rel in REPLACE:
            shutil.copy2(os.path.join(ROOT, rel), os.path.join(working, rel))
            results[rel] = "已替换"
    for rel in added:
        shutil.copy2(os.path.join(ROOT, rel), os.path.join(working, rel))
        results[rel] = "已复制"
    results["src/config.py"] = patch_config(os.path.join(working, "src", "config.py"))
    for rel in results:
        if rel.endswith(".py"):
            path = os.path.join(working, rel)
            with open(path, encoding="utf-8") as f:
                compile(f.read(), path, "exec")
    set_ocr_backend(working, "OpenVINO")
    save_manifest(working, results)
    for rel, result in results.items():
        print(f"{rel}: {result}")
    print("configs/OCR设置.json: OCR后端 = OpenVINO（f32）")
    print(f"原始文件备份在 {os.path.join(working, 'speedup_backup')}")


def uninstall(working):
    for rel in BACKUP_TARGETS:
        saved = backup_path(working, rel)
        if os.path.exists(saved) and (rel.endswith(".json") or is_ours(working, rel)):
            shutil.copy2(saved, os.path.join(working, rel))
            print(f"{rel}: 已还原")
    for rel in ADD:
        path = os.path.join(working, rel)
        if os.path.exists(path):
            os.remove(path)
            print(f"{rel}: 已删除")


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
