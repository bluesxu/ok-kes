"""
现场记录（出击模式、卡厄思模式共用）：内存里一直保留最近 30 秒的每一帧，出了问题把前后各 30 秒写成「现场包」，
事后用 scripts/scene.py 看文字时间线、画框、生成回放页、离线重放。术语和事件见 CONTEXT.md「现场包」。

- 每帧记：全屏 OCR 文字、接手的处理函数（加速关闭时记不到）、所有识别调用的「方法 + 参数 → 结果」（重放时查表）、
  动作（点击坐标、按键）、这一帧写的战斗记录（出牌等决定和当时的识别结果）。
- 画面：1280 宽 JPEG 每秒最多 2 张；有动作的那一帧一定存，并另存一份原尺寸（重放时复现识别）。编码在后台线程做。
- 触发：TRIGGER_KINDS 里的异常、战斗输了、处理函数出错、主窗口侧边栏的「标记现场」按钮（src/globals.py，要写说明）。
  录制中又触发就从新触发起再录 30 秒并入同一个包，单个包最长 3 分钟；同类异常每轮只完整保存一次，每轮最多 5 个包
  （手动标记不受这两条限制）。
- 开关复用「详细战斗日志」+「异常截图」，与异常截图相同；清理复用 battle_logs 的保留天数和总大小上限。

由 speedup.install 在两个模式上安装（它接管了 run 和点击按键，这里再包一层）；官方版补丁不带本模块，
battle_log 调用本模块时会先确认能导入。
注意：本文件不能定义顶层类，框架会把 ok_tasks 下含类的 .py 当作任务加载。
"""
import collections
import concurrent.futures
import datetime
import json
import os
import pickle
import re
import threading
import time

import cv2
import numpy as np

import battle_log

PRE_SECONDS = 30      # 触发前保留多久
POST_SECONDS = 30     # 触发后再录多久
MAX_SECONDS = 180     # 单个现场包最长多久
MAX_PER_ROUND = 5     # 每轮最多几个现场包（手动标记不计）
IMAGE_GAP = 0.5       # 没有动作的帧，两张画面至少隔这么久
IMAGE_WIDTH = 1280
_SMALL_QUALITY = 70
_FULL_QUALITY = 85
_SAME_PAGE = 0.6      # 没有接手的处理函数时，画面文字重合度达到这么多算同一个页面

SCENE_DIR = os.path.join(battle_log.LOG_DIR, "现场")
MARK_KEY = "标记现场"
MANUAL = "手动标记"
RUNNING_GAP = 5       # 最近这么多秒内跑过一帧，才算任务在运行
LOST_BATTLE = "战斗输了"
HANDLER_ERROR = "处理函数出错"
# 写现场包的异常；其余异常（AP不足、牌名没读到等常见且有兜底的）只记日志。名单可按需要调整
TRIGGER_KINDS = frozenset((
    "疑似循环", "未识别页面", "画面卡住", "出不掉牌", "按键无效", "按 E 无效", "结束回合按钮一直不出现",
    "手牌识别失败", "意外打开撤退菜单", "装备页读不到装备", "零式系统读不到存档价值", "赛季初始页读不到存档价值",
    "记忆卡读不到文字", LOST_BATTLE, HANDLER_ERROR, MANUAL,
))

# 识别类调用：结果记下来，重放时按「方法 + 参数」查表
PERCEPTION_METHODS = ("ocr", "find_feature", "find_one", "feature_exists", "wait_ocr", "wait_feature", "next_frame")
# 动作类调用：click_box 内部还会调用 click，只记最外层
ACTION_METHODS = ("click", "click_box", "click_relative", "send_key", "scroll", "scroll_relative", "move",
                  "move_relative", "mouse_down", "mouse_up", "swipe_relative")
# 框架用来停止任务的异常，不算处理函数出错
_STOP_EXCEPTIONS = ("TaskDisabledException", "FinishedException", "CaptureException")
# 不属于跨帧状态的任务属性（框架对象、方法、本模块和加速补丁自己的状态、每帧都会重算的画面数据）
_STATE_SKIP = frozenset(("_recorder", "_speedup", "all_texts", "_prev_frame_gray", "_speedup_stuck_sample",
                         "executor", "config", "default_config", "config_description", "config_type"))
_STATE_ALWAYS = ("node_status", "member_status", "_battle_log")

_ENCODER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="现场记录")
_local = threading.local()


