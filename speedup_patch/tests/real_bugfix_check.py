"""用作者真实的 handle_boss_selection / handle_rest_sortie / handle_rest 等验证国际服修正。
只模拟读屏幕（OCR 文字、模板匹配、截图），流程代码全部是真实的。每项先跑一遍修正前的原逻辑，确认能复现问题。"""
import os
import sys
import time

WORK = r"D:\Program Files\ok-kes\data\apps\ok-kes\working"
# 仓库里的 utils 等已含源码修正，所以只从仓库取 speedup.py，其余用官方安装目录里的原版
SPEEDUP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "ok_tasks", "speedup.py")
sys.path[:0] = [os.path.join(WORK, "ok_tasks"), WORK]
os.chdir(WORK)

import numpy as np  # noqa: E402
from ok import Box  # noqa: E402
import utils  # noqa: E402
import utils_chaos  # noqa: E402
import utils_sortie  # noqa: E402

W, H = 2560, 1440
FRAME = np.zeros((H, W, 3), dtype=np.uint8)
results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}：{detail}")


def text(name, cx, cy, w=0.06, h=0.03):
    return Box((cx - w / 2) * W, (cy - h / 2) * H, to_x=(cx + w / 2) * W, to_y=(cy + h / 2) * H, name=name)


class Task:
    name = "自动出击模式"

    def __init__(self, texts, rereads=(), features=(), confirm=()):
        self.default_config, self.config_description = {}, {}
        self.config = {"游戏语言": "繁体中文", "生命值大于多少优先闪光(百分比)": "60"}
        self.width, self.height, self.trigger_interval = W, H, 1
        self.all_texts = [text(*t) for t in texts]
        self.rereads = [[text(*t) for t in frame] for frame in rereads]  # 之后每次重新截图识别看到的文字
        self.features, self.confirm = set(features), [text(c, 0.570, 0.669) for c in confirm]
        self.node_status = {"flash_or_rest": True, "shop": False}
        self.clicked, self.logs, self.ocr_calls = [], [], 0
        self.executor = type("Executor", (), {})()
        self.executor.paused, self.executor.current_task = False, None
        self.executor.reset_scene = lambda check_enabled=True: None
        self.executor._speedup_original_next_frame = lambda: FRAME

    def ocr(self, *args, frame=None, **kwargs):
        self.ocr_calls += 1
        return list(self.rereads.pop(0)) if self.rereads else []

    def wait_ocr(self, *args, match=None, time_out=0, **kwargs):
        return [b for b in self.confirm if match.search(b.name)]

    def find_one(self, feature_name=None, box=None, threshold=0, **kwargs):
        if feature_name in self.features:
            found = text(feature_name, 0.83 if "flash" in feature_name else 0.30, 0.50 if "flash" in feature_name else 0.70)
            found.confidence = 0.95
            return found
        return None

    def box_of_screen(self, x, y, to_x=1.0, to_y=1.0, **kwargs):
        return Box(x * W, y * H, to_x=to_x * W, to_y=to_y * H)

    def click_box(self, box=None, *args, **kwargs):
        self.clicked.append(box.name)

    def click(self, x=-1, y=-1, *args, **kwargs):
        self.clicked.append(f"({x:.3f},{y:.3f})")

    def move_relative(self, x, y):
        pass

    move = scroll = mouse_down = mouse_up = send_key = lambda self, *a, **k: None

    def sleep(self, timeout):
        return True

    def find_feature(self, **kwargs):
        return []

    def run(self):
        pass

    def log_info(self, message):
        self.logs.append(message)


BOSS_PAGE = [("请选择在核心遭遇的BOSS。", 0.484, 0.928, 0.25), ("灵魂收割者", 0.358, 0.706, 0.10),
             ("瘟神", 0.641, 0.706, 0.06)]
REST_PAGE = [("闪光", 0.83, 0.50), ("20", 0.83, 0.55), ("免费", 0.25, 0.70), ("300", 0.794, 0.054)]
HP_LOW, HP_HIGH, CREDIT = ("150/1200", 0.209, 0.040), ("1100/1200", 0.209, 0.040), ("300", 0.794, 0.054)
REST_FEATURES = ("flash_in_sortie_safezoom", "rest")


def rest_run(texts, rereads=(), handler=None):
    task = Task(texts, rereads, REST_FEATURES, confirm=["確認"])
    start = time.time()
    (handler or utils_sortie.handle_rest_sortie)(task)
    return task, time.time() - start


