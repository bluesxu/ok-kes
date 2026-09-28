"""
出击模式的详细战斗日志：结构化战斗记录 + 异常截图 + 自动清理。

- 战斗记录写到 battle_logs/战斗记录_YYYY-MM-DD.jsonl，每行一条 JSON（事件名、时间、所处节点、观测到的内容、做出的决策和理由）；
  主日志只写一行摘要，避免把大段观测数据刷进 ok-script.log。
- 异常截图存到 battle_logs/截图/，JPG 质量 80；同一种异常每场战斗最多截 1 张。
- 清理：超过保留天数的文件删除；总大小超过上限时从最旧的删起。任务启动时清一次，运行中每小时清一次。

注意：本文件不能定义顶层类，框架会把 ok_tasks 下含类的 .py 当作任务加载。
"""
import datetime
import json
import os
import time

import cv2

import config_io

LOG_KEY = "详细战斗日志"
SHOT_KEY = "异常截图"
KEEP_DAYS_KEY = "战斗日志保留天数"
MAX_MB_KEY = "战斗日志总大小上限(MB)"

LOG_DIR = "battle_logs"
SHOT_DIR = os.path.join(LOG_DIR, "截图")
_CLEANUP_EVERY = 3600
_JPG_QUALITY = 80


def install(task):
    """给任务加上日志相关配置项；这些配置只影响本机，不进配置码也不上传。"""
    task.default_config[LOG_KEY] = True
    task.default_config[SHOT_KEY] = True
    task.default_config[KEEP_DAYS_KEY] = 7
    task.default_config[MAX_MB_KEY] = 500
    task.config_description[LOG_KEY] = "把每次出牌看到了什么、为什么这样出写进 battle_logs 目录，便于统计和排查"
    task.config_description[SHOT_KEY] = "出现异常（AP不足、出不掉牌、识别失败、战斗失败等）时截图，同一种异常每场战斗最多 1 张"
    task.config_description[KEEP_DAYS_KEY] = "battle_logs 里的记录和截图保留多少天"
    task.config_description[MAX_MB_KEY] = "battle_logs 总大小超过这个值时从最旧的文件删起"
    config_io.UI_ONLY_CONFIG_KEYS.update({LOG_KEY, SHOT_KEY, KEEP_DAYS_KEY, MAX_MB_KEY})


def _config(task, key):
    config = getattr(task, "config", None)
    if config is not None and key in config:
        return config[key]
    return task.default_config.get(key)


def _state(task):
    state = getattr(task, "_battle_log", None)
    if state is None:
        state = {"battle_id": 0, "shots": set(), "last_cleanup": 0.0}
        task._battle_log = state
    return state


def enabled(task):
    return bool(_config(task, LOG_KEY))


def new_battle(task):
    """进入一场新战斗：战斗编号 +1，重新允许每种异常各截 1 张。"""
    state = _state(task)
    state["battle_id"] += 1
    state["shots"] = set()
    return state["battle_id"]


def _node(task):
    status = getattr(task, "node_status", None) or {}
    return {
        "node": status.get("node_count"),
        "node_type": status.get("node_type"),
        "boss_battle": bool(status.get("final_boss_battle")),
    }


def record(task, event, **fields):
    """写一条战斗记录。写失败只记主日志，不影响出牌。"""
    if not enabled(task):
        return
    state = _state(task)
    entry = {"time": datetime.datetime.now().isoformat(timespec="milliseconds"),
             "task": getattr(task, "name", ""), "battle": state["battle_id"], "event": event}
    entry.update(_node(task))
    entry.update(fields)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"战斗记录_{datetime.date.today().isoformat()}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        task.log_info(f"战斗记录写入失败：{e}")
    maybe_cleanup(task)


def anomaly(task, kind, detail, frame=None, **fields):
    """记录一次异常；开启截图时，同一种异常每场战斗最多截 1 张。"""
    task.log_info(f"战斗异常「{kind}」：{detail}")
    shot = None
    state = _state(task)
    if enabled(task) and _config(task, SHOT_KEY) and kind not in state["shots"]:
        frame = frame if frame is not None else getattr(task, "frame", None)
        if frame is not None and getattr(frame, "size", 0):
            state["shots"].add(kind)
            try:
                os.makedirs(SHOT_DIR, exist_ok=True)
                name = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_战斗{state['battle_id']}_{kind}.jpg"
                shot = os.path.join(SHOT_DIR, name)
                ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPG_QUALITY])
                if ok:
                    data.tofile(shot)  # 路径含中文时 cv2.imwrite 会失败，改用 tofile
                else:
                    shot = None
            except OSError as e:
                task.log_info(f"异常截图保存失败：{e}")
                shot = None
    record(task, "异常", kind=kind, detail=detail, screenshot=shot, **fields)


def maybe_cleanup(task, force=False):
    state = _state(task)
    now = time.time()
    if not force and now - state["last_cleanup"] < _CLEANUP_EVERY:
        return
    state["last_cleanup"] = now
    try:
        keep_days = float(_config(task, KEEP_DAYS_KEY) or 7)
        max_mb = float(_config(task, MAX_MB_KEY) or 500)
    except (TypeError, ValueError):
        keep_days, max_mb = 7, 500
    removed = cleanup(LOG_DIR, keep_days, max_mb, now)
    if removed:
        task.log_info(f"清理战斗日志：删除 {removed} 个旧文件")


def cleanup(folder, keep_days, max_mb, now=None):
    """删除 folder 下超过 keep_days 天的文件；总大小仍超过 max_mb 时从最旧的删起。返回删除的文件数。"""
    now = time.time() if now is None else now
    files = []
    for root, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
    files.sort()
    removed, total = 0, sum(size for _, size, _ in files)
    limit = max_mb * 1024 * 1024
    for mtime, size, path in files:
        if now - mtime <= keep_days * 86400 and total <= limit:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
        total -= size
    return removed