def install(task):
    """包装任务的 run、识别和动作方法；重复调用无副作用。要在 speedup.install 接管这些方法之后调用。"""
    if getattr(task, "_recorder", None) is not None or getattr(task, "_replay", False):
        return
    st = {
        "lock": threading.RLock(), "frames": collections.deque(), "images": collections.deque(), "next_image": 0,
        "current": None, "loose_events": [], "last_frame_obj": None, "last_image": None, "last_small_t": 0.0,
        "recording": None, "round_id": None, "count": 0, "seen": [], "mark": None, "hold": None,
        "last_hit": None, "last_frame_t": 0.0,
        "baseline": set(vars(task)),
    }
    task._recorder = st
    for name in PERCEPTION_METHODS:
        if callable(getattr(task, name, None)):
            setattr(task, name, _perception(task, st, name, getattr(task, name)))
    for name in ACTION_METHODS:
        if callable(getattr(task, name, None)):
            setattr(task, name, _action(task, st, name, getattr(task, name)))
    task.run = _run(task, st, task.run)


# ---------------- 手动标记（侧边栏按钮，界面线程调用） ----------------

def running(task):
    st = getattr(task, "_recorder", None)
    return st is not None and time.time() - st["last_frame_t"] <= RUNNING_GAP


def hold(task):
    """按下「标记现场」：先记下这一刻，写说明期间不裁掉这一刻之前 PRE_SECONDS 秒的帧。返回无法标记的原因，能标记返回 None。"""
    st = getattr(task, "_recorder", None)
    if st is None:
        return "任务没装现场记录"
    if not enabled(task):
        return "「详细战斗日志」或「异常截图」没开，标记无效"
    if not running(task):
        return "任务没在运行，标记无效"
    st["hold"] = time.time()
    return None


def mark(task, note):
    """说明写好了：下一帧开始录，录到按下按钮后 POST_SECONDS 秒。马上弹提示，不然要等录完才有动静。"""
    st = task._recorder
    at = st["hold"] or time.time()
    st["mark"] = (at, note)
    _notify(task, f"已标记，录到按下按钮后 {POST_SECONDS} 秒存好现场包")


def unhold(task):
    """取消写说明：不存现场包。"""
    st = getattr(task, "_recorder", None)
    if st is not None:
        st["hold"] = None


def _notify(task, message, error=False):
    """弹出界面提示；拿不到界面时只记日志。"""
    task.log_info(f"现场记录：{message}")
    try:
        task.notification(message, title=MARK_KEY, error=error)
    except Exception:
        pass


def enabled(task):
    return battle_log.enabled(task) and bool(battle_log._config(task, battle_log.SHOT_KEY))


# ---------------- 记录 ----------------

def _run(task, st, inner):
    def run():
        if not enabled(task):
            with st["lock"]:
                st["frames"].clear()
                st["images"].clear()
                st["current"] = None
            return inner()
        _begin_frame(task, st)
        try:
            return inner()
        except Exception as e:
            if type(e).__name__ not in _STOP_EXCEPTIONS:
                st["current"]["error"] = f"{type(e).__name__}: {e}"
                trigger(task, HANDLER_ERROR, f"{type(e).__name__}: {e}")
            raise
        finally:
            _end_frame(task, st)
    return run


def _begin_frame(task, st):
    now = time.time()
    with st["lock"]:
        _sync_round(task, st)
        frame = {"t": now, "hit": None, "gated": False, "texts": None, "calls": [], "actions": [],
                 "events": st["loose_events"], "images": [], "error": None, "state": _snapshot(task, st)}
        st["loose_events"] = []
        st["current"] = frame
    if st["mark"]:
        (at, note), st["mark"] = st["mark"], None
        trigger(task, MANUAL, f"界面上按了「标记现场」：{note}", at=at, note=note)
        st["hold"] = None


def _sync_round(task, st):
    """新的一轮：每轮上限和同类去重重新计。"""
    round_id = battle_log._state(task)["round_id"]
    if round_id != st["round_id"]:
        st.update(round_id=round_id, count=0, seen=[])


def _end_frame(task, st):
    now = time.time()
    speed = getattr(task, "_speedup", None) or {}
    with st["lock"]:
        frame = st["current"]
        if frame is None:
            return
        st["current"] = None
        st["last_frame_t"] = now
        # 加速补丁每帧开头把 hit 清空，run 结束时已把 active 复位，所以这里不看 active；加速关闭时 hit 一直是 None
        frame["hit"] = speed.get("hit")
        frame["gated"] = bool(speed.get("gated"))
        if frame["hit"]:
            st["last_hit"] = frame["hit"]
        st["frames"].append(frame)
        rec = st["recording"]
        if rec is not None:
            rec["frames"].append(frame)
        _prune(st, now)
    _finish_if_due(task, st)