# ---------------- 修正前：复现问题 ----------------
original_boss = utils_sortie.handle_boss_selection
task = Task(BOSS_PAGE)
check("修正前：繁中 BOSS 选择页没人处理", original_boss(task) is False and not task.clicked, f"点击 {task.clicked}")
task, _ = rest_run(REST_PAGE, rereads=[REST_PAGE + [HP_LOW]])
check("修正前：读不到生命值时照样闪光", "闪光" in task.clicked and "等待休息确认按钮超时" in task.logs,
      f"点击 {task.clicked}；实际生命值 12%；确认按钮「確認」没等到")
task, _ = rest_run([t for t in REST_PAGE if t[0] != "300"] + [HP_HIGH], rereads=[REST_PAGE + [HP_HIGH]])
check("修正前：读不到信用点时满血也不闪光", "闪光" not in task.clicked, f"点击 {task.clicked}")

# ---------------- 装上补丁 ----------------
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("speedup", SPEEDUP_PY)
speedup = sys.modules["speedup"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(speedup)
speedup.install(Task([]))

task = Task(BOSS_PAGE)
entry = next(h for h in utils_sortie.PAGE_HANDLERS if h.__name__ == "handle_boss_selection")
check("处理函数列表里的 BOSS 选择已替换", entry is not original_boss and entry is utils_sortie.handle_boss_selection,
      f"已替换={entry is not original_boss}")
check("繁中 BOSS 选择页：随机点一个 BOSS", entry(task) is True and task.clicked[0] in ("(0.358,0.706)", "(0.641,0.706)"),
      f"点击 {task.clicked}")
task = Task([("请选择在核心遇见的首领", 0.484, 0.928, 0.25)] + BOSS_PAGE[1:])
check("国服写法仍由原逻辑处理", entry(task) is True and len(task.clicked) == 1, f"点击 {task.clicked}")
check("其他页面不受影响", entry(Task([("随便什么", 0.484, 0.928)])) is False, "返回 False")

task, elapsed = rest_run(REST_PAGE, rereads=[REST_PAGE, REST_PAGE + [HP_LOW]])
check("读不到生命值：重新识别读到 12%，选择休息", "闪光" not in task.clicked and "rest" in task.clicked,
      f"点击 {task.clicked}，重新识别 {task.ocr_calls} 次，耗时 {elapsed:.2f}s")
check("休息后等到繁体「確認」，状态已复位", "等待休息确认按钮超时" not in task.logs
      and task.node_status["flash_or_rest"] is False, f"flash_or_rest={task.node_status['flash_or_rest']}")
task, elapsed = rest_run(REST_PAGE)
check("一直读不到生命值：按 0% 处理，选择休息", "闪光" not in task.clicked and "rest" in task.clicked
      and any("按 0% 处理" in m for m in task.logs), f"点击 {task.clicked}，耗时 {elapsed:.2f}s")
task, elapsed = rest_run(REST_PAGE + [HP_HIGH])
check("生命值读得到时判断不变（92% ≥ 60% 闪光）", task.clicked == ["闪光"] and task.ocr_calls == 0,
      f"点击 {task.clicked}，没有额外识别，耗时 {elapsed:.2f}s")
task, _ = rest_run([t for t in REST_PAGE if t[0] != "300"] + [HP_HIGH], rereads=[REST_PAGE + [HP_HIGH]])
check("读不到信用点：重新识别读到 300，满血闪光", task.clicked == ["闪光"], f"点击 {task.clicked}")

task = Task([])
start = time.time()
value = utils_chaos._get_current_hp_percent(task)
check("休息区以外读不到生命值：照原逻辑立即返回，不重新识别", value is False and task.ocr_calls == 0
      and time.time() - start < 0.05, f"返回 {value}，识别 {task.ocr_calls} 次")
check("卡厄思的休息处理也已替换", utils.handle_rest in utils_chaos.PAGE_HANDLERS
      and getattr(utils.handle_rest, "_speedup_wrapped", False), "utils_chaos.PAGE_HANDLERS 里是包装后的 handle_rest")
flag = speedup._rest_decision_wrapper(lambda t: t._speedup_rest_decision)
task = Task([])
check("休息区判断期间才开启重读", flag(task) is True and task._speedup_rest_decision is False, "调用中 True，调用后 False")

print(f"\n{sum(results)}/{len(results)} 项通过")
sys.exit(0 if all(results) else 1)
