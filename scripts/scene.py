"""
现场包工具（现场包由 ok_tasks/recorder.py 写到 battle_logs/现场/，说明见 CLAUDE.md「现场包」）。

    python scripts/scene.py list                  # 还没写「诊断.md」的现场包
    python scripts/scene.py timeline <现场包>      # 精简文字时间线：页面切换、动作、决定、异常
    python scripts/scene.py timeline <现场包> --texts   # 每段再附上画面文字
    python scripts/scene.py draw <现场包> <帧序号>  # 给这一帧的画面画上 OCR 框和动作位置，存到 annotated/
    python scripts/scene.py html <现场包>          # 生成 index.html 回放页，浏览器里逐帧翻看
    python scripts/scene.py replay <现场包>        # 用记录的识别结果重跑页面处理函数，和当时的决定逐帧对比

<现场包> 可以写完整路径，也可以只写目录名里能唯一匹配的一段（如时间 20261002-1015）。
"""
import argparse
import collections
import json
import os
import pickle
import sys
import traceback
from unittest import mock

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(ROOT, "ok_tasks")
SCENE_DIR = os.path.join(ROOT, "battle_logs", "现场")
DIAGNOSIS = "诊断.md"
MODE_HANDLERS = {"自动卡厄思模式": "utils_chaos", "自动出击模式": "utils_sortie"}


# ---------------- 读取 ----------------

def resolve(name):
    if os.path.isdir(name):
        return os.path.abspath(name)
    matches = [d for d in sorted(os.listdir(SCENE_DIR)) if name in d] if os.path.isdir(SCENE_DIR) else []
    if len(matches) != 1:
        sys.exit(f"找不到唯一的现场包「{name}」：{matches or '没有匹配'}")
    return os.path.join(SCENE_DIR, matches[0])


def load(path):
    with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(path, "timeline.jsonl"), encoding="utf-8") as f:
        frames = [json.loads(line) for line in f if line.strip()]
    return meta, frames


def read_image(path, image_id, size=None, prefer_full=True):
    """读一张画面；没有原尺寸时把 1280 宽的放大回原尺寸（size 为 [宽, 高]）。"""
    for folder in (("full", "frames") if prefer_full else ("frames",)):
        file = os.path.join(path, folder, f"{image_id:05d}.jpg")
        if os.path.exists(file):
            image = cv2.imdecode(np.fromfile(file, dtype=np.uint8), cv2.IMREAD_COLOR)
            if size and folder == "frames" and image.shape[1] != size[0]:
                image = cv2.resize(image, tuple(size), interpolation=cv2.INTER_CUBIC)
            return image
    return None


def action_point(action, size):
    """动作在原画面上的位置 (x, y)；按键之类没有位置的返回 None。"""
    name, args, kwargs = json.loads(action["a"])
    width, height = size
    if name == "click_box":
        box = args[0] if args else kwargs.get("box")
        if isinstance(box, dict) and "box" in box:
            x, y, w, h = box["box"][:4]
            rx = args[1] if len(args) > 1 else kwargs.get("relative_x", 0.5)
            ry = args[2] if len(args) > 2 else kwargs.get("relative_y", 0.5)
            return x + w * rx, y + h * ry
        return None
    if name in ("drag", "drag_move"):  # 后台拖牌：落点（相对坐标）
        point = args[-1]
        return point[0] * width, point[1] * height
    if name in ("click", "click_relative", "move", "move_relative", "mouse_down"):
        x = args[0] if args else kwargs.get("x")
        y = args[1] if len(args) > 1 else kwargs.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or x < 0 or y < 0:
            return None
        if name.endswith("_relative") or (x <= 1 and y <= 1 and (isinstance(x, float) or isinstance(y, float))):
            return x * width, y * height
        return x, y
    return None