def _prune(st, now):
    """不在录制中时只留最近 PRE_SECONDS 秒；录制中的帧已放进 recording，这里照样裁掉。"""
    frames, images = st["frames"], st["images"]
    cutoff = now - PRE_SECONDS
    if st["hold"] is not None and now - st["hold"] < MAX_SECONDS:  # 正在写说明：留住按下按钮前 PRE_SECONDS 秒
        cutoff = min(cutoff, st["hold"] - PRE_SECONDS)
    while frames and frames[0]["t"] < cutoff:
        frames.popleft()
    oldest = frames[0]["t"] if frames else now
    rec = st["recording"]
    if rec is not None:
        oldest = min(oldest, rec["start"])
    while images and images[0]["t"] < oldest - 1:
        images.popleft()


def _snapshot(task, st):
    """跨帧状态（node_status、member_status 和处理函数挂在任务上的各种计数）：每帧开头存一份，
    重放从现场包第一帧的状态开始。存不下的属性跳过。"""
    state = {}
    for name, value in vars(task).items():
        if name in _STATE_SKIP or callable(value):
            continue
        if name in st["baseline"] and name not in _STATE_ALWAYS:
            continue
        try:
            state[name] = pickle.dumps(value)
        except Exception:
            continue
    return state


def _capture(task, st, full):
    """记下当前画面，返回编号。同一个画面对象只存一次；full 时补一份原尺寸。没动作的帧受 IMAGE_GAP 限制。"""
    # 直接取执行器手上的当前帧：task.frame 在没有帧时会去截一帧，不能因为记录改变取帧时机
    image = getattr(getattr(task, "executor", None), "_frame", None)
    if image is None or not getattr(image, "size", 0):
        return None
    now = time.time()
    with st["lock"]:
        last = st["last_image"]
        if last is not None and st["last_frame_obj"] is image:
            if full and last["full"] is None:
                last["full"] = _ENCODER.submit(_encode, image, False)
            return last["id"]
        if not full and now - st["last_small_t"] < IMAGE_GAP:
            return None
        entry = {"id": st["next_image"], "t": now, "size": [int(image.shape[1]), int(image.shape[0])],
                 "small": _ENCODER.submit(_encode, image, True),
                 "full": _ENCODER.submit(_encode, image, False) if full else None}
        st["next_image"] += 1
        st["images"].append(entry)
        st["last_image"], st["last_frame_obj"], st["last_small_t"] = entry, image, now
        return entry["id"]


def _encode(image, small):
    if small and image.shape[1] > IMAGE_WIDTH:
        height = round(image.shape[0] * IMAGE_WIDTH / image.shape[1])
        image = cv2.resize(image, (IMAGE_WIDTH, height), interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, _SMALL_QUALITY if small else _FULL_QUALITY])
    return data.tobytes() if ok else None


def _depth(kind):
    return getattr(_local, kind, 0)


def _perception(task, st, name, inner):
    def wrapped(*args, **kwargs):
        frame = st["current"]
        if frame is None or _depth("perception"):
            return inner(*args, **kwargs)
        _local.perception = 1
        try:
            full_ocr = name == "ocr" and not args and kwargs.get("frame") is None and frame["texts"] is None
            result = inner(*args, **kwargs)
            frame["calls"].append({"m": name, "k": call_key(name, args, kwargs), "r": encode_result(result),
                                   "t": round(time.time() - frame["t"], 3)})
            if full_ocr:  # 本帧的全屏识别：记下文字，顺带存一张画面（识别完才一定有帧）
                frame["texts"] = _texts(result)
            if full_ocr or name == "next_frame":
                image_id = _capture(task, st, False)
                if image_id is not None:
                    frame["images"].append(image_id)
            return result
        finally:
            _local.perception = 0
    return wrapped


def _action(task, st, name, inner):
    def wrapped(*args, **kwargs):
        frame = st["current"]
        if frame is None or _depth("action"):
            return inner(*args, **kwargs)
        _local.action = 1
        try:
            image_id = _capture(task, st, True)  # 做决定时看到的画面，存原尺寸
            if image_id is not None and image_id not in frame["images"]:
                frame["images"].append(image_id)
            frame["actions"].append({"m": name, "a": call_key(name, args, kwargs), "img": image_id,
                                     "t": round(time.time() - frame["t"], 3)})
            return inner(*args, **kwargs)
        finally:
            _local.action = 0
    return wrapped


