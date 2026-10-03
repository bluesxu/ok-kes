"""按 ok-script 框架源码仿写执行器/任务，离线验证 speedup 补丁的时序与安全性。"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ok_tasks"))
sys.path.insert(0, HERE)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import utils  # noqa: E402  测试替身
import utils_chaos  # noqa: E402
import utils_sortie  # noqa: E402
# 与真实模块一致：页面处理函数列表在加载时就存好了原函数对象，补丁装上之后才替换
ORIGINAL_BATTLE_PAGE = utils_sortie.handle_battle_page
from utils import _simplify_texts  # noqa: E402
import speedup  # noqa: E402

OCR_TIME = 0.13
W, H = 2560, 1440

# 合成一张大图，把各组图标埋在已知位置，供真实 cv2.matchTemplate 匹配
_rng = np.random.default_rng(7)
SCENE = _rng.integers(0, 255, (1300, 2200, 3), dtype=np.uint8)
TEMPLATES = {}
for _i, _name in enumerate(speedup._GROUP_OF):
    _t = _rng.integers(0, 255, (60, 60, 3), dtype=np.uint8)
    TEMPLATES[_name] = _t
    _y, _x = 40 + (_i // 12) * 150, 40 + (_i % 12) * 170
    SCENE[_y:_y + 60, _x:_x + 60] = _t


class Frame(np.ndarray):
    """模拟一帧：与 ok 框架一样本身是像素数组，boxes 是这帧上的 OCR 文字框。"""


class Box:
    def __init__(self, name, x, y, w=160, h=48):
        self.name, self.x, self.y, self.width, self.height = name, x, y, w, h

    def area(self):
        return self.width * self.height


class TaskDisabled(Exception):
    pass


PAGES = {
    "A": [Box("标题A", 1100, 150), Box("说明文字", 1000, 600), Box("确认", 1800, 1250)],
    "B": [Box("奖励", 1100, 150), Box("继续进行", 2200, 1300), Box("卡牌", 600, 700)],
    "SHOP": [Box("商店", 1100, 150), Box("免费", 1400, 1300), Box("货币 300", 2200, 60)],
    "MULTI": [Box("打开", 1200, 700), Box("菜单", 200, 100)],
    "ROUTE": [Box("路线", 300, 100), Box("第3层", 2300, 100)],
    "LONG": [Box("长按卡牌", 1200, 700)],
    "BATTLE": [Box("5/10", 1300, 1390)],
    "EMPTY": [],
}
MID = [Box("标题A", 1100, 150)]  # 过渡中：按钮已消失，只剩残影文字


class World:
    """被模拟的游戏：点击后按规则延迟 delay 秒开始过渡，持续 dur 秒后切到目标页面。"""

    def __init__(self, page, rules=None):
        self.page, self.rules, self.transition = page, rules or {}, None
        self.clicks = []
        self.deck, self.deck_offset, self.scrolls, self.deck_views = [], 0, [], 0
        self.image_fn, self.deck_anim, self.scroll_times, self.recognize_log, self.seen = None, None, [], [], []
        self.click_fn = None  # 个别页面自定义的点击响应
        self.selected_cards = set()  # 已选中（金色边框）的牌库序号
        # 战斗出牌：按数字键后 CARD_RAISE 秒卡牌上滑到位，回车后 CARD_PLAY 秒手牌数减少
        self.hand, self.keys, self.raise_at, self.play_at = 0, [], None, None
        self.play_fails = False  # True 模拟这张牌没打出去（手牌数不减少）
        self.popup_on_enter, self.popup_at = False, None  # 出牌后弹出“请选择功能”，盖住手牌数
        self.raise_delay = CARD_RAISE  # 网络卡顿时游戏处理按键会慢一些
        self.cards_visible_at = 0.0    # 手牌在这之后才出现在画面上（新抽的牌还没到手）
        self.end_turn_button = False   # 画面上有「结束回合」按钮
        self.hand_anim_until = 0.0     # 手牌区在这之前一直在动（上一张牌的动画）
        self.flicker = None            # (点击后多久, 持续多久)：被点的文字这段时间 OCR 识别不到

    # 牌库：每行 4 张，以半行为单位滚动（一次 3 格）；视野里 4 个半行位置，0~2 能正常识别，
    # 3（最下面一行）描述在区域外，只有允许“只凭卡名保留”时才识别得到（与日志实测一致）
    def _rows(self):
        return [self.deck[i:i + 4] for i in range(0, len(self.deck), 4)]

    def _max_offset(self):
        return max(0, 2 * len(self._rows()) - 4)

    def visible_deck(self, include_bottom=False):
        """返回视野里的 (牌库序号, 卡名)。"""
        self.deck_views += 1
        seen, visible = [], []
        for index, row in enumerate(self._rows()):
            slot = 2 * index - self.deck_offset
            if 0 <= slot <= 2 or (slot == 3 and include_bottom):
                seen += [(index, card) for card in row]
                visible += [(index * 4 + i, card) for i, card in enumerate(row)]
        self.seen.append(seen)
        return visible

    def deck_at_bottom(self):
        return self.deck_offset >= self._max_offset()

    def on_scroll(self, count):
        self.scrolls.append(count)
        self.scroll_times.append(time.time())
        if count < 0:
            target = min(self.deck_offset + 1, self._max_offset())
            if target != self.deck_offset:
                self.deck_anim = (time.time(), self.deck_offset, target)
            self.deck_offset = target

    def image_at(self, now):
        return self.image_fn(now) if self.image_fn else SCENE

    def on_key(self, key):
        now = time.time()
        self.keys.append((now, key))
        if key.isdigit():
            self.raise_at = now + self.raise_delay
        elif key == "enter" and self.hand and self.popup_on_enter:
            self.popup_at = self.popup_at or now + CARD_PLAY
        elif key == "enter" and self.hand and not self.play_fails:
            self.play_at = now + CARD_PLAY

    def boxes_at(self, t):
        if self.page == "BATTLE_PLAY":
            if self.popup_at and t >= self.popup_at:
                return [Box("请选择功能", 1150, 180)]
            if self.play_at and t >= self.play_at:
                self.hand -= 1
                self.play_at, self.raise_at = None, None
            boxes = [Box(f"{self.hand}/10", int(0.515 * W) - 60, int(0.972 * H) - 24, 120, 48)]
            if self.hand > 0 and t >= self.cards_visible_at:
                boxes.append(Box("手牌", 1200, 1100))
            if self.end_turn_button:
                boxes.append(Box("结束回合", 2300, 1250))
            return boxes
        if self.flicker and self.clicks:
            delay, duration = self.flicker
            clicked_at = self.clicks[-1][0]
            if clicked_at + delay <= t < clicked_at + delay + duration:
                return [b for b in PAGES[self.page] if b.name != "免费"]  # 被点的按钮这一帧没识别到
        if self.transition:
            start, end, target, mid = self.transition
            if t >= end:
                self.page, self.transition = target, None
            elif t >= start:
                return mid
        return PAGES[self.page]

    def on_click(self, t, x, y):
        self.clicks.append((t, self.page, x, y))
        if self.click_fn:
            self.click_fn(x, y)
        rule = self.rules.get(self.page)
        if rule and self.transition is None:
            delay, dur, target, mid = rule
            self.transition = (t + delay, t + delay + dur, target, mid)


class FakeExecutor:
    def __init__(self, world):
        self.world, self._frame, self.paused, self.current_task = world, None, False, None

    def check_enabled(self):
        if self.current_task is not None and self.current_task.disabled_flag:
            raise TaskDisabled()

    def reset_scene(self, check_enabled=True):
        if check_enabled:
            self.check_enabled()
        self._frame = None

    def next_frame(self, time_out=6):
        self.reset_scene()
        now = time.time()
        self._frame = np.asarray(self.world.image_at(now)).view(Frame)
        self._frame.boxes = list(self.world.boxes_at(now))
        self.captures = getattr(self, "captures", 0) + 1
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


class FakeTask:
    """方法实现照抄 ok/task/task.py 中与时序相关的部分。"""

    def __init__(self, executor, enabled=True):
        self._executor = executor
        self.default_config, self.config_description = {}, {}
        self.config = {"加速模式": enabled, "点击前悬停等待(秒)": 0.1, "非战斗检测间隔(秒)": 0.3,
                       "优先移除基础牌": False, "刷空档": False,
                       "移除卡牌列表": ["粉丝福利", "拍照时间"],
                       "任务优先级": ["复制", "移除", "闪光1次", "信用点增加"]}
        self.member_status = {"deck": {}}
        self.trigger_interval = 1
        self.all_texts, self.logs, self.marks = [], [], {}
        self.match_log = []
        self.disabled_flag = False
        self.width, self.height = W, H

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
            return self.click_relative(x, y, move_back=move_back, move=move, after_sleep=after_sleep, name=name)
        self.executor.world.on_click(time.time(), x, y)
        if after_sleep > 0:
            self.sleep(after_sleep)
        self.executor.reset_scene()
        return True

    def click_relative(self, x, y, move_back=False, hcenter=False, vcenter=False, move=True, after_sleep=0,
                       name=None, interval=-1, down_time=0.02, key="left"):
        self.click(int(self.width * x), int(self.height * y), move_back, name=name, move=move,
                   down_time=down_time, after_sleep=after_sleep, interval=interval, key=key)

    def click_box(self, box=None, relative_x=0.5, relative_y=0.5, raise_if_not_found=False,
                  move_back=False, move=True, down_time=0.01, after_sleep=1):
        if isinstance(box, list):
            box = box[0]
        x, y = int(box.x + box.width * relative_x), int(box.y + box.height * relative_y)
        return self.click(x, y, name=box.name, move_back=move_back, move=move, down_time=down_time,
                          after_sleep=after_sleep)

    def move_relative(self, x, y):
        self.move(int(self.width * x), int(self.height * y))

    def move(self, x, y):
        self.marks["move"] = time.time()
        self.executor.reset_scene()

    def scroll_relative(self, x, y, count):
        self.scroll(int(self.width * x), int(self.height * y), count)

    def scroll(self, x, y, count):
        self.executor.world.on_scroll(count)
        self.executor.reset_scene()

    def is_adb(self):
        return False

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        self.marks["down"] = time.time()
        self.executor.reset_scene()

    def mouse_up(self, name=None, key="left"):
        self.marks["up"] = time.time()

    def send_key(self, key, down_time=0.02, interval=-1, after_sleep=0):
        self.executor.world.on_key(str(key))
        self.executor.reset_scene()
        if after_sleep > 0:
            self.sleep(after_sleep)

    def ocr(self, x=0, y=0, to_x=1, to_y=1, match=None, frame=None, **kwargs):
        """与 ok 框架相同：可以只识别一块区域，也可以指定帧；区域越小越快。"""
        image = frame if frame is not None else self.executor.frame
        whole = (x, y, to_x, to_y) == (0, 0, 1, 1)
        time.sleep(OCR_TIME if whole else OCR_TIME / 4)
        boxes = [Box(b.name, b.x, b.y, b.width, b.height) for b in image.boxes]
        if whole:
            return boxes
        return [b for b in boxes if x <= (b.x + b.width / 2) / self.width <= to_x
                and y <= (b.y + b.height / 2) / self.height <= to_y]

    def find_feature(self, feature_name=None, horizontal_variance=0, vertical_variance=0, threshold=0,
                     box=None, frame=None, limit=0):
        # 与 ok 框架相同：没传 frame 就取 executor.frame；匹配用真实 cv2.matchTemplate
        image = frame if frame is not None else self.executor.frame
        self.match_log.append((feature_name, threading.current_thread().name, id(image), time.time()))
        area = image
        if box is not None:
            area = area[box.y:box.y + box.height, box.x:box.x + box.width]
        result = cv2.matchTemplate(area, TEMPLATES[feature_name], cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= (threshold or 0.8))
        return [Box(feature_name, int(x), int(y), 60, 60) for x, y in zip(xs, ys)]

    def log_info(self, message):
        self.logs.append(message)

    def info_set(self, key, value):
        self.logs.append(f"info {key}={value}")

    def _check_upload_if_needed(self):
        pass

    def run(self):
        # 与 ChaosMode.run 源码一致，供 speedup 的兼容性检查使用
        self.all_texts = _simplify_texts(self.ocr())
        for handle_page in utils_chaos.PAGE_HANDLERS:
            if handle_page(self):
                return
        self._check_upload_if_needed()


def run_loop(task, duration, stop=None):
    """仿 TaskExecutor.execute：按 trigger_interval 触发，触发前先取帧。"""
    ex, last, end = task.executor, 0.0, time.time() + duration
    while time.time() < end and not (stop and stop()):
        now = time.time()
        if now - last > task.trigger_interval:
            last = now
            ex.current_task = task
            try:
                ex.next_frame()
                task.run()
            except TaskDisabled:
                task.marks["disabled_raised"] = time.time()
                task.disabled_flag = False
            ex.current_task = None
        else:
            time.sleep(0.002)


def make(page, rules, handlers, enabled=True):
    world = World(page, rules)
    task = FakeTask(FakeExecutor(world), enabled)
    speedup.install(task)
    utils_chaos.PAGE_HANDLERS[:] = handlers
    return world, task


class SortieTask(FakeTask):
    """出击模式：run() 结构与卡厄思相同，但用的是 utils_sortie 的处理函数列表。"""
    name = "自动出击模式"

    def run(self):
        self.all_texts = _simplify_texts(self.ocr())
        for handle_page in utils_sortie.PAGE_HANDLERS:
            if handle_page(self):
                return
        self._check_upload_if_needed()


class OtherTask(FakeTask):
    """run() 结构与预期不符的任务（例如作者改版后）。"""
    name = "其他任务"

    def run(self):
        self.all_texts = _simplify_texts(self.ocr())
        handle_center_confirm(self)


def make_sortie(page, rules, handlers, enabled=True):
    world = World(page, rules)
    task = SortieTask(FakeExecutor(world), enabled)
    utils_chaos.PAGE_HANDLERS[:] = []  # 卡厄思的列表留空：出击模式必须用自己的
    utils_sortie.PAGE_HANDLERS[:] = handlers  # 与真实情况一致：列表先建好，补丁后装
    speedup.install(task)
    return world, task


def handle_battle_page(task):
    """出击模式的战斗处理函数。"""
    return bool(find(task, "5/10"))


def find(task, name):
    return next((b for b in task.all_texts if b.name == name), None)


# ---------------- 页面处理函数（模仿 ok-kes 写法） ----------------
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


def handle_multi(task):
    box = find(task, "打开")
    if box:
        task.click_box(box, after_sleep=0)
        task.marks["click1"] = time.time()
        task.sleep(0.5)
        _ = task.frame
        task.marks["frame_after_sleep"] = time.time()
        task.click(0.5, 0.5)
        task.marks["click2"] = time.time()
        task.sleep(1)
        return True
    return False


def handle_route_selection(task):
    if find(task, "路线"):
        task.marks["route_sleep_call"] = time.time()
        task.sleep(1)
        _ = task.frame
        task.marks["route_frame"] = time.time()
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
        task.marks["done"] = True
        return True
    return False


def handle_battle_auto_check(task):
    return bool(find(task, "5/10"))


PAGES["MATCH"] = [Box("匹配测试", 300, 100)]
MATCH_BOX = Box("区域", 0, 0, 2200, 1300)


def handle_matching(task):
    """按 ok-kes 的写法依次匹配：路线页 9 种、小地图两组（阈值不同）、牌库/选卡页卡牌类型。"""
    if not find(task, "匹配测试"):
        return False
    task.marks["settle_call"] = time.time()
    task.sleep(1)
    # 先请求牌库组：走普通并行路径，取帧前补足 sleep(1)；之后各组都用这同一帧
    cards = {n: task.find_feature(feature_name=n, box=MATCH_BOX, threshold=0.65)
             for n in speedup._PARALLEL_GROUPS[3] + speedup._PARALLEL_GROUPS[4]}
    route = {n: task.find_feature(feature_name=n, box=MATCH_BOX)
             for n in ("safezone", "enemy", "elite", "event", "settlement")}
    route.update({n: task.find_feature(feature_name=n, box=MATCH_BOX) for n in ("shop", "kalei", "seal", "hard")})
    task.marks["route_end"] = time.time()
    minimap = {n: task.find_feature(feature_name=n, box=MATCH_BOX, threshold=0.85)
               for n in speedup._PARALLEL_GROUPS[1]}
    minimap.update({n: task.find_feature(feature_name=n, box=MATCH_BOX, threshold=0.65)
                    for n in speedup._PARALLEL_GROUPS[2]})
    task.find_feature(feature_name="safezone", box=MATCH_BOX, limit=1)  # 带额外参数：应走原逻辑
    task.marks["results"] = {k: [(b.x, b.y) for b in v] for k, v in {**route, **minimap, **cards}.items()}
    task.marks["done"] = True
    return True


# ---------------- 删卡流程 ----------------
DECK_ANIM = 0.2
DECK_TEXTURE = _rng.integers(0, 255, (3000, 2200, 3), dtype=np.uint8)
CARD_RAISE = 0.10  # 按数字键后卡牌上滑到位所需时间（实测 0.01~0.04 秒）
CARD_PLAY = 0.25   # 回车后手牌数减少所需时间（实测 0.2~0.3 秒）


def battle_image_fn(world):
    """卡牌上滑到位后手牌区域变样，其余时间不动。"""
    y1, y2, x1, x2 = int(0.660 * 1300), int(0.950 * 1300), int(0.159 * 2200), int(0.836 * 2200)

    def image_at(now):
        if now < world.hand_anim_until:  # 上一张牌的动画：手牌区每帧都不一样
            image = SCENE.copy()
            shift = int(now * 1000) % 600
            image[y1:y2, x1:x2] = DECK_TEXTURE[shift:shift + y2 - y1, x1:x2]
            return image
        if not (world.raise_at and now >= world.raise_at):
            return SCENE
        image = SCENE.copy()
        image[y1:y2, x1:x2] = DECK_TEXTURE[1000:1000 + y2 - y1, x1:x2]
        return image
    return image_at


def deck_image_fn(world):
    """卡牌区域画面随滚动移动：每次滚动后有 0.2 秒动画，然后静止。"""
    y1, y2, x1, x2 = int(0.108 * 1300), int(0.874 * 1300), int(0.274 * 2200), int(0.929 * 2200)

    def image_at(now):
        visual = world.deck_offset
        if world.deck_anim:
            start, before, after = world.deck_anim
            visual = before + (after - before) * min(1.0, (now - start) / DECK_ANIM)
        image = SCENE.copy()
        shift = int(visual * 150)
        image[y1:y2, x1:x2] = DECK_TEXTURE[shift:shift + (y2 - y1), x1:x2]
        return image
    return image_at

PAGES["REMOVE"] = [Box("请选择1张要移除的卡牌", 300, 60)]


def handle_remove(task):
    """模仿 handle_select_card：每次进入删卡页，牌库从顶部开始显示。"""
    if not find(task, "请选择1张要移除的卡牌"):
        return False
    world = task.executor.world
    world.deck_offset, views_before, scrolls_before = 0, world.deck_views, len(world.scrolls)
    utils.select_card(task, utils._get_card_list(task, "移除卡牌列表"), count=1, action="移除")
    task.marks.setdefault("flows", []).append((world.deck_views - views_before, len(world.scrolls) - scrolls_before))
    return True


# 要求移除多张的删卡页：点卡牌即选中；“跳过”直接离开，一张不删；“移除”删掉已选中的卡
REMOVE_BUTTON = Box("移除", 2400, 1330, 120, 48)
CARD_POINT = (W // 2, H // 2)  # 替身 select_card 点卡牌的位置
SKIP_POINT = (utils._SkipButton.x + utils._SkipButton.width // 2, utils._SkipButton.y + utils._SkipButton.height // 2)
REMOVE_POINT = (REMOVE_BUTTON.x + REMOVE_BUTTON.width // 2, REMOVE_BUTTON.y + REMOVE_BUTTON.height // 2)
PAGES["DONE"] = [Box("删卡完成", 1100, 600)]


def removal_game(world, count, partial_ok=True):
    """partial_ok=False 模拟“移除”按钮要选够张数才可点的情况。"""
    PAGES["REMOVE_N"] = [Box(f"请选择{count}张要移除的卡牌", 300, 60)]
    world.page, world.selected_cards = "REMOVE_N", set()
    game = {"selected": 0, "result": None, "need": 1 if partial_ok else count}

    def on_click(x, y):
        if world.page != "REMOVE_N":
            return
        if (x, y) == CARD_POINT:
            game["selected"] += 1
        elif (x, y) == SKIP_POINT:
            game["result"], world.page = ("跳过", 0), "DONE"
        elif (x, y) == REMOVE_POINT and game["selected"] >= game["need"]:
            game["result"], world.page = ("移除", game["selected"]), "DONE"
    world.click_fn = on_click
    world.removal = game
    return game


def handle_confirm_remove(task):
    """模仿 handle_remove：右下角“移除”按钮可点时点击，按待移除计数记录删了几张。"""
    game = task.executor.world.removal
    if not any(b.name.startswith("请选择") for b in task.all_texts) or game["selected"] < game["need"]:
        return False
    task.click_box(REMOVE_BUTTON)
    task.marks["recorded"] = max(1, getattr(task, "_pending_removed_card_count", 0))
    task._pending_removed_card_count = 0
    task.sleep(1)
    return True


def handle_select_remove(task):
    """模仿 handle_select_card：按标题里的张数选卡；再次进入时牌库停在上次翻到的位置。"""
    title = next((b.name for b in task.all_texts if b.name.startswith("请选择")), None)
    if not title:
        return False
    task.marks["select_calls"] = task.marks.get("select_calls", 0) + 1
    count = int(title[len("请选择"):title.index("张")])
    utils.select_card(task, utils._get_card_list(task, "移除卡牌列表"), count=count, action="移除")
    return True


# ---------------- 路线页图标入场动画 ----------------
ROUTE_BASE = _rng.integers(0, 255, (700, 900, 3), dtype=np.uint8)
ROUTE_FINAL = {n: (60 + (i % 3) * 250, 60 + (i // 3) * 200) for i, n in enumerate(speedup._PARALLEL_GROUPS[0])}


def route_image_fn(anim_end, icons=True):
    """anim_end 之前图标在横向来回移动，之后停在最终位置；icons=False 模拟 Boss 节点（没有普通节点图标）。"""
    def image_at(now):
        image = ROUTE_BASE.copy()
        if icons:
            shift = int(((anim_end - now) * 400) % 200) if now < anim_end else 0
            for name, (x, y) in ROUTE_FINAL.items():
                image[y:y + 60, x + shift:x + shift + 60] = TEMPLATES[name]
        return image
    return image_at


PAGES["ROUTE_SETTLE"] = [Box("路线稳定测试", 300, 100)]


def handle_route_settle(task):
    """模仿 handle_route_selection：先 sleep(1) 等图标入场，再逐个匹配 9 种节点/标记。"""
    if not find(task, "路线稳定测试"):
        return False
    task.marks["settle_call"] = time.time()
    task.sleep(1)
    results = {n: task.find_feature(feature_name=n, box=None) for n in speedup._PARALLEL_GROUPS[0]}
    task.marks["settle_done"] = time.time()
    task.marks["positions"] = {n: sorted((b.x, b.y) for b in v) for n, v in results.items()}
    task.marks["done"] = True
    return True


# ---------------- 场景 ----------------
results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"[{'通过' if ok else '失败'}] {name}：{detail}")


def scenario_transition(enabled):
    world, task = make("A", {"A": (0.1, 0.5, "B", MID)}, [handle_center_confirm, handle_page_b], enabled)
    run_loop(task, 4, stop=lambda: "reached_b" in task.marks)
    click_t = world.clicks[0][0]
    confirm_clicks = sum(1 for c in world.clicks if c[1] == "A")
    return task.marks["reached_b"] - click_t, confirm_clicks


def main():
    base, base_clicks = scenario_transition(False)
    fast, fast_clicks = scenario_transition(True)
    check("关闭加速=原版时序", 2.0 <= base < 2.6 and base_clicks == 1, f"点确认→处理下一页 {base:.2f}s")
    check("正常过渡明显提速且不重复点击", fast < 1.3 and fast_clicks == 1,
          f"{fast:.2f}s（原版 {base:.2f}s），确认按钮被点 {fast_clicks} 次")

    # 点了没反应的按钮：不能比原版更快地重复点
    world, task = make("SHOP", {}, [handle_free])
    run_loop(task, 5.2)
    times = [c[0] for c in world.clicks]
    gaps = [b - a for a, b in zip(times, times[1:])]
    check("页面不响应时按原版间隔重试", gaps and min(gaps) >= 2.0,
          f"重复点击间隔 {', '.join(f'{g:.2f}s' for g in gaps)}")

    # 多步处理函数：中间等待必须补足，尾部等待按原版
    world, task = make("MULTI", {}, [handle_multi])
    run_loop(task, 2.5, stop=lambda: len(world.clicks) >= 3)
    mid_wait = task.marks["frame_after_sleep"] - task.marks["click1"]
    next_click = world.clicks[2][0] - world.clicks[1][0] if len(world.clicks) >= 3 else None
    check("多步操作中途取画面前补足等待", mid_wait >= 0.5, f"sleep(0.5) 后取画面间隔 {mid_wait:.2f}s")
    check("多步操作尾部等待不缩短", next_click is not None and next_click >= 1.0,
          f"最后一次点击到下一轮点击 {next_click:.2f}s")

    # 路线选择：点击前的 sleep(1) 要真等；悬停缩到 0.1s；点击后交给闸门
    world, task = make("ROUTE", {"ROUTE": (0.05, 0.3, "EMPTY", [])}, [handle_route_selection])
    run_loop(task, 3, stop=lambda: "route_click" in task.marks)
    settle = task.marks["route_frame"] - task.marks["route_sleep_call"]
    hover = task.marks["route_click"] - task.marks["move"]
    check("识别前的稳定等待保持原时长", settle >= 1.0, f"{settle:.2f}s")
    check("点击前悬停缩短为 0.1s", 0.09 <= hover < 0.2, f"{hover:.2f}s（原版 0.5s）")

    # 长按：按住期间的 sleep(2) 必须完整；松开后的 sleep(1) 在取画面前补足
    world, task = make("LONG", {}, [handle_long_press])
    run_loop(task, 3.6, stop=lambda: task.marks.get("done"))
    held = task.marks["up"] - task.marks["down"]
    after_up = task.marks["frame_after_up"] - task.marks["up"]
    check("长按时长不被缩短", held >= 2.0, f"按住 {held:.2f}s")
    check("松开后等待补足", after_up >= 1.0, f"{after_up:.2f}s")

    # 战斗中按战斗间隔（speedup._BATTLE_INTERVAL），非战斗 0.3 秒
    world, task = make("BATTLE", {}, [handle_battle_auto_check])
    run_loop(task, 0.5)
    battle_interval = task.trigger_interval
    world.page = "EMPTY"
    utils_chaos.PAGE_HANDLERS[:] = [handle_page_b, handle_battle_auto_check]
    world.page = "B"
    run_loop(task, 1.2)
    check("战斗/非战斗检测间隔", battle_interval == speedup._BATTLE_INTERVAL and task.trigger_interval == 0.3,
          f"战斗 {battle_interval}s，非战斗 {task.trigger_interval}s")

    # 出击模式：处理函数列表取自 utils_sortie，闸门与检测间隔同样生效
    world, task = make_sortie("A", {"A": (0.1, 0.5, "B", MID)}, [handle_center_confirm, handle_page_b])
    run_loop(task, 4, stop=lambda: "reached_b" in task.marks)
    latency = task.marks["reached_b"] - world.clicks[0][0]
    confirm_clicks = sum(1 for click in world.clicks if click[1] == "A")
    check("出击模式：用自己的页面处理函数列表，闸门照常生效",
          task._speedup["handlers"] is utils_sortie and latency < 1.3 and confirm_clicks == 1,
          f"点确认到处理下一页 {latency:.2f}s（原版 2.13s），确认按钮被点 {confirm_clicks} 次")
    world, task = make_sortie("BATTLE", {}, [handle_battle_page])
    run_loop(task, 0.5)
    battle_interval = task.trigger_interval
    utils_sortie.PAGE_HANDLERS[:] = [handle_page_b, handle_battle_page]
    world.page = "B"
    run_loop(task, 1.2)
    check("出击模式：战斗中按战斗间隔", battle_interval == speedup._BATTLE_INTERVAL and task.trigger_interval == 0.3,
          f"战斗 {battle_interval}s，非战斗 {task.trigger_interval}s")

    # run() 结构与预期不符（例如作者改版）：自动停用闸门，其余优化照常
    world = World("A", {"A": (0.1, 0.5, "B", MID)})
    task = OtherTask(FakeExecutor(world), True)
    speedup.install(task)
    run_loop(task, 3.5)
    check("run() 与预期不符时停用闸门，其余优化照常",
          task._speedup["gate_ok"] is False and len(world.clicks) == 1 and task.trigger_interval == 0.3,
          f"闸门 {task._speedup['gate_ok']}，确认按钮被点 {len(world.clicks)} 次，检测间隔 {task.trigger_interval}s")

    # 卡死检测：检测变密后仍约 1 秒采样一次
    world, task = make("EMPTY", {}, [utils.handle_stuck_log])
    run_loop(task, 3.05)
    samples = getattr(task, "stuck_samples", 0)
    check("卡死检测采样频率不变", 3 <= samples <= 4, f"3 秒内采样 {samples} 次")

    # 等待途中用户停止任务：异常要正常抛出，状态复位后能继续跑
    world, task = make("A", {"A": (5, 1, "B", MID)}, [handle_center_confirm, handle_page_b])
    timer = threading.Timer(0.6, lambda: setattr(task, "disabled_flag", True))
    timer.start()
    run_loop(task, 1.5)
    state = task._speedup
    check("停止任务时正常中断并复位", "disabled_raised" in task.marks and state["owed_until"] == 0.0
          and not state["in_run"], f"中断于点击后 {task.marks.get('disabled_raised', 0) - world.clicks[0][0]:.2f}s")

    # 并行模板匹配：结果一致、同一帧、真在工作线程并行、不跨组白算、耗时下降
    runs = {}
    for enabled in (False, True):
        world, task = make("MATCH", {}, [handle_matching], enabled)
        run_loop(task, 4, stop=lambda: task.marks.get("done"))
        runs[enabled] = task
    seq, par = runs[False], runs[True]
    found = sum(1 for v in par.marks["results"].values() if v)
    check("并行匹配结果与逐个匹配完全一致", seq.marks["results"] == par.marks["results"] and found == 29,
          f"29 种图标全部命中且坐标相同（命中 {found} 种）")
    grouped = [c for c in par.match_log if c[1].startswith("speedup-match")]
    on_main = [c for c in par.match_log if not c[1].startswith("speedup-match")]
    check("组内图标在工作线程并行、每个只算一次", len(grouped) == 29 and len(on_main) == 1,
          f"工作线程 {len(grouped)} 次，主线程 {len(on_main)} 次（带 limit 参数的那次）")
    check("整组共用同一帧", len({c[2] for c in par.match_log}) == 1, f"用到 {len({c[2] for c in par.match_log})} 帧")
    first_match = min(c[3] for c in par.match_log)
    check("匹配前的 sleep(1) 仍然补足", first_match - par.marks["settle_call"] >= 1.0,
          f"sleep(1) 调用后 {first_match - par.marks['settle_call']:.2f}s 才开始匹配")
    check("关闭加速时全部在主线程逐个匹配", all(not c[1].startswith("speedup-match") for c in seq.match_log),
          f"{len(seq.match_log)} 次")
    def route_time(task):  # 从第一个路线图标真正开始匹配，到 9 个结果全部拿到
        start = min(c[3] for c in task.match_log if c[0] in speedup._PARALLEL_GROUPS[0])
        return task.marks["route_end"] - start
    check("路线页 9 种图标匹配耗时下降", route_time(par) < route_time(seq) * 0.6,
          f"逐个 {route_time(seq) * 1000:.0f}ms → 并行 {route_time(par) * 1000:.0f}ms")

    # 路线页：图标停稳即继续（至少 0.4s，最长 1s）
    def route_case(anim_seconds, enabled=True, icons=True):
        world, task = make("ROUTE_SETTLE", {}, [handle_route_settle], enabled)
        world.image_fn = route_image_fn(time.time() + anim_seconds, icons)
        run_loop(task, 3, stop=lambda: task.marks.get("done"))
        wait = task.marks["settle_done"] - task.marks["settle_call"]
        final = {n: [p] for n, p in ROUTE_FINAL.items()}
        return wait, task.marks["positions"] == final
    wait, ok = route_case(0.2)
    check("路线图标早停稳：提前继续且位置是最终位置", 0.4 <= wait < 0.85 and ok, f"等待 {wait:.2f}s（原 1s），位置正确={ok}")
    wait, ok = route_case(2.0)
    check("路线图标一直在动：等满原来的 1 秒", 0.95 <= wait < 1.4, f"等待 {wait:.2f}s")
    wait, _ = route_case(0.0, icons=False)
    check("Boss 节点（无图标）：等满原来的 1 秒", 0.95 <= wait < 1.4, f"等待 {wait:.2f}s")
    wait, ok = route_case(0.2, enabled=False)
    check("关闭加速：路线页仍固定等 1 秒", 0.95 <= wait < 1.6 and ok, f"等待 {wait:.2f}s（含逐个匹配 9 个图标）")

    # 删卡翻页：步长和次数不变；最下面一行也能识别；滚动后列表停稳即识别
    deck = ["声音测试", "安可", "聚光灯", "音乐开始", "暗黑之刃", "物质再生", "黑暗斩击", "共鸣之暗",
            "掠食者之刃", "点心时间", "喘气", "安可", "聚光灯", "声音测试", "音乐开始", "黑暗斩击"]  # 4 行

    def removal_run(enabled, cards=deck):
        world, task = make("REMOVE", {}, [handle_remove], enabled)
        world.deck = list(cards)
        world.image_fn = deck_image_fn(world)
        run_loop(task, 20, stop=lambda: len(task.marks.get("flows", [])) >= 1)
        return world, task

    def views_per_row(world):
        return [sum(1 for page in world.seen if any(row == index for row, _ in page)) for index in range(4)]

    def waits_after_scroll(world):
        times = [t for _, _, t in world.recognize_log]
        return [next(t - s for t in times if t > s) for s in world.scroll_times if any(t > s for t in times)]

    base_world, base_task = removal_run(False)
    fast_world, fast_task = removal_run(True)
    check("翻页次数和步长与原逻辑相同", base_task.marks["flows"][0] == fast_task.marks["flows"][0]
          and base_world.scrolls == fast_world.scrolls,
          f"原逻辑 {base_task.marks['flows'][0]}，加速 {fast_task.marks['flows'][0]}（识别页数, 滚动次数）")
    base_views, fast_views = views_per_row(base_world), views_per_row(fast_world)
    check("每行被识别的次数不少于原逻辑（最下面一行也算上）",
          all(f >= b for b, f in zip(base_views, fast_views)) and sum(fast_views) > sum(base_views),
          f"每行识别次数 原逻辑 {base_views}，加速 {fast_views}")
    base_waits, fast_waits = waits_after_scroll(base_world), waits_after_scroll(fast_world)
    check("滚动后列表停稳即识别", min(base_waits) >= 0.5 and max(fast_waits) < 0.45,
          f"滚动到识别 原逻辑 {[round(w, 2) for w in base_waits]}，加速 {[round(w, 2) for w in fast_waits]}")
    world, task = make("REMOVE", {}, [], True)
    world.deck = list(deck)
    world.image_fn = lambda now: SCENE  # 画面不动：检测不到滚动
    task._speedup["active"] = True
    task.executor.current_task = task
    speedup._wrap_executor_next_frame(task.executor)
    start = time.time()
    utils._scroll_card_page(task, 0.251, 0.735, -3, "select_card-移除")
    _ = task.frame
    task._speedup["active"] = False
    check("没检测到滚动时照原逻辑等满 0.5 秒", time.time() - start >= 0.5, f"{time.time() - start:.2f}s")

    def thresholds(page, active=True):
        world, task = make("REMOVE", {}, [], True)
        world.deck = list(deck)
        task._speedup["active"] = active
        utils.recognize_cards_in_deck(task, page=page)
        return world.recognize_log[-1][1]
    check("只凭卡名保留只用于删卡/复制卡",
          "attack_in_deck" in thresholds("select_card-移除") and "attack_in_deck" in thresholds("select_card-复制")
          and thresholds("select_card-闪光") == ["hex_in_deck", "hex_in_deck_tw"]
          and thresholds("select_card-移除", active=False) == ["hex_in_deck", "hex_in_deck_tw"],
          f"闪光页 {thresholds('select_card-闪光')}")
    found = []
    for row in range(4):
        cards = list(deck)
        cards[row * 4 + 2] = "拍照时间"
        world, task = removal_run(True, cards)
        found.append(any(click[1] == "REMOVE" and click[2:] == CARD_POINT for click in world.clicks))
    check("目标卡放在任何一行都能找到", all(found), f"各行 {found}")

    # 要求移除多张但目标卡不够：选中几张就删几张，一张没选中才跳过
    def place(targets):
        cards = list(deck)
        for row, name in targets:
            cards[row * 4 + 1] = name
        return cards

    def multi_removal(count, targets, enabled=True, partial_ok=True, world=None, task=None):
        if world is None:
            world, task = make("REMOVE_N", {}, [handle_confirm_remove, handle_select_remove], enabled)
            world.image_fn = deck_image_fn(world)
        world.deck, world.deck_offset, task.marks["select_calls"] = place(targets), 0, 0
        task.marks.pop("recorded", None)
        game = removal_game(world, count, partial_ok)
        run_loop(task, 20, stop=lambda: game["result"] is not None)
        return game["result"], task.marks.get("recorded"), task.marks["select_calls"], world, task

    one, two = [(1, "拍照时间")], [(0, "拍照时间"), (2, "粉丝福利")]
    result, recorded, calls, world, task = multi_removal(2, one)
    check("需删 2 张只找到 1 张：删掉这 1 张", result == ("移除", 1) and recorded == 1 and calls == 1
          and not any(click[2:] == SKIP_POINT for click in world.clicks),
          f"结果 {result}，记录 {recorded} 张，进入选卡 {calls} 次")
    result, recorded, calls, _, _ = multi_removal(2, one, world=world, task=task)
    check("紧接着再遇到删卡：仍删掉找到的卡", result == ("移除", 1) and recorded == 1 and calls == 1,
          f"结果 {result}，记录 {recorded} 张，进入选卡 {calls} 次")
    result, recorded, calls, _, _ = multi_removal(3, two)
    check("需删 3 张找到 2 张：删掉 2 张并记录 2 张", result == ("移除", 2) and recorded == 2 and calls == 1,
          f"结果 {result}，记录 {recorded} 张")
    result, recorded, calls, _, _ = multi_removal(2, two)
    check("找够张数时与原逻辑相同", result == ("移除", 2) and recorded == 2 and calls == 1,
          f"结果 {result}，记录 {recorded} 张")
    result, _, calls, _, _ = multi_removal(2, [])
    check("一张目标都没有：照原逻辑跳过", result == ("跳过", 0) and calls == 1, f"结果 {result}")
    result, _, calls, _, _ = multi_removal(2, one, enabled=False)
    check("关闭加速：照原逻辑跳过", result == ("跳过", 0) and calls == 1, f"结果 {result}")
    result, _, calls, _, task = multi_removal(2, [(0, "拍照时间")], partial_ok=False)
    check("“移除”按钮要选够才可点时：只多进一次选卡，随后照原逻辑跳过",
          result == ("跳过", 0) and calls == 2 and getattr(task, "_pending_removed_card_count", 0) == 0,
          f"结果 {result}，进入选卡 {calls} 次")

    world, task = make("REMOVE_N", {}, [], True)
    world.deck, world.image_fn = place(one), deck_image_fn(world)
    game = removal_game(world, 2)
    task._speedup["active"] = True
    task.executor.current_task = task
    speedup._wrap_executor_next_frame(task.executor)
    utils.select_card(task, utils._get_card_list(task, "移除卡牌列表"), count=2, action="复制")
    task._speedup["active"] = False
    check("复制卡不够时仍照原逻辑跳过", game["result"] == ("跳过", 0), f"结果 {game['result']}")

    # 出击模式战斗出牌：按数字键后等卡牌上滑、回车后等手牌数减少
    PAGES["BATTLE_PLAY"] = []

    def handle_battle_fallback(task):
        """仿 handle_battle_page 未命中出牌优先级时：从手牌数倒着把按键都按一遍。"""
        if not any("/10" in b.name for b in task.all_texts):
            return False
        task.marks.setdefault("plays", []).append(time.time())
        utils_sortie._try_all_card_keys(task, 5)
        return True

    def play_card(enabled, hand=5, raise_card=True, play_works=True, popup=False, handler=None):
        world, task = make_sortie("BATTLE_PLAY", {}, [handler or ORIGINAL_BATTLE_PAGE], enabled)
        world.hand, world.play_fails, world.popup_on_enter = hand, not play_works, popup
        world.image_fn = battle_image_fn(world) if raise_card else (lambda now: SCENE)
        run_loop(task, 12, stop=lambda: len(task.marks.get("plays", [])) >= 1)
        world.task = task
        keys = {key: t for t, key in world.keys}
        return time.time() - world.keys[0][0], world, keys

    make_sortie("BATTLE_PLAY", {}, [ORIGINAL_BATTLE_PAGE])
    entry = utils_sortie.PAGE_HANDLERS[0]
    check("处理函数列表里存的原出牌函数也被替换成加速版本",
          entry is not ORIGINAL_BATTLE_PAGE and getattr(entry, "_speedup_wrapped", False)
          and entry.__name__ == "handle_battle_page",
          f"列表里是「{entry.__name__}」，已替换={entry is not ORIGINAL_BATTLE_PAGE}")
    original_play, _, _ = play_card(False)
    fast_play, world, keys = play_card(True)
    digit_time = next(t for t, key in world.keys if key.isdigit())
    enter_time = keys.get("enter", 0)
    check("关闭加速：出牌按原固定时长等待", original_play >= 2.9, f"一张牌 {original_play:.2f}s（1 秒 + 2 秒）")
    check("出牌提速：卡牌上滑后才回车，手牌数减少后继续",
          fast_play < 1.2 and enter_time - digit_time >= CARD_RAISE and world.hand == 4,
          f"一张牌 {fast_play:.2f}s（原 {original_play:.2f}s），按键到回车 {enter_time - digit_time:.2f}s，"
          f"手牌 5→{world.hand}")
    stuck_play, stuck_world, _ = play_card(True, raise_card=False, play_works=False)
    check("卡牌没上滑、也没打出去时照原逻辑等满", stuck_play >= 2.9 and stuck_world.hand == 5,
          f"一张牌 {stuck_play:.2f}s，手牌仍是 {stuck_world.hand}")
    popup_play, popup_world, _ = play_card(True, popup=True)
    check("出牌后弹出“请选择功能”：不再干等，马上交给下一轮处理",
          popup_play < 1.2 and [key for _, key in popup_world.keys] == ["4", "enter"]
          and popup_world.task.trigger_interval == 0.3 and world.task.trigger_interval == speedup._BATTLE_INTERVAL,
          f"出牌到结束 {popup_play:.2f}s（原 3 秒），按键 {[key for _, key in popup_world.keys]}，"
          f"下一轮间隔 {popup_world.task.trigger_interval}s（普通出牌后 {world.task.trigger_interval}s）")
    fallback_original, _, _ = play_card(False, handler=handle_battle_fallback)
    fallback_fast, fallback_world, _ = play_card(True, handler=handle_battle_fallback)
    check("兜底出牌逐张等牌打出就继续", fallback_fast < fallback_original * 0.6 and fallback_world.hand == 0,
          f"5 张牌 {fallback_fast:.2f}s（原 {fallback_original:.2f}s），手牌 5→{fallback_world.hand}")
    fallback_popup, popup_world, _ = play_card(True, popup=True, handler=handle_battle_fallback)
    check("兜底出牌中途弹窗：剩下的按键不再发送",
          fallback_popup < 1.5 and [key for _, key in popup_world.keys] == ["5", "enter"],
          f"{fallback_popup:.2f}s，按键 {[key for _, key in popup_world.keys]}（原逻辑会继续按 4、3、2、1）")

    # 上一张牌的动画还在手牌区播放、游戏又卡了一下：不能把动画误判成上滑而过早回车，否则牌会停在上滑状态
    world, task = make_sortie("BATTLE_PLAY", {}, [ORIGINAL_BATTLE_PAGE])
    world.hand, world.image_fn = 5, battle_image_fn(world)
    world.hand_anim_until, world.raise_delay = time.time() + 0.4, 0.3
    run_loop(task, 6, stop=lambda: len(task.marks.get("plays", [])) >= 1)
    digit = next(t for t, key in world.keys if key.isdigit())
    enter = next(t for t, key in world.keys if key == "enter")
    check("手牌区还在动时先等静止再按键，卡牌真正上滑后才回车",
          digit >= world.hand_anim_until - 0.05 and enter >= digit + world.raise_delay and world.hand == 4,
          f"动画结束后 {digit - world.hand_anim_until:+.2f}s 按键，按键后 {enter - digit:.2f}s 回车"
          f"（卡牌 {world.raise_delay}s 后才上滑），手牌 5→{world.hand}")

    # 结束回合：出过牌后新抽的牌还没到手时不按 E；按过 E 后不连按；要连续两轮都判断该结束回合
    def end_turn_run(enabled, seconds, cards_after=None, just_played=False):
        world, task = make_sortie("BATTLE_PLAY", {}, [ORIGINAL_BATTLE_PAGE], enabled)
        world.hand, world.end_turn_button, world.image_fn = (5 if cards_after else 0), True, battle_image_fn(world)
        start = time.time()
        if cards_after:
            world.cards_visible_at = start + cards_after
        if just_played:
            task._speedup["last_play_key"] = start
        run_loop(task, seconds)
        return [t - start for t, key in world.keys if key == "e"], [t - start for t, key in world.keys
                                                                     if key.isdigit()]

    original_e, _ = end_turn_run(False, 6)
    fast_e, _ = end_turn_run(True, 8)
    gaps = [b - a for a, b in zip(fast_e, fast_e[1:])]
    check("结束回合不连按：按过 E 后 5 秒内不再按，第一次也要连续两轮确认",
          fast_e and fast_e[0] >= 0.8 and all(g >= 5.0 for g in gaps) and len(original_e) >= 4,
          f"加速：按 E 的时刻 {[round(t, 1) for t in fast_e]}；原逻辑 6 秒内按了 {len(original_e)} 次")
    drawn_e, digits = end_turn_run(True, 4, cards_after=1.5, just_played=True)
    check("刚出过牌、新抽的牌还没到手时不结束回合，牌到手后继续出牌",
          not drawn_e and digits and digits[0] >= 1.5,
          f"按 E {drawn_e}，牌在 1.5s 到手，{digits[0] if digits else '—':.2f}s 开始出牌" if digits
          else f"按 E {drawn_e}，没有出牌")

    # 文字闸门：被点的按钮只是 OCR 闪了一下（一帧没识别到），不能当成页面已响应而马上再点一次
    world, task = make("SHOP", {}, [handle_free])
    world.flicker = (0.3, 0.15)
    run_loop(task, 5.2)
    times = [c[0] for c in world.clicks]
    gaps = [b - a for a, b in zip(times, times[1:])]
    check("按钮文字闪一下不算页面已响应，照原间隔重试", gaps and min(gaps) >= 1.9,
          f"重复点击间隔 {[round(g, 2) for g in gaps]}（原逻辑约 2 秒）")

    # 按钮文字被 OCR 切成两个框时仍能找到按钮（实测国际服「赋予灵光一闪」的框位置）
    def span(name, x1, x2, y1, y2):
        return Box(name, int(x1 * W), int(y1 * H), int((x2 - x1) * W), int((y2 - y1) * H))

    world, task = make("EMPTY", {}, [], True)
    task.all_texts = [span("跳过", 0.750, 0.792, 0.905, 0.947),
                      span("赋予灵光-", 0.862, 0.941, 0.902, 0.949),
                      span("一闪", 0.949, 0.973, 0.906, 0.944)]
    merged = utils.find_box_at_point(task, 0.945, 0.918)   # 作者用来找右下角按钮的点，落在两框的空隙里
    single = utils.find_box_at_point(task, 0.770, 0.925)
    nothing = utils.find_box_at_point(task, 0.830, 0.925)  # 「跳过」和按钮之间的大空隙
    check("按钮文字被切成两个框时合并后仍能找到",
          merged is not None and merged.name == "赋予灵光一闪"
          and merged.x <= int(0.862 * W) and merged.x + merged.width >= int(0.973 * W) - 1,
          f"合并为「{getattr(merged, 'name', None)}」x={getattr(merged, 'x', 0) / W:.3f}~"
          f"{(getattr(merged, 'x', 0) + getattr(merged, 'width', 0)) / W:.3f}")
    check("原本找得到的框和相距远的框不受影响",
          single is not None and single.name == "跳过" and nothing is None,
          f"单框「{getattr(single, 'name', None)}」，大空隙处 {nothing}")

    # 加速选项不进导出配置
    import config_io
    check("加速选项不写入导出配置码", {"加速模式", "点击前悬停等待(秒)", "非战斗检测间隔(秒)"} <= config_io.UI_ONLY_CONFIG_KEYS,
          str(sorted(config_io.UI_ONLY_CONFIG_KEYS)))

    # 自动卡厄思模式：商店信用点 < 80 就离开；每次回到商店页（即上一次商店操作结束后）再查
    def shop_boxes(credit, on_shop=True, leave=True):
        boxes = []
        if on_shop:
            boxes.append(Box("移除卡牌", 1800, 350, 160, 48))  # 盖住 0.729, 0.261
        if credit is not None:
            boxes.append(Box(str(credit), 1980, 55, 120, 40))  # 盖住 0.794, 0.054
        if leave:
            boxes.append(Box("离开", 2340, 1290, 160, 48))  # 盖住 0.945, 0.918
        return boxes

    def run_shop(mode, credit, on_shop=True, leave=True, shop_flag=True):
        world, task = make("EMPTY", {}, [])
        task.name = mode
        task.node_status = {"shop": shop_flag}
        task.all_texts = shop_boxes(credit, on_shop, leave)
        returned = utils.handle_shop(task)
        left = any("退出商店" in line for line in task.logs)
        return returned, task.marks.get("shop_original", 0), left, task.node_status["shop"], len(world.clicks)

    returned, ran, left, shop_flag, clicks = run_shop("自动卡厄思模式", 62)
    check("卡厄思商店信用点小于80直接离开，不再购买",
          returned is True and ran == 0 and left and shop_flag is False and clicks == 1,
          f"返回{returned}，原逻辑调用{ran}次，离开={left}，shop={shop_flag}，点击{clicks}次")
    returned, ran, left, shop_flag, clicks = run_shop("自动卡厄思模式", 80)
    check("信用点等于80仍照原逻辑购买",
          returned is False and ran == 1 and not left and shop_flag is True and clicks == 0,
          f"返回{returned}，原逻辑调用{ran}次，离开={left}")
    returned, ran, left, shop_flag, clicks = run_shop("自动卡厄思模式", None)
    check("商店信用点读不到时不退出",
          returned is False and ran == 1 and not left and clicks == 0,
          f"返回{returned}，原逻辑调用{ran}次，离开={left}")
    returned, ran, left, shop_flag, clicks = run_shop("自动出击模式", 62)
    check("出击模式不套用卡厄思的商店退出",
          returned is False and ran == 1 and not left and shop_flag is True,
          f"返回{returned}，原逻辑调用{ran}次，离开={left}")
    returned, ran, left, shop_flag, clicks = run_shop("自动卡厄思模式", 62, on_shop=False)
    check("不在商店页时不因为信用点低而点离开",
          returned is False and ran == 1 and not left and clicks == 0,
          f"返回{returned}，原逻辑调用{ran}次，离开={left}")
    returned, ran, left, shop_flag, clicks = run_shop("自动卡厄思模式", 62, leave=False)
    check("信用点不足但离开按钮暂时没有时，本轮也不购买",
          returned is True and ran == 0 and left and shop_flag is False and clicks == 0,
          f"返回{returned}，原逻辑调用{ran}次，离开={left}，shop={shop_flag}")

    world, task = make("EMPTY", {}, [])
    task.name = "自动卡厄思模式"
    task.node_status = {"shop": False}
    task.all_texts = shop_boxes(232)
    first = utils.handle_shop(task)
    task.all_texts = shop_boxes(62)
    second = utils.handle_shop(task)
    check("每次商店操作结束后再检测，掉到80以下就离开",
          first is False and task.marks.get("shop_original") == 1 and second is True
          and any("退出商店" in line for line in task.logs) and task.node_status["shop"] is False,
          f"第一次返回{first}（原逻辑{task.marks.get('shop_original')}次），第二次返回{second}")

    import config_io
    panel = FakeTask(FakeExecutor(World("EMPTY")))
    panel.name = "自动卡厄思模式"
    speedup.install(panel)
    check("卡厄思配置面板有「刷新商店」开关，默认关闭",
          panel.default_config.get("刷新商店") is False
          and "免费" in panel.config_description.get("刷新商店", "")
          and "刷新商店" in config_io.UI_ONLY_CONFIG_KEYS,
          f"默认={panel.default_config.get('刷新商店')}")

    free = Box("免费", 340, 1316, 160, 48)  # 中心约 0.164, 0.931，落在原版刷新区域
    world, task = make("EMPTY", {}, [])
    task.name = "自动卡厄思模式"
    task.node_status = {"shop": True}
    task.all_texts = shop_boxes(232) + [free]
    task.marks["shop_click"] = "免费"
    returned = utils.handle_shop(task)
    check("刷新商店关闭时不点「免费」，交给离开",
          returned is False and task.marks.get("shop_original") == 1 and len(world.clicks) == 0
          and any("刷新商店已关闭" in line for line in task.logs),
          f"返回{returned}，点击{len(world.clicks)}次")

    world, task = make("EMPTY", {}, [])
    task.name = "自动卡厄思模式"
    task.config["刷新商店"] = True
    task.node_status = {"shop": True}
    task.all_texts = shop_boxes(232) + [free]
    task.marks["shop_click"] = "免费"
    returned = utils.handle_shop(task)
    check("刷新商店打开时仍点「免费」",
          returned is True and len(world.clicks) == 1,
          f"返回{returned}，点击{len(world.clicks)}次")

    buy = Box("信用点", 1200, 1100, 80, 40)
    world, task = make("EMPTY", {}, [])
    task.name = "自动卡厄思模式"
    task.node_status = {"shop": True}
    task.all_texts = shop_boxes(232) + [buy]
    task.marks["shop_click"] = "信用点"
    returned = utils.handle_shop(task)
    check("关闭刷新不影响购买",
          returned is True and len(world.clicks) == 1,
          f"返回{returned}，点击{len(world.clicks)}次")

    def decompose_task(checked):
        world, task = make("EMPTY", {}, [])
        label_x, label_y, label_h = 1155, 791, 32
        task.all_texts = [
            Box("分解存档资料", 1100, 430, 360, 48),
            Box("下次登入前不再显示", label_x, label_y, 360, label_h),
            Box("确认", 1500, 920, 200, 48),
        ]
        frame = np.full((H, W, 3), 230, dtype=np.uint8)
        cx = int(label_x - 0.029 * W)
        cy = int(label_y + label_h / 2)
        if checked:
            frame[cy - 23:cy + 23, cx - 23:cx + 23] = (40, 120, 220)  # BGR 橙色
        task.executor._frame = frame
        task.marks["checkbox"] = (cx, cy)
        return world, task, cx, cy

    world, task, cx, cy = decompose_task(False)
    clicked = speedup._ensure_decompose_checkbox(task)
    hit = world.clicks[-1][2:] if world.clicks else None
    check("分解存档确认框未勾选时先点勾选框",
          clicked is True and hit is not None and abs(hit[0] - cx) <= 2 and abs(hit[1] - cy) <= 2,
          f"点击{hit}，期望({cx}, {cy})")
    world, task, cx, cy = decompose_task(True)
    clicked = speedup._ensure_decompose_checkbox(task)
    check("分解存档确认框已勾选时不再点，避免取消勾选",
          clicked is False and not world.clicks,
          f"点击了{len(world.clicks)}次")
    world, task = make("EMPTY", {}, [])
    task.all_texts = [Box("确认", 1500, 920, 200, 48)]
    task.executor._frame = np.full((H, W, 3), 230, dtype=np.uint8)
    clicked = speedup._ensure_decompose_checkbox(task)
    check("不是分解存档确认框时不点勾选框",
          clicked is False and not world.clicks,
          f"返回{clicked}")

    print(f"\n{sum(results)}/{len(results)} 项通过")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
