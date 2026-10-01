"""
详细日志（出击模式、卡厄思模式共用）：结构化记录 + 异常截图 + 自动清理。事件表见 CONTEXT.md「日志事件表」。

- 记录写到 battle_logs/战斗记录_YYYY-MM-DD.jsonl，每行一条 JSON（事件名、时间、第几轮、第几场战斗、所处节点、
  观测到的内容、做出的决策和理由）；主日志只写一行摘要，避免把大段观测数据刷进 ok-script.log。
- 只在真正点下去的那一刻记一条决定，没点的帧不记。
- 异常截图存到 battle_logs/截图/，JPG 质量 80；同一种异常每场战斗（卡厄思模式为每轮）最多截 1 张。
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
_BATTLE_GONE = 8        # 离开战斗画面这么多秒才算战斗结束（出击模式战斗中会弹出选择页面）
_UNHANDLED_AFTER = 10   # 连续这么多秒没有页面处理函数认领画面，记一次「未识别页面」
_UNHANDLED_GAP = 3      # 兜底函数两次调用间隔超过这么久，说明中间有帧被别的处理函数认领了，重新计时


def install(task):
    """给任务加上日志相关配置项；这些配置只影响本机，不进配置码也不上传。"""
    task.default_config[LOG_KEY] = True
    task.default_config[SHOT_KEY] = True
    task.default_config[KEEP_DAYS_KEY] = 7
    task.default_config[MAX_MB_KEY] = 500
    task.config_description[LOG_KEY] = "把每个决定（出牌、装备、选卡、路线等）看到了什么、为什么这样做写进 battle_logs 目录，便于统计和排查"
    task.config_description[SHOT_KEY] = "出现异常（识别失败、画面卡住、未识别页面等）时截图，同一种异常每场战斗最多 1 张"
    task.config_description[KEEP_DAYS_KEY] = "battle_logs 里的记录和截图保留多少天"
    task.config_description[MAX_MB_KEY] = "battle_logs 总大小超过这个值时从最旧的文件删起"
    config_io.UI_ONLY_CONFIG_KEYS.update({LOG_KEY, SHOT_KEY, KEEP_DAYS_KEY, MAX_MB_KEY})


def _config(task, key):
    config = getattr(task, "config", None)
    if config is not None and key in config:
        return config[key]
    return (getattr(task, "default_config", None) or {}).get(key)


def _state(task):
    state = getattr(task, "_battle_log", None)
    if state is None:
        # round_id：本次运行第几轮；in_battle/battle_start/battle_last：战斗画面起止；last_hp：上次进入节点时的生命值
        state = {"battle_id": 0, "round_id": 1, "round_start": time.time(), "shots": set(), "last_cleanup": 0.0,
                 "in_battle": False, "battle_start": 0.0, "battle_last": 0.0, "last_hp": None,
                 "rerolls": 0, "once": set(), "unhandled_since": None, "unhandled_last": 0.0, "unhandled_reported": False}
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
        "layer": (status.get("pass_final_boss_count") or 0) + 1,
        "boss_battle": bool(status.get("final_boss_battle")),
    }


def record(task, event, **fields):
    """写一条战斗记录。写失败只记主日志，不影响出牌。"""
    if not enabled(task):
        return
    state = _state(task)
    entry = {"time": datetime.datetime.now().isoformat(timespec="milliseconds"),
             "task": getattr(task, "name", ""), "round": state["round_id"], "battle": state["battle_id"],
             "event": event}
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
    if kind not in state["shots"]:
        shot = save_shot(task, kind, frame if frame is not None else getattr(task, "frame", None))
        if shot:
            state["shots"].add(kind)
    record(task, "异常", kind=kind, detail=detail, screenshot=shot, **fields)


def end_round(task, **fields):
    """一轮结束：记一条带用时的「一轮结束」，之后的记录算下一轮。"""
    state = _state(task)
    record(task, "一轮结束", seconds=round(time.time() - state["round_start"]), rerolls=state["rerolls"], **fields)
    state.update(round_id=state["round_id"] + 1, round_start=time.time(), shots=set(), last_hp=None, rerolls=0, once=set())


def once(task, key):
    """本轮第一次遇到 key 时返回 True。用于「看了没点」的决定（比如达标后交给「进入」按钮），
    页面会停好几帧，只记第一帧。"""
    seen = _state(task)["once"]
    if key in seen:
        return False
    seen.add(key)
    return True


def reroll(task, page, **fields):
    """刷存档类的重来（零式系统重新合成、获得法典卡厄思合成、赛季再次观测等）：记一条「重开」，计入本轮 rerolls。"""
    _state(task)["rerolls"] += 1
    record(task, "重开", page=page, **fields)


def node_entered(task, hp=None, **fields):
    """进入一个新节点时的状态；hp 为 (当前, 上限)，hp_change 是与上一个节点相比的变化（中间打过的战斗掉的血）。"""
    state = _state(task)
    change = hp[0] - state["last_hp"][0] if hp and state["last_hp"] else None
    if hp:
        state["last_hp"] = hp
    record(task, "进入节点", hp=hp and list(hp), hp_change=change, **fields)


def battle_frame(task, in_battle):
    """每帧告诉日志当前是不是战斗画面：第一次看到记「战斗开始」，离开超过 _BATTLE_GONE 秒记「战斗结束」。"""
    state = _state(task)
    now = time.time()
    if in_battle:
        if not state["in_battle"]:
            new_battle(task)
            state.update(in_battle=True, battle_start=now)
            record(task, "战斗开始")
        state["battle_last"] = now
    elif state["in_battle"] and now - state["battle_last"] >= _BATTLE_GONE:
        state["in_battle"] = False
        record(task, "战斗结束", seconds=round(state["battle_last"] - state["battle_start"]))


def unhandled_frame(task):
    """PAGE_HANDLERS 末尾的兜底：这一帧没有任何处理函数认领。连续 _UNHANDLED_AFTER 秒都这样，
    记一次「未识别页面」异常（截图 + 画面上的全部文字），同一个页面只记一次。"""
    state = _state(task)
    now = time.time()
    if state["in_battle"]:
        return False  # 卡厄思模式自动战斗时处理函数每帧都不认领，不算未识别页面
    if state["unhandled_since"] is None or now - state["unhandled_last"] > _UNHANDLED_GAP:
        state.update(unhandled_since=now, unhandled_reported=False)
    state["unhandled_last"] = now
    if not state["unhandled_reported"] and now - state["unhandled_since"] >= _UNHANDLED_AFTER:
        state["unhandled_reported"] = True
        anomaly(task, "未识别页面", f"已 {now - state['unhandled_since']:.0f} 秒没有页面处理函数认领画面",
                texts=[b.name for b in getattr(task, "all_texts", [])][:150])
    return False


def unhandled_seconds(task):
    """当前这段「没有处理函数认领画面」已持续多少秒；战斗中或刚被认领过返回 0。"""
    state = _state(task)
    if state["in_battle"] or state["unhandled_since"] is None or time.time() - state["unhandled_last"] > _UNHANDLED_GAP:
        return 0
    return state["unhandled_last"] - state["unhandled_since"]


def save_shot(task, kind, frame, force=False):
    """开启截图时把 frame 存进截图目录，返回路径；没开或保存失败返回 None。
    force：不看开关（卡厄思模式没有战斗记录的配置项，校准用的截图照样要存）。"""
    if not (force or enabled(task) and _config(task, SHOT_KEY)) or frame is None or not getattr(frame, "size", 0):
        return None
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        name = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_战斗{_state(task)['battle_id']}_{kind}.jpg"
        shot = os.path.join(SHOT_DIR, name)
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPG_QUALITY])
        if not ok:
            return None
        data.tofile(shot)  # 路径含中文时 cv2.imwrite 会失败，改用 tofile
        return shot
    except OSError as e:
        task.log_info(f"截图保存失败：{e}")
        return None


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