def action_text(action):
    name, args, kwargs = json.loads(action["a"])
    if name == "send_key":
        return f"按键 {args[0] if args else kwargs.get('key')}"
    if name == "drag":
        return f"拖牌 ({args[0][0]:.3f}, {args[0][1]:.3f}) → ({args[1][0]:.3f}, {args[1][1]:.3f})"
    if name == "drag_move":
        return f"拖着移到 ({args[0][0]:.3f}, {args[0][1]:.3f})"
    if name == "click_box":
        box = args[0] if args else kwargs.get("box")
        label = box["box"][4] if isinstance(box, dict) and "box" in box else box
        return f"点「{label}」"
    shown = [a for a in args if not isinstance(a, dict)][:4]
    return f"{name}({', '.join(str(a) for a in shown)})"


def event_text(event):
    name = event.get("event")
    keys = ("kind", "card", "key", "reason", "target_reason", "chosen", "choice", "action", "detail", "result")
    parts = [f"{k}={event[k]}" for k in keys if event.get(k) not in (None, "", [])]
    return f"{name}：{'，'.join(str(p) for p in parts)[:200]}"


# ---------------- list / timeline ----------------

def cmd_list(_args):
    if not os.path.isdir(SCENE_DIR):
        print("还没有现场包")
        return
    shown = 0
    for name in sorted(os.listdir(SCENE_DIR)):
        path = os.path.join(SCENE_DIR, name)
        if not os.path.isdir(path) or os.path.exists(os.path.join(path, DIAGNOSIS)):
            continue
        try:
            meta, _ = load(path)
        except OSError:
            print(f"{name}  （还没写完或已损坏）")
            continue
        kinds = "、".join(t["kind"] for t in meta["triggers"])
        print(f"{name}  {meta['mode']} 第{meta['round']}轮 战斗{meta['battle']}  {meta['seconds']}秒/{meta['frames']}帧  触发：{kinds}")
        for note in meta.get("notes") or []:  # 手动标记时写的说明
            print(f"    说明：{note}")
        shown += 1
    if not shown:
        print("没有待看的现场包（都已写了诊断.md）")


def cmd_timeline(args):
    path = resolve(args.scene)
    meta, frames = load(path)
    print(f"{os.path.basename(path)}  {meta['mode']} 第{meta['round']}轮 战斗{meta['battle']}  开始 {meta['start']}")
    for trigger in meta["triggers"]:
        print(f"  触发 +{trigger['t']}s「{trigger['kind']}」{trigger['detail']}")
    triggers = collections.deque(sorted(meta["triggers"], key=lambda t: t["t"]))
    previous, repeat = None, 0

    def flush():
        if repeat:
            print(f"        … 同上 {repeat} 帧")

    for frame in frames:
        while triggers and triggers[0]["t"] <= frame["t"]:
            flush()
            repeat = 0
            trigger = triggers.popleft()
            print(f"  ==== +{trigger['t']}s 触发「{trigger['kind']}」 ====")
        label = "（闸门等待）" if frame["gated"] else frame["hit"] or ("（出错）" if frame["error"] else "（无人接手）")
        # 同一个处理函数、同样的动作、没有新记录：算重复帧（循环时每帧点同一个按钮也合并成一行）
        sig = (label, tuple(action_text(a) for a in frame["actions"]))
        if sig == previous and not frame["events"] and not frame["error"]:
            repeat += 1
            continue
        flush()
        repeat = 0
        images = ",".join(str(i) for i in frame["images"])
        print(f"#{frame['i']:<4} +{frame['t']:7.2f}s {label}" + (f"  [画面 {images}]" if images else ""))
        if args.texts or (label != (previous or ("",))[0] and not frame["hit"] and not frame["gated"]):
            texts = [t[0] for t in frame["texts"] or []]
            if texts:
                print(f"        文字：{' | '.join(texts[:40 if args.texts else 15])}")
        for action in frame["actions"]:
            print(f"        动作 +{action['t']:.2f}s {action_text(action)}")
        for event in frame["events"]:
            print(f"        记录 {event_text(event)}")
        if frame["error"]:
            print(f"        出错 {frame['error']}")
        previous = sig
    flush()