def note_action(task, name, *args, **kwargs):
    """绕过任务方法直接发给游戏的动作（如出击模式的后台拖牌）由调用处报上来，记法和包装过的动作一样。"""
    st = getattr(task, "_recorder", None)
    frame = st and st["current"]
    if frame is None:
        return
    image_id = _capture(task, st, True)
    if image_id is not None and image_id not in frame["images"]:
        frame["images"].append(image_id)
    frame["actions"].append({"m": name, "a": call_key(name, args, kwargs), "img": image_id,
                             "t": round(time.time() - frame["t"], 3)})


def note_event(task, entry):
    """battle_log.record 每写一条（不论详细日志开没开）都交一份过来，挂在当前帧上。"""
    st = getattr(task, "_recorder", None)
    if st is None:
        return
    with st["lock"]:
        target = st["current"]["events"] if st["current"] is not None else st["loose_events"]
        target.append(entry)
        del st["loose_events"][:-50]


def _texts(boxes):
    try:
        import utils
        boxes = utils._simplify_texts(boxes)
    except Exception:
        pass
    return [[b.name, int(b.x), int(b.y), int(b.width), int(b.height)] for b in boxes or [] if hasattr(b, "name")]


# ---------------- 参数和结果的序列化（scene.py 重放时用同样的规则查表） ----------------

def _plain(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, np.ndarray):
        return {"ndarray": list(value.shape)}
    if isinstance(value, re.Pattern):
        return {"re": value.pattern}
    if hasattr(value, "x") and hasattr(value, "width") and hasattr(value, "name"):
        return {"box": [int(value.x), int(value.y), int(value.width), int(value.height), value.name]}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if callable(value):
        return {"fn": getattr(value, "__name__", "?")}
    return str(value)


def call_key(name, args, kwargs):
    """调用的「方法 + 参数」，转成可比较的 JSON 字符串。"""
    return json.dumps([name, _plain(list(args)), _plain(dict(sorted(kwargs.items())))], ensure_ascii=False)


def encode_result(result):
    if isinstance(result, np.ndarray):
        return {"ndarray": list(result.shape)}
    if result is None or isinstance(result, (bool, int, float, str)):
        return result
    if hasattr(result, "x") and hasattr(result, "width"):
        return {"box": [int(result.x), int(result.y), int(result.width), int(result.height),
                        getattr(result, "name", None), float(getattr(result, "confidence", 0) or 0)]}
    if isinstance(result, (list, tuple)):
        return [encode_result(v) for v in result]
    return {"repr": str(result)}


# ---------------- 触发和写盘 ----------------

def trigger(task, kind, detail="", at=None, note=None):
    """出了问题。返回现场包目录（本次记进了哪个包）；没开、被去重或超出上限时返回 None。
    at：出问题的时刻（手动标记是按下按钮的时刻，写说明要花时间），默认现在；note：手动标记写的说明。"""
    st = getattr(task, "_recorder", None)
    if st is None or not enabled(task) or kind not in TRIGGER_KINDS:
        return None
    now = time.time()
    at = now if at is None else min(at, now)
    entry = {"kind": kind, "detail": detail, "t": at}
    if note:
        entry["note"] = note
    with st["lock"]:
        _sync_round(task, st)
        rec = st["recording"]
        if rec is not None:
            rec["triggers"].append(entry)
            rec["until"] = min(max(rec["until"], at + POST_SECONDS), rec["start"] + MAX_SECONDS)
            _schedule(task, st, rec)
            return rec["path"]
        if kind != MANUAL:
            texts = set(t[0] for t in (st["frames"][-1]["texts"] or [])) if st["frames"] else set()
            same = next((s for s in st["seen"] if _same_kind(s, kind, st["last_hit"], texts)), None)
            if same is not None:
                same["times"] += 1
                skipped = f"同上，第 {same['times']} 次（{same['path']}）"
            elif st["count"] >= MAX_PER_ROUND:
                skipped = f"本轮已存 {MAX_PER_ROUND} 个现场包"
            else:
                skipped = None
            if skipped is not None:
                battle_log.record(task, "现场包跳过", kind=kind, reason=skipped)
                return None
            st["count"] += 1
        stamp = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        path = os.path.join(SCENE_DIR, f"{stamp}_{getattr(task, 'name', '')}_{kind}")
        frames = list(st["frames"])
        if kind == MANUAL:  # 按下按钮前 PRE_SECONDS 秒起（写说明期间多留的帧里更早的不要）
            frames = [f for f in frames if f["t"] >= at - PRE_SECONDS]
        rec = {"path": path, "start": frames[0]["t"] if frames else at, "until": at + POST_SECONDS,
               "triggers": [entry], "frames": frames, "timer": None,
               "round": battle_log._state(task)["round_id"], "battle": battle_log._state(task)["battle_id"]}
        rec["until"] = min(rec["until"], rec["start"] + MAX_SECONDS)
        if kind != MANUAL:
            st["seen"].append({"kind": kind, "hit": st["last_hit"], "texts": texts, "times": 1, "path": path})
        st["recording"] = rec
        _schedule(task, st, rec)
    task.log_info(f"现场记录：「{kind}」，记录前后 {PRE_SECONDS}+{POST_SECONDS} 秒到 {path}")
    return path


