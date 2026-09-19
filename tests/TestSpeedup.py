# 卡厄思模式加速模式（ok_tasks/speedup.py）：时序、并行匹配、路线页等待、牌库翻页与删卡快捷路径测试
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ok_tasks"))

import config_io  # noqa: E402
import speedup  # noqa: E402
import utils  # noqa: E402
import utils_chaos  # noqa: E402
from utils import _simplify_texts  # noqa: E402

OCR_TIME = 0.13
WIDTH, HEIGHT = 2560, 1440
CARD_HELPERS = ("_scroll_card_page", "select_card", "recognize_cards_in_deck", "_get_card_list")

# 合成画面：把每个并行匹配组里的图标埋在已知位置，供真实 cv2.matchTemplate 匹配
_rng = np.random.default_rng(7)
SCENE = _rng.integers(0, 255, (1300, 2200, 3), dtype=np.uint8)
TEMPLATES = {}
for _index, _name in enumerate(speedup._GROUP_OF):
    _template = _rng.integers(0, 255, (60, 60, 3), dtype=np.uint8)
    TEMPLATES[_name] = _template
    _y, _x = 40 + (_index // 12) * 150, 40 + (_index % 12) * 170
    SCENE[_y:_y + 60, _x:_x + 60] = _template
ROUTE_BASE = _rng.integers(0, 255, (700, 900, 3), dtype=np.uint8)
ROUTE_FINAL = {n: (60 + (i % 3) * 250, 60 + (i // 3) * 200) for i, n in enumerate(speedup._PARALLEL_GROUPS[0])}


class Box:
    def __init__(self, name, x, y, width=160, height=48):
        self.name, self.x, self.y, self.width, self.height = name, x, y, width, height


class TaskDisabled(Exception):
    pass


class Frame(list):
    """一帧画面：list 部分是 OCR 文字框，image 是模板匹配用的像素。"""
    image = SCENE


PAGES = {
    "A": [Box("标题", 1100, 150), Box("说明文字", 1000, 600), Box("确认", 1800, 1250)],
    "B": [Box("奖励", 1100, 150), Box("继续进行", 2200, 1300), Box("卡牌", 600, 700)],
    "SHOP": [Box("商店", 1100, 150), Box("免费", 1400, 1300), Box("货币", 2200, 60)],
    "MULTI": [Box("打开", 1200, 700), Box("菜单", 200, 100)],
    "ROUTE": [Box("路线", 300, 100), Box("第3层", 2300, 100)],
    "LONG": [Box("长按卡牌", 1200, 700)],
    "BATTLE": [Box("5/10", 1300, 1390)],
    "MATCH": [Box("匹配测试", 300, 100)],
    "ROUTE_SETTLE": [Box("路线稳定测试", 300, 100)],
    "REMOVE": [Box("请选择1张要移除的卡牌", 300, 60)],
    "EMPTY": [],
}
FADING = [Box("标题", 1100, 150)]  # 过渡中：按钮已消失，只剩残影文字
DECK = ["声音测试", "安可", "聚光灯", "音乐开始", "暗黑之刃", "物质再生", "黑暗斩击", "共鸣之暗",
        "掠食者之刃", "点心时间", "喘气", "安可", "聚光灯", "声音测试", "音乐开始", "黑暗斩击"]  # 4 行


class Game:
    """模拟游戏：点击后延迟 delay 秒开始过渡，duration 秒后切到目标页面；另含可滚动的牌库和可变的画面。"""

    def __init__(self, page, rules=None):
        self.page, self.rules, self.transition, self.clicks = page, rules or {}, None, []
        self.deck, self.deck_offset, self.scrolls, self.deck_views = [], 0, [], 0
        self.image_fn = None

    def boxes_at(self, now):
        if self.transition:
            start, end, target, fading = self.transition
            if now >= end:
                self.page, self.transition = target, None
            elif now >= start:
                return fading
        return PAGES[self.page]

    def image_at(self, now):
        return self.image_fn(now) if self.image_fn else SCENE

    def on_click(self, now):
        self.clicks.append((now, self.page))
        rule = self.rules.get(self.page)
        if rule and self.transition is None:
            delay, duration, target, fading = rule
            self.transition = (now + delay, now + delay + duration, target, fading)

    # 牌库：每行 4 张，一屏 2 行；一次 3 格滚动移动半行（与实测一致）
    def _rows(self):
        return [self.deck[i:i + 4] for i in range(0, len(self.deck), 4)]

    def visible_deck(self):
        self.deck_views += 1
        top = self.deck_offset // 2
        return [card for row in self._rows()[top:top + 2] for card in row]

    def deck_at_bottom(self):
        return self.deck_offset // 2 + 2 >= len(self._rows())

    def on_scroll(self, count):
        self.scrolls.append(count)
        if count < 0:
            self.deck_offset = min(self.deck_offset + 1, max(0, 2 * (len(self._rows()) - 2)))


class FakeExecutor:
    """与 ok TaskExecutor 中 sleep/next_frame/frame/reset_scene 的时序一致。"""

    def __init__(self, game):
        self.game, self._frame, self.paused, self.current_task = game, None, False, None

    def check_enabled(self):
        if self.current_task is not None and self.current_task.disabled:
            raise TaskDisabled()

    def reset_scene(self, check_enabled=True):
        if check_enabled:
            self.check_enabled()
        self._frame = None

    def next_frame(self, time_out=6):
        self.reset_scene()
        now = time.time()
        self._frame = Frame(self.game.boxes_at(now))
        self._frame.image = self.game.image_at(now)
        return self._frame

    @property
    def frame(self):
        if self._frame is None:
            self.next_frame()
        return self._frame

    def sleep(self, timeout):
        self.reset_scene(check_enabled=False)
        end = time.time() + timeout
        while True:
            self.check_enabled()
            remaining = end - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.005))


class FakeChaosTask:
    """点击、睡眠、移动、匹配等方法照抄 ok BaseTask 的调用关系（click_box 默认 after_sleep=1）。"""

    def __init__(self, game, enabled=True):
        self._executor = FakeExecutor(game)
        self.default_config, self.config_description = {}, {}
        self.config = {speedup.ENABLE_KEY: enabled, speedup.HOVER_KEY: 0.1, speedup.INTERVAL_KEY: 0.3,
                       speedup.REMOVAL_KEY: True, "优先移除基础牌": False, "刷空档": False,
                       "移除卡牌列表": ["粉丝福利", "拍照时间"],
                       "任务优先级": ["复制", "移除", "闪光1次", "信用点增加"]}
        self.member_status = {"deck": {}}
        self.trigger_interval = 1
        self.all_texts, self.logs, self.marks, self.match_log = [], [], {}, []
        self.disabled = False
        self.width, self.height = WIDTH, HEIGHT

    @property
    def executor(self):
        return self._executor

    @property
    def frame(self):
        return self.executor.frame

    def sleep(self, timeout):
        self.executor.sleep(timeout)
        return True

    def click(self, x=-1, y=-1, move_back=False, name=None, interval=-1, move=True,
              down_time=0.02, after_sleep=0, key='left', hcenter=False, vcenter=False):
        if isinstance(x, Box) or isinstance(x, list):
            return self.click_box(x, move_back=move_back, move=move, down_time=down_time, after_sleep=after_sleep)
        elif 0 < x < 1 or 0 < y < 1:
            return self.click_relative(x, y, after_sleep=after_sleep, name=name)
        self.executor.game.on_click(time.time())
        if after_sleep > 0:
            self.sleep(after_sleep)
        self.executor.reset_scene()
        return True

    def click_relative(self, x, y, after_sleep=0, name=None, **kwargs):
        self.click(int(self.width * x), int(self.height * y), name=name, after_sleep=after_sleep)

    def click_box(self, box=None, relative_x=0.5, relative_y=0.5, raise_if_not_found=False,
                  move_back=False, move=True, down_time=0.01, after_sleep=1):
        if isinstance(box, list):
            box = box[0]
        x, y = int(box.x + box.width * relative_x), int(box.y + box.height * relative_y)
        return self.click(x, y, name=box.name, after_sleep=after_sleep)

    def move_relative(self, x, y):
        self.move(int(self.width * x), int(self.height * y))

    def move(self, x, y):
        self.marks["move"] = time.time()
        self.executor.reset_scene()

    def scroll_relative(self, x, y, count):
        self.scroll(int(self.width * x), int(self.height * y), count)

    def scroll(self, x, y, count):
        self.executor.game.on_scroll(count)
        self.executor.reset_scene()

    def is_adb(self):
        return False

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        self.marks["down"] = time.time()
        self.executor.reset_scene()

    def mouse_up(self, name=None, key="left"):
        self.marks["up"] = time.time()

    def send_key(self, key, down_time=0.02, interval=-1, after_sleep=0):
        self.executor.reset_scene()

    def ocr(self):
        frame = self.executor.frame
        time.sleep(OCR_TIME)
        return [Box(b.name, b.x, b.y, b.width, b.height) for b in frame]

    def find_feature(self, feature_name=None, horizontal_variance=0, vertical_variance=0, threshold=0,
                     box=None, frame=None, limit=0):
        # 与 ok 框架相同：没传 frame 就取 executor.frame；匹配用真实 cv2.matchTemplate
        image = frame if frame is not None else self.executor.frame
        self.match_log.append((feature_name, threading.current_thread().name, id(image), time.time()))
        area = image.image
        if box is not None:
            area = area[box.y:box.y + box.height, box.x:box.x + box.width]
        result = cv2.matchTemplate(area, TEMPLATES[feature_name], cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= (threshold or 0.8))
        return [Box(feature_name, int(x), int(y), 60, 60) for x, y in zip(xs, ys)]

    def log_info(self, message):
        self.logs.append(message)

    def info_set(self, key, value):
        self.logs.append(f"{key}={value}")

    def _check_upload_if_needed(self):
        pass

    def run(self):
        # 与 ChaosMode.run() 结构一致，供 speedup 的兼容性检查使用
        self.all_texts = _simplify_texts(self.ocr())
        for handle_page in utils_chaos.PAGE_HANDLERS:
            if handle_page(self):
                return
        self._check_upload_if_needed()


def run_executor(task, seconds, stop=None):
    """仿 TaskExecutor.execute：距上次触发超过 trigger_interval 才触发，触发前先取帧。"""
    executor, last, end = task.executor, 0.0, time.time() + seconds
    while time.time() < end and not (stop and stop()):
        now = time.time()
        if now - last > task.trigger_interval:
            last = now
            executor.current_task = task
            try:
                executor.next_frame()
                task.run()
            except TaskDisabled:
                task.marks["stopped"] = time.time()
                task.disabled = False
            executor.current_task = None
        else:
            time.sleep(0.002)


def find(task, name):
    return next((b for b in task.all_texts if b.name == name), None)


def handle_center_confirm(task):
    box = find(task, "确认")
    if box:
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_page_b(task):
    if find(task, "继续进行"):
        task.marks.setdefault("reached_b", time.time())
        return True
    return False


def handle_free(task):
    box = find(task, "免费")
    if box:
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_multi_step(task):
    box = find(task, "打开")
    if box:
        task.click_box(box, after_sleep=0)
        task.marks["click1"] = time.time()
        task.sleep(0.5)
        _ = task.frame
        task.marks["frame_after_sleep"] = time.time()
        task.click(0.5, 0.5)
        task.sleep(1)
        return True
    return False


def handle_route_selection(task):
    if find(task, "路线"):
        task.marks["settle_start"] = time.time()
        task.sleep(1)
        _ = task.frame
        task.marks["settle_end"] = time.time()
        task.move_relative(0.6, 0.5)
        task.sleep(0.5)
        task.click(0.6, 0.5)
        task.marks["route_click"] = time.time()
        task.sleep(2)
        return True
    return False


def handle_long_press(task):
    if find(task, "长按卡牌"):
        task.move_relative(0.5, 0.5)
        task.mouse_down(1280, 720)
        task.sleep(2)
        task.mouse_up()
        task.sleep(1)
        _ = task.frame
        task.marks["frame_after_up"] = time.time()
        return True
    return False


def handle_battle_auto_check(task):
    return bool(find(task, "5/10"))


MATCH_BOX = Box("区域", 0, 0, 2200, 1300)


def handle_matching(task):
    """按 ok-kes 的写法依次匹配：牌库/选卡页卡牌类型、路线页 9 种、小地图两组（阈值不同）。"""
    if not find(task, "匹配测试"):
        return False
    task.marks["sleep_call"] = time.time()
    task.sleep(1)
    cards = {n: task.find_feature(feature_name=n, box=MATCH_BOX, threshold=0.65)
             for n in speedup._PARALLEL_GROUPS[3] + speedup._PARALLEL_GROUPS[4]}
    route = {n: task.find_feature(feature_name=n, box=MATCH_BOX) for n in speedup._PARALLEL_GROUPS[0]}
    task.marks["route_end"] = time.time()
    minimap = {n: task.find_feature(feature_name=n, box=MATCH_BOX, threshold=0.85)
               for n in speedup._PARALLEL_GROUPS[1]}
    minimap.update({n: task.find_feature(feature_name=n, box=MATCH_BOX, threshold=0.65)
                    for n in speedup._PARALLEL_GROUPS[2]})
    task.find_feature(feature_name="safezone", box=MATCH_BOX, limit=1)  # 带额外参数：应走原逻辑
    task.marks["results"] = {k: [(b.x, b.y) for b in v] for k, v in {**cards, **route, **minimap}.items()}
    return True


def route_image_fn(anim_end, icons=True):
    """anim_end 之前路线图标横向来回移动，之后停在最终位置；icons=False 模拟没有普通节点图标的 Boss 节点。"""
    def image_at(now):
        image = ROUTE_BASE.copy()
        if icons:
            shift = int(((anim_end - now) * 400) % 200) if now < anim_end else 0
            for name, (x, y) in ROUTE_FINAL.items():
                image[y:y + 60, x + shift:x + shift + 60] = TEMPLATES[name]
        return image
    return image_at


def handle_route_settle(task):
    """仿 handle_route_selection：先 sleep(1) 等图标入场，再逐个匹配 9 种节点/标记。"""
    if not find(task, "路线稳定测试"):
        return False
    task.marks["settle_call"] = time.time()
    task.sleep(1)
    results = {n: task.find_feature(feature_name=n, box=None) for n in speedup._PARALLEL_GROUPS[0]}
    task.marks["settle_done"] = time.time()
    task.marks["positions"] = {n: sorted((b.x, b.y) for b in v) for n, v in results.items()}
    return True


# ---------------- 删卡流程替身：外部行为与 utils 中同名函数一致，内部经 utils.xxx 调用以经过加速包装 ----------------
class SkipButton:
    name, x, y, width, height = "跳过", 2300, 1350, 120, 48


def fake_scroll_card_page(task, x, y, amount, page, distance=0.25):
    task.move_relative(x, y)
    task.sleep(0.05)
    task.scroll_relative(x, y, amount)
    task.sleep(0.5)


def fake_recognize_cards_in_deck(task, region=(0.274, 0.108, 0.929, 0.874), page=""):
    _ = task.frame
    return [{"name": name} for name in task.executor.game.visible_deck()]


def fake_select_card(task, card_names, count=1, action=""):
    """逐页找目标 → 到底后删最底页咒术卡 → 仍不够就点“跳过”。"""
    page = f"select_card-{action}"
    selected = 0
    cards = utils.recognize_cards_in_deck(task, page=page)
    while True:
        for card in cards:
            if selected < count and any(t in card["name"] or card["name"] in t for t in card_names):
                task.click(0.5, 0.5)
                selected += 1
        if selected >= count:
            return True
        if task.executor.game.deck_at_bottom():
            break
        utils._scroll_card_page(task, 0.251, 0.735, -3, page)
        cards = utils.recognize_cards_in_deck(task, page=page)
    if action == "移除":
        for card in cards:
            if selected < count and card["name"].startswith("咒术"):
                task.click(0.5, 0.5)
                selected += 1
        if selected >= count:
            return True
    task.click_box(SkipButton())
    return True


def fake_get_card_list(task, key):
    value = task.config.get(key, [])
    return list(value) if isinstance(value, (list, tuple)) else []


def handle_remove(task):
    """仿 handle_select_card：每次进入删卡页，牌库从顶部开始显示。"""
    if not find(task, "请选择1张要移除的卡牌"):
        return False
    game = task.executor.game
    game.deck_offset, views, scrolls = 0, game.deck_views, len(game.scrolls)
    utils.select_card(task, utils._get_card_list(task, "移除卡牌列表"), count=1, action="移除")
    task.marks.setdefault("flows", []).append((game.deck_views - views, len(game.scrolls) - scrolls))
    return True


class ScrollRecorder:
    """真实 utils._scroll_card_page 会调用的任务接口。"""

    def __init__(self, active, jump=False):
        self._speedup = {"active": active, "jump_scroll": jump}
        self.scrolls = 0

    def is_adb(self):
        return False

    def log_info(self, message):
        pass

    def move_relative(self, x, y):
        pass

    def sleep(self, timeout):
        pass

    def scroll_relative(self, x, y, count):
        self.scrolls += 1


class TestSpeedup(unittest.TestCase):

    def setUp(self):
        self._handlers = list(utils_chaos.PAGE_HANDLERS)
        self._is_frame_stuck = utils.is_frame_stuck
        self._ui_only_keys = set(config_io.UI_ONLY_CONFIG_KEYS)
        self._card_helpers = {name: getattr(utils, name) for name in CARD_HELPERS}

    def tearDown(self):
        utils_chaos.PAGE_HANDLERS[:] = self._handlers
        utils.is_frame_stuck = self._is_frame_stuck
        config_io.UI_ONLY_CONFIG_KEYS.clear()
        config_io.UI_ONLY_CONFIG_KEYS.update(self._ui_only_keys)
        for name, function in self._card_helpers.items():
            setattr(utils, name, function)

    def make(self, page, rules, handlers, enabled=True):
        game = Game(page, rules)
        task = FakeChaosTask(game, enabled)
        speedup.install(task)
        utils_chaos.PAGE_HANDLERS[:] = handlers
        return game, task

    def use_fake_card_helpers(self):
        utils._scroll_card_page = fake_scroll_card_page
        utils.select_card = fake_select_card
        utils.recognize_cards_in_deck = fake_recognize_cards_in_deck
        utils._get_card_list = fake_get_card_list
        speedup._patch_card_helpers()

    # ---------------- 点击后等待 ----------------
    def transition_latency(self, enabled):
        game, task = self.make("A", {"A": (0.1, 0.5, "B", FADING)}, [handle_center_confirm, handle_page_b], enabled)
        run_executor(task, 4, stop=lambda: "reached_b" in task.marks)
        confirm_clicks = sum(1 for _, page in game.clicks if page == "A")
        return task.marks["reached_b"] - game.clicks[0][0], confirm_clicks

    def test_disabled_keeps_original_timing(self):
        latency, clicks = self.transition_latency(enabled=False)
        self.assertGreaterEqual(latency, 2.0)
        self.assertEqual(1, clicks)

    def test_enabled_continues_after_page_responds_without_double_click(self):
        latency, clicks = self.transition_latency(enabled=True)
        self.assertLess(latency, 1.3)
        self.assertEqual(1, clicks)

    def test_unresponsive_page_is_retried_at_original_interval(self):
        game, task = self.make("SHOP", {}, [handle_free])
        run_executor(task, 5.2)
        times = [t for t, _ in game.clicks]
        self.assertGreaterEqual(len(times), 2)
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 2.0)

    def test_multi_step_handler_keeps_intermediate_and_tail_waits(self):
        game, task = self.make("MULTI", {}, [handle_multi_step])
        run_executor(task, 2.5, stop=lambda: len(game.clicks) >= 3)
        self.assertGreaterEqual(task.marks["frame_after_sleep"] - task.marks["click1"], 0.5)
        self.assertGreaterEqual(game.clicks[2][0] - game.clicks[1][0], 1.0)

    def test_settle_wait_kept_and_hover_shortened(self):
        game, task = self.make("ROUTE", {"ROUTE": (0.05, 0.3, "EMPTY", [])}, [handle_route_selection])
        run_executor(task, 3, stop=lambda: "route_click" in task.marks)
        self.assertGreaterEqual(task.marks["settle_end"] - task.marks["settle_start"], 1.0)
        hover = task.marks["route_click"] - task.marks["move"]
        self.assertGreaterEqual(hover, 0.09)
        self.assertLess(hover, 0.3)

    def test_long_press_duration_kept(self):
        game, task = self.make("LONG", {}, [handle_long_press])
        run_executor(task, 3.6, stop=lambda: "frame_after_up" in task.marks)
        self.assertGreaterEqual(task.marks["up"] - task.marks["down"], 2.0)
        self.assertGreaterEqual(task.marks["frame_after_up"] - task.marks["up"], 1.0)

    def test_trigger_interval_in_battle_and_navigation(self):
        game, task = self.make("BATTLE", {}, [handle_battle_auto_check])
        run_executor(task, 0.5)
        self.assertEqual(1.0, task.trigger_interval)
        utils_chaos.PAGE_HANDLERS[:] = [handle_page_b, handle_battle_auto_check]
        game.page = "B"
        run_executor(task, 1.2)
        self.assertEqual(0.3, task.trigger_interval)

    def test_stop_during_wait_resets_state(self):
        game, task = self.make("A", {"A": (5, 1, "B", FADING)}, [handle_center_confirm, handle_page_b])
        timer = threading.Timer(0.6, lambda: setattr(task, "disabled", True))
        timer.start()
        run_executor(task, 1.5)
        timer.join()
        self.assertIn("stopped", task.marks)
        self.assertEqual(0.0, task._speedup["owed_until"])
        self.assertFalse(task._speedup["in_run"])

    def test_stuck_detection_still_samples_about_once_per_second(self):
        calls = []

        def counting_is_frame_stuck(task, stuck_threshold_seconds=30, change_threshold=0.08):
            calls.append(time.time())
            task._last_change_time = time.time()
            return False

        utils.is_frame_stuck = counting_is_frame_stuck
        speedup._keep_stuck_detection_at_one_second()
        task = SimpleNamespace()
        end = time.time() + 2.05
        while time.time() < end:
            utils.is_frame_stuck(task, stuck_threshold_seconds=10)
            time.sleep(0.1)
        self.assertLessEqual(len(calls), 4)
        self.assertGreaterEqual(len(calls), 2)

    def test_speed_options_excluded_from_exported_config(self):
        self.make("EMPTY", {}, [])
        for key in (speedup.ENABLE_KEY, speedup.HOVER_KEY, speedup.INTERVAL_KEY, speedup.REMOVAL_KEY):
            self.assertIn(key, config_io.UI_ONLY_CONFIG_KEYS)

    # ---------------- 并行模板匹配 ----------------
    def matching_run(self, enabled):
        game, task = self.make("MATCH", {}, [handle_matching], enabled)
        run_executor(task, 4, stop=lambda: "results" in task.marks)
        return task

    def test_parallel_matching_matches_sequential_results(self):
        sequential, parallel = self.matching_run(False), self.matching_run(True)
        self.assertEqual(sequential.marks["results"], parallel.marks["results"])
        self.assertEqual(29, sum(1 for boxes in parallel.marks["results"].values() if boxes))
        workers = [c for c in parallel.match_log if c[1].startswith("speedup-match")]
        on_caller = [c for c in parallel.match_log if not c[1].startswith("speedup-match")]
        self.assertEqual(29, len(workers))   # 每个图标只算一次
        self.assertEqual(1, len(on_caller))  # 带 limit 参数的调用走原逻辑
        self.assertEqual(1, len({c[2] for c in parallel.match_log}))  # 同一帧
        self.assertGreaterEqual(min(c[3] for c in parallel.match_log) - parallel.marks["sleep_call"], 1.0)
        self.assertTrue(all(not c[1].startswith("speedup-match") for c in sequential.match_log))

    # ---------------- 路线页识别前的等待 ----------------
    def route_wait(self, anim_seconds, enabled=True, icons=True):
        game, task = self.make("ROUTE_SETTLE", {}, [handle_route_settle], enabled)
        game.image_fn = route_image_fn(time.time() + anim_seconds, icons)
        run_executor(task, 3, stop=lambda: "positions" in task.marks)
        final = {n: [p] for n, p in ROUTE_FINAL.items()}
        return task.marks["settle_done"] - task.marks["settle_call"], task.marks["positions"] == final

    def test_route_icons_settled_early_continue_before_one_second(self):
        wait, at_final_position = self.route_wait(0.2)
        self.assertGreaterEqual(wait, 0.4)
        self.assertLess(wait, 0.85)
        self.assertTrue(at_final_position)

    def test_route_icons_still_moving_wait_full_second(self):
        wait, _ = self.route_wait(2.0)
        self.assertGreaterEqual(wait, 0.95)

    def test_route_boss_node_without_icons_waits_full_second(self):
        wait, _ = self.route_wait(0.0, icons=False)
        self.assertGreaterEqual(wait, 0.95)

    def test_route_disabled_keeps_fixed_wait(self):
        wait, at_final_position = self.route_wait(0.2, enabled=False)
        self.assertGreaterEqual(wait, 0.95)
        self.assertTrue(at_final_position)

    # ---------------- 牌库翻页 ----------------
    def test_deck_scroll_sends_two_messages_only_on_deck_pages(self):
        speedup._patch_card_helpers()
        cases = (
            (ScrollRecorder(True), "select_card-移除", 2),
            (ScrollRecorder(True), "移除卡牌目标主战员查找", 1),
            (ScrollRecorder(False), "select_card-移除", 1),
            (ScrollRecorder(True, jump=True), "select_card-移除", speedup._JUMP_SCROLLS),
        )
        for recorder, page, expected in cases:
            utils._scroll_card_page(recorder, 0.251, 0.735, -3, page)
            self.assertEqual(expected, recorder.scrolls, page)

    # ---------------- 删卡快捷路径 ----------------
    def removal_flows(self, deck, flows, enabled=True, **config):
        self.use_fake_card_helpers()
        game, task = self.make("REMOVE", {}, [handle_remove], enabled)
        task.config.update(config)
        game.deck = list(deck)
        run_executor(task, 20, stop=lambda: len(task.marks.get("flows", [])) >= flows)
        return game, task

    def test_full_scan_without_targets_marks_round_and_later_scans_are_short(self):
        _, original = self.removal_flows(DECK, 1, enabled=False)
        self.assertEqual((5, 4), original.marks["flows"][0])  # 原逻辑：每次半行，识别 5 页
        game, task = self.removal_flows(DECK, 3)
        (views1, scrolls1), (views2, _), (views3, _) = task.marks["flows"][:3]
        self.assertEqual((3, 4), (views1, scrolls1))  # 每次一整行：识别 3 页，滚动总量不变
        self.assertIs(task.member_status, task._speedup["removal_exhausted"])
        self.assertEqual((2, 2), (views2, views3))  # 本局之后只看首页和最底页

    def test_task_priority_moves_removal_last_until_new_round(self):
        game, task = self.removal_flows(DECK, 1)
        task._speedup["active"] = True  # 模拟处于运行中
        self.assertEqual(["复制", "闪光1次", "信用点增加", "移除"], utils._get_card_list(task, "任务优先级"))
        task.member_status = {"deck": {}}  # 新的一局
        self.assertEqual(["复制", "移除", "闪光1次", "信用点增加"], utils._get_card_list(task, "任务优先级"))
        self.assertIsNone(task._speedup["removal_exhausted"])

    def test_deck_with_target_is_removed_and_not_marked(self):
        game, task = self.removal_flows(DECK[:12] + ["粉丝福利"] + DECK[13:], 1)
        self.assertTrue(any(page == "REMOVE" for _, page in game.clicks))
        self.assertIsNone(task._speedup["removal_exhausted"])

    def test_shortcut_still_removes_bottom_curse(self):
        game, task = self.removal_flows(DECK, 1)
        game.deck = DECK[:15] + ["咒术：诅咒"]  # 本局后来染上咒术卡，排在最底
        clicks_before = len(game.clicks)
        run_executor(task, 20, stop=lambda: len(task.marks["flows"]) >= 2)
        self.assertEqual(2, task.marks["flows"][1][0])
        self.assertEqual(1, len(game.clicks) - clicks_before)  # 点了咒术卡，没有点“跳过”

    def test_shortcut_disabled_when_removing_base_cards(self):
        game, task = self.removal_flows(DECK, 2, **{"优先移除基础牌": True})
        self.assertIsNone(task._speedup["removal_exhausted"])
        self.assertEqual(3, task.marks["flows"][1][0])

    # ---------------- 与真实 ChaosMode 的兼容性 ----------------
    def test_real_chaos_mode_run_matches_gated_run(self):
        import ChaosMode
        from src.config import config

        task = ChaosMode.ChaosMode(executor=SimpleNamespace(scene=None, config=config), app=None)
        self.assertTrue(task._speedup["gate_ok"], "ChaosMode.run() 已改动，请同步 speedup._gated_run")
        self.assertFalse(task.default_config[speedup.ENABLE_KEY])


if __name__ == '__main__':
    unittest.main()