# ---------------- draw / html ----------------

def frame_of_image(frames, image_id):
    return next((f for f in frames if image_id in f["images"]), None)


def cmd_draw(args):
    path = resolve(args.scene)
    meta, frames = load(path)
    frame = frames[args.frame]
    if not frame["images"]:
        sys.exit(f"第 {args.frame} 帧没有存画面，附近有画面的帧："
                 f"{[f['i'] for f in frames if f['images'] and abs(f['i'] - args.frame) <= 10]}")
    os.makedirs(os.path.join(path, "annotated"), exist_ok=True)
    for image_id in frame["images"]:
        size = meta["image_sizes"].get(str(image_id))
        image = read_image(path, image_id, size)
        if image is None:
            continue
        scale = max(1.0, image.shape[1] / 1600)
        for index, (text, x, y, w, h) in enumerate(frame["texts"] or []):
            cv2.rectangle(image, (x, y), (x + w, y + h), (0, 200, 255), max(1, int(2 * scale)))
            cv2.putText(image, str(index), (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * scale, (0, 200, 255),
                        max(1, int(scale)))
        for number, action in enumerate(frame["actions"], 1):
            point = action_point(action, size or [image.shape[1], image.shape[0]])
            if action.get("img") != image_id or point is None:
                continue
            center = (int(point[0]), int(point[1]))
            cv2.circle(image, center, int(18 * scale), (0, 0, 255), max(2, int(3 * scale)))
            cv2.putText(image, f"A{number}", (center[0] + int(20 * scale), center[1]), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8 * scale, (0, 0, 255), max(2, int(2 * scale)))
        out = os.path.join(path, "annotated", f"{image_id:05d}.jpg")
        cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(out)
        print(f"画面 {image_id} → {out}")
    print("OCR 框编号（这一帧全屏识别的文字）：")
    for index, (text, *_rest) in enumerate(frame["texts"] or []):
        print(f"  {index}: {text}")
    for number, action in enumerate(frame["actions"], 1):
        print(f"  A{number}: {action_text(action)}（画面 {action.get('img')}）")


def cmd_html(args):
    path = resolve(args.scene)
    meta, frames = load(path)
    data = []
    current = None
    for frame in frames:
        if frame["images"]:
            current = frame["images"][0]
        image = current
        size = meta["image_sizes"].get(str(image)) if image is not None else None
        points = []
        for number, action in enumerate(frame["actions"], 1):
            point = action_point(action, size) if size and action.get("img") == image else None
            points.append({"n": number, "text": action_text(action), "p": point and [round(v) for v in point]})
        data.append({
            "i": frame["i"], "t": frame["t"],
            "label": "（闸门等待）" if frame["gated"] else frame["hit"] or ("（出错）" if frame["error"] else "（无人接手）"),
            "img": image, "size": size, "texts": frame["texts"] or [], "actions": points,
            "events": [event_text(e) for e in frame["events"]], "error": frame["error"],
            "calls": len(frame["calls"]),
        })
    page = _HTML.replace("__TITLE__", os.path.basename(path)).replace(
        "__DATA__", json.dumps({"meta": meta, "frames": data}, ensure_ascii=False).replace("</", "<\\/"))
    out = os.path.join(path, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(out)


_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#f6f6f4; --panel:#fff; --text:#222; --muted:#777; --line:#ddd; --accent:#d33; --hit:#2a7; --sel:#ffe9a8; }
@media (prefers-color-scheme: dark) { :root { --bg:#1b1b1d; --panel:#242427; --text:#e8e8e8; --muted:#999; --line:#3a3a3e; --accent:#ff6b6b; --hit:#5c9; --sel:#5a4a14; } }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui, "Microsoft YaHei", sans-serif; }
header { padding:10px 16px; border-bottom:1px solid var(--line); } header b { font-size:15px; } .muted { color:var(--muted); }
main { display:grid; grid-template-columns:300px 1fr; height:calc(100vh - 58px); }
#list { overflow:auto; border-right:1px solid var(--line); background:var(--panel); }
#list div { padding:3px 10px; cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-bottom:1px solid var(--line); }
#list div.sel { background:var(--sel); } #list .act { color:var(--accent); } #list .trig { background:var(--accent); color:#fff; }
#view { overflow:auto; padding:12px 16px; } #stage { position:relative; display:inline-block; max-width:100%; }
#stage img { max-width:100%; display:block; } #stage svg { position:absolute; inset:0; width:100%; height:100%; }
.box { fill:none; stroke:#fb0; stroke-width:2; } .pt { fill:none; stroke:var(--accent); stroke-width:5; } .pl { fill:var(--accent); font:bold 36px sans-serif; }
#info p { margin:4px 0; } .hit { color:var(--hit); font-weight:bold; } label { user-select:none; }
@media (max-width:800px) { main { grid-template-columns:1fr; height:auto; } #list { max-height:30vh; } }
</style></head><body>
<header><b>__TITLE__</b> <span id="sum" class="muted"></span> <label><input type="checkbox" id="boxes"> 显示 OCR 框</label> <span class="muted">← → 翻帧</span></header>
<main><div id="list"></div><div id="view"><div id="stage"><img id="img"><svg id="svg"></svg></div><div id="info"></div></div></main>
<script>
const D = __DATA__; let cur = 0;
const list = document.getElementById('list'), img = document.getElementById('img'), svg = document.getElementById('svg');
const trigAt = D.meta.triggers.map(t => t.t);
document.getElementById('sum').textContent = `${D.meta.mode} 第${D.meta.round}轮 战斗${D.meta.battle} ${D.meta.seconds}秒 触发：` + D.meta.triggers.map(t => `+${t.t}s ${t.kind}`).join('，');
D.frames.forEach((f, k) => {
  const d = document.createElement('div');
  const trig = trigAt.some(t => t >= f.t && (k + 1 >= D.frames.length || t < D.frames[k + 1].t));
  d.textContent = `#${f.i} +${f.t.toFixed(1)}s ${f.label}` + (f.actions.length ? ` · ${f.actions.map(a => a.text).join('；')}` : '');
  if (f.actions.length || f.events.length) d.classList.add('act');
  if (trig) d.classList.add('trig');
  d.onclick = () => show(k); list.appendChild(d);
});
function esc(s) { return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function show(k) {
  cur = Math.max(0, Math.min(D.frames.length - 1, k)); const f = D.frames[cur];
  [...list.children].forEach((d, j) => d.classList.toggle('sel', j === cur)); list.children[cur].scrollIntoView({block: 'nearest'});
  img.src = f.img === null ? '' : `frames/${String(f.img).padStart(5, '0')}.jpg`;
  let s = '';
  if (f.size) {
    svg.setAttribute('viewBox', `0 0 ${f.size[0]} ${f.size[1]}`);
    if (document.getElementById('boxes').checked) f.texts.forEach(t => { s += `<rect class="box" x="${t[1]}" y="${t[2]}" width="${t[3]}" height="${t[4]}"><title>${esc(t[0])}</title></rect>`; });
    f.actions.forEach(a => { if (a.p) s += `<circle class="pt" cx="${a.p[0]}" cy="${a.p[1]}" r="28"/><text class="pl" x="${a.p[0] + 34}" y="${a.p[1] + 12}">A${a.n}</text>`; });
  }
  svg.innerHTML = s;
  document.getElementById('info').innerHTML =
    `<p>#${f.i} +${f.t.toFixed(2)}s <span class="hit">${esc(f.label)}</span> <span class="muted">画面 ${f.img ?? '无'} · 识别调用 ${f.calls} 次</span></p>` +
    f.actions.map(a => `<p>A${a.n} ${esc(a.text)}</p>`).join('') + f.events.map(e => `<p>记录 ${esc(e)}</p>`).join('') +
    (f.error ? `<p style="color:var(--accent)">出错 ${esc(f.error)}</p>` : '') +
    `<p class="muted">${f.texts.map(t => esc(t[0])).join(' | ')}</p>`;
}
document.getElementById('boxes').onchange = () => show(cur);
document.addEventListener('keydown', e => { if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { show(cur + 1); e.preventDefault(); } if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { show(cur - 1); e.preventDefault(); } });
show(Math.max(0, D.frames.findIndex(f => f.t >= (trigAt[0] ?? 0)) - 3));
</script></body></html>
"""


# ---------------- replay ----------------

class ReplayTask:
    """重放用的任务：识别调用从记录里取，动作只记下来，sleep 只推进时钟。"""

    def __init__(self, path, meta, clock):
        self._replay = True
        self.name = meta["mode"]
        self.config = dict(meta["config"])
        self.default_config = dict(meta["config"])
        self.config_description = {}
        self.config_type = {}
        self.trigger_interval = 1
        self._path, self._meta, self._clock = path, meta, clock
        self._images, self._image_index, self._image_cache = [], 0, {}
        self._pending = {}
        self.replay_actions, self.replay_misses, self.replay_logs = [], [], []
        self._prev_frame_gray = None  # state.pkl 不存上一帧灰度图，却存了 _last_change_time，is_frame_stuck 会直接读它
        self.executor = _ReplayExecutor(self)
        first = next(iter(meta["image_sizes"].values()), [2560, 1440])
        self.width, self.height = first

    # 每帧开始时由 replay() 设置
    def _start_frame(self, frame, images):
        self._pending = collections.defaultdict(collections.deque)
        for call in frame["calls"]:
            self._pending[call["k"]].append(call["r"])
        self._images, self._image_index = images, 0
        self.replay_actions, self.replay_misses = [], []

    @property
    def frame(self):
        if not self._images:
            return None
        image_id = self._images[min(self._image_index, len(self._images) - 1)]
        if image_id not in self._image_cache:
            self._image_cache.clear()
            self._image_cache[image_id] = read_image(self._path, image_id, self._meta["image_sizes"].get(str(image_id)))
        return self._image_cache[image_id]

    def _serve(self, name, args, kwargs, default):
        import recorder
        key = recorder.call_key(name, args, kwargs)
        queue = self._pending.get(key)
        if queue:
            return _decode(queue.popleft(), self)
        self.replay_misses.append(key)
        return default()

    def ocr(self, *args, **kwargs):
        return self._serve("ocr", args, kwargs, lambda: _real_ocr(self, *args, **kwargs))

    def find_feature(self, *args, **kwargs):
        return self._serve("find_feature", args, kwargs, list)

    def find_one(self, *args, **kwargs):
        return self._serve("find_one", args, kwargs, lambda: None)

    def feature_exists(self, *args, **kwargs):
        return self._serve("feature_exists", args, kwargs, lambda: False)

    def wait_ocr(self, *args, **kwargs):
        return self._serve("wait_ocr", args, kwargs, lambda: None)

    def wait_feature(self, *args, **kwargs):
        return self._serve("wait_feature", args, kwargs, lambda: None)

    def next_frame(self, *args, **kwargs):
        self._serve("next_frame", args, kwargs, lambda: None)
        self._image_index = min(self._image_index + 1, max(0, len(self._images) - 1))
        return self.frame

    def _act(self, name, args, kwargs):
        import recorder
        self.replay_actions.append(recorder.call_key(name, args, kwargs))
        return True

    def click(self, *a, **k): return self._act("click", a, k)
    def click_box(self, *a, **k): return self._act("click_box", a, k)
    def click_relative(self, *a, **k): return self._act("click_relative", a, k)
    def send_key(self, *a, **k): return self._act("send_key", a, k)
    def scroll(self, *a, **k): return self._act("scroll", a, k)
    def scroll_relative(self, *a, **k): return self._act("scroll_relative", a, k)
    def move(self, *a, **k): return self._act("move", a, k)
    def move_relative(self, *a, **k): return self._act("move_relative", a, k)
    def mouse_down(self, *a, **k): return self._act("mouse_down", a, k)
    def mouse_up(self, *a, **k): return self._act("mouse_up", a, k)
    def swipe_relative(self, x1, y1, x2, y2, *a, **k):  # 当时走的是后台拖动，按「drag」记才能对上
        return self._act("drag", ((x1, y1), (x2, y2)), {})

    def sleep(self, timeout):
        self._clock[0] += max(0.0, timeout or 0)
        return True

    def box_of_screen(self, x, y, to_x=1.0, to_y=1.0, width=0.0, height=0.0, name=None, **_kwargs):
        # 和框架的 BaseTask.box_of_screen / relative_box 一致（四舍五入、名字用传入的 width/height），
        # 否则调用键对不上记录，模板匹配全部当没找到
        from ok import relative_box
        return relative_box(self.width, self.height, x, y, to_x=to_x, to_y=to_y, width=width, height=height,
                            name=name if name is not None else f"{x} {y} {width} {height}")

    def log_info(self, message, *a, **k): self.replay_logs.append(str(message))
    log_debug = log_info
    log_error = log_info
    def info_set(self, *a, **k): pass
    def is_adb(self): return False
    def disable(self): self.replay_logs.append("任务被停止（disable）")
    def run(self): pass
    def _check_upload_if_needed(self): pass


class _ReplayExecutor:
    def __init__(self, task):
        self._task = task
        self.paused = False
        self.feature_set = None

    @property
    def frame(self):
        return self._task.frame

    @property
    def _frame(self):
        return self._task.frame

    def next_frame(self, *a, **k):
        return self._task.next_frame()

    def reset_scene(self, *a, **k): pass
    def check_enabled(self, *a, **k): pass


def _decode(value, task):
    from ok import Box
    if isinstance(value, list):
        return [_decode(v, task) for v in value]
    if isinstance(value, dict):
        if "box" in value:
            x, y, w, h, name, *rest = value["box"]
            return Box(x, y, w, h, confidence=rest[0] if rest else 1.0, name=name)
        if "ndarray" in value:
            return task.frame
        return value.get("repr")
    return value


_engine = None


def _real_ocr(task, x=0, y=0, to_x=1, to_y=1, frame=None, **_kwargs):
    """记录里没有的识别调用（改代码后新增的）：在当时的画面上真的跑一次 OCR。"""
    global _engine
    from ok import Box
    image = frame if frame is not None else task.frame
    if image is None:
        return []
    ox = oy = 0
    if frame is None:
        height, width = image.shape[:2]
        ox, oy = int(x * width), int(y * height)
        image = image[oy:int(to_y * height), ox:int(to_x * width)]
    if _engine is None:
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr
        _engine = ONNXPaddleOcr(use_angle_cls=False, use_openvino=False)
    boxes = []
    for points, (text, confidence) in _engine.ocr(image)[0] or []:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        boxes.append(Box(int(min(xs)) + ox, int(min(ys)) + oy, int(max(xs) - min(xs)), int(max(ys) - min(ys)),
                         confidence=confidence, name=text))
    match = _kwargs.get("match")
    if match is not None:
        import re
        patterns = match if isinstance(match, list) else [match]
        boxes = [b for b in boxes if any(re.search(p, b.name) if isinstance(p, (str, re.Pattern)) else False
                                         for p in patterns)]
    return boxes


def cmd_replay(args):
    path = resolve(args.scene)
    meta, frames = load(path)
    module_name = MODE_HANDLERS.get(meta["mode"])
    if module_name is None:
        sys.exit(f"不认识的模式：{meta['mode']}")
    sys.path.insert(0, TASKS)
    os.chdir(ROOT)
    import battle_log
    import recorder
    import speedup
    import utils
    handlers = __import__(module_name)
    clock = [frames[0]["abs_t"] if frames else 0.0]
    task = ReplayTask(path, meta, clock)
    events = []

    def now():
        # 每次读时间推进一点：按真实时间计时的等待循环（如 _wait_hand_settled）在冻结的时钟下永远退不出
        clock[0] += 0.02
        return clock[0]

    with mock.patch("time.time", now), \
            mock.patch.object(battle_log, "enabled", lambda _task: False), \
            mock.patch.object(recorder, "note_event", lambda _task, entry: events.append(entry)):
        speedup.install(task)  # 只为套上加速补丁对各模块处理函数的替换；run 不经过它，补丁处于未启用状态
        task.config.update(meta["config"])
        state_file = os.path.join(path, "state.pkl")
        if os.path.exists(state_file):
            with open(state_file, "rb") as f:
                for name, data in pickle.load(f).items():
                    try:
                        setattr(task, name, pickle.loads(data))
                    except Exception as e:
                        print(f"状态「{name}」恢复失败：{e}")
        last_image = None
        same = differ = skipped = 0
        for frame in frames:
            images = list(frame["images"]) or ([last_image] if last_image is not None else [])
            if frame["images"]:
                last_image = frame["images"][-1]
            if frame["gated"] or frame["texts"] is None:
                skipped += 1
                continue
            clock[0] = frame["abs_t"]
            task._start_frame(frame, images)
            events.clear()
            hit, error = None, None
            try:
                task.all_texts = utils._simplify_texts(task.ocr())
                for handle_page in handlers.PAGE_HANDLERS:
                    if handle_page(task):
                        hit = handle_page.__name__
                        break
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                if args.verbose:
                    traceback.print_exc()
            recorded_actions = [a["a"] for a in frame["actions"]]
            ok = hit == frame["hit"] and task.replay_actions == recorded_actions
            same += ok
            differ += not ok
            if ok and not args.verbose:
                continue
            mark = "  " if ok else "≠ "
            print(f"{mark}#{frame['i']:<4} +{frame['t']:7.2f}s 当时 {frame['hit'] or '（无人接手）'}  重放 {hit or '（无人接手）'}")
            if task.replay_actions != recorded_actions:
                print(f"        当时动作：{[action_text({'a': a}) for a in recorded_actions]}")
                print(f"        重放动作：{[action_text({'a': a}) for a in task.replay_actions]}")
            for event in events:
                print(f"        重放记录 {event_text(event)}")
            for miss in task.replay_misses[:5]:
                print(f"        记录里没有的识别调用：{miss}")
            if error:
                print(f"        重放出错 {error}")
            if args.verbose:
                for line in task.replay_logs[-10:]:
                    print(f"        日志 {line}")
            task.replay_logs.clear()
    print(f"共 {same + differ} 帧重放：一致 {same}，不一致 {differ}；跳过 {skipped} 帧（闸门等待或没有全屏识别）")


def main():
    parser = argparse.ArgumentParser(description="现场包工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("timeline")
    p.add_argument("scene")
    p.add_argument("--texts", action="store_true", help="每段附上画面文字")
    p.set_defaults(fn=cmd_timeline)
    p = sub.add_parser("draw")
    p.add_argument("scene")
    p.add_argument("frame", type=int)
    p.set_defaults(fn=cmd_draw)
    p = sub.add_parser("html")
    p.add_argument("scene")
    p.set_defaults(fn=cmd_html)
    p = sub.add_parser("replay")
    p.add_argument("scene")
    p.add_argument("-v", "--verbose", action="store_true", help="一致的帧也列出来，附上重放日志和出错堆栈")
    p.set_defaults(fn=cmd_replay)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args.fn(args)


if __name__ == "__main__":
    main()