def _same_kind(seen, kind, hit, texts):
    if seen["kind"] != kind:
        return False
    if hit or seen["hit"]:
        return hit == seen["hit"]
    union = texts | seen["texts"]
    return not union or len(texts & seen["texts"]) / len(union) >= _SAME_PAGE


def _schedule(task, st, rec):
    """任务停了就没有下一帧来收尾，定个时兜底。"""
    if rec["timer"] is not None:
        rec["timer"].cancel()
    rec["timer"] = threading.Timer(max(0.0, rec["until"] - time.time()) + 1, _finish_if_due, (task, st))
    rec["timer"].daemon = True
    rec["timer"].start()


def _finish_if_due(task, st):
    with st["lock"]:
        rec = st["recording"]
        if rec is None or time.time() < rec["until"]:
            return
        st["recording"] = None
        if rec["timer"] is not None:
            rec["timer"].cancel()
        first = rec["frames"][0]["t"] if rec["frames"] else rec["start"]
        ids = {i for f in rec["frames"] for i in f["images"]}
        images = [img for img in st["images"] if img["id"] in ids or first - 1 <= img["t"] <= rec["until"]]
    _start_write(task, rec, images)


def _start_write(task, rec, images):
    """后台写盘，不耽误下一帧（测试里换成直接调用 _write）。"""
    threading.Thread(target=_write, args=(task, rec, images), daemon=True, name="现场记录写盘").start()


def _write(task, rec, images):
    try:
        path = rec["path"]
        os.makedirs(os.path.join(path, "frames"), exist_ok=True)
        os.makedirs(os.path.join(path, "full"), exist_ok=True)
        start = rec["frames"][0]["t"] if rec["frames"] else rec["start"]
        sizes = {}
        for image in images:
            sizes[image["id"]] = image["size"]
            for kind in ("small", "full"):
                future = image[kind]
                data = future.result() if future is not None else None
                if data:
                    folder = "frames" if kind == "small" else "full"
                    with open(os.path.join(path, folder, f"{image['id']:05d}.jpg"), "wb") as f:
                        f.write(data)
        with open(os.path.join(path, "timeline.jsonl"), "w", encoding="utf-8") as f:
            for index, frame in enumerate(rec["frames"]):
                line = {k: v for k, v in frame.items() if k != "state"}
                line.update(i=index, t=round(frame["t"] - start, 3), abs_t=frame["t"])
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        if rec["frames"]:
            with open(os.path.join(path, "state.pkl"), "wb") as f:
                pickle.dump(rec["frames"][0]["state"], f)
        meta = {
            "mode": getattr(task, "name", ""), "round": rec["round"], "battle": rec["battle"],
            "start": datetime.datetime.fromtimestamp(start).isoformat(timespec="seconds"),
            "seconds": round((rec["frames"][-1]["t"] - start) if rec["frames"] else 0, 1),
            "frames": len(rec["frames"]), "images": len(images),
            "triggers": [dict(t, t=round(t["t"] - start, 1)) for t in rec["triggers"]],
            "notes": [t["note"] for t in rec["triggers"] if t.get("note")],
            "image_sizes": {str(k): v for k, v in sizes.items()},
            "config": _config_summary(task),
        }
        with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
        battle_log.record(task, "现场包", path=path, kinds=[t["kind"] for t in rec["triggers"]],
                          seconds=meta["seconds"], frames=meta["frames"])
        message = f"已写入 {path}（{meta['frames']} 帧，{len(images)} 张画面）"
        if any(t["kind"] == MANUAL for t in rec["triggers"]):
            _notify(task, message)
        else:
            task.log_info(f"现场记录：{message}")
    except Exception as e:  # 写盘失败不影响任务
        task.log_info(f"现场记录写入失败：{e}")


def _config_summary(task):
    config = {}
    for source in (getattr(task, "default_config", None) or {}, getattr(task, "config", None) or {}):
        try:
            items = dict(source).items()
        except Exception:
            continue
        for key, value in items:
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue
            config[key] = value
    return config
