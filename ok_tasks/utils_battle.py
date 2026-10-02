"""
自动出击模式的战斗出牌：读画面（手牌费用/类型、剩余费用、我方血量与预计扣血、敌人血量/护盾/行动倒计时/意图），
按规则挑一张牌，按键或拖到目标身上打出。术语见仓库根目录 CONTEXT.md。

出牌规则（每次只出一张，出完下一帧重新观察）：
1. 只考虑出得起的牌：读到费用的按费用判断；读不到的先试着出，没打出去（手牌数和 AP 都没减少）就本回合不再出。
2. 顺序：崩溃牌 → 预计挨打后会被打死或血量低于「防御血量线」时先出防御牌 → 「出牌优先级」里的牌（按列表顺序）→ 0 费牌 → 强化牌 → 攻击牌
   → 技能/防御/其余牌；同一类牌里按出牌优先级、再按费用从低到高。
3. 没有出得起的牌就按 E 结束回合。
攻击牌拖到目标身上打出（见 docs/adr/0001）：Boss 战优先打 Boss；否则集中打同一个敌人直到它死，
第一个目标按「攻击意图优先、行动倒计时小的优先、血少的优先」挑。非攻击牌仍用数字键 + 回车。

注意：本文件不能定义顶层类，框架会把 ok_tasks 下含类的 .py 当作任务加载。
"""
import collections
import glob
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

import battle_log
import config_io
from ok import Box
from utils import _get_config_value, _move_and_click, _normalize_text, _simplify_texts, close_monster_panel

DEFENSE_KEY = "防御卡牌列表"
COLLECT_KEY = "意图采集"
DANGER_KEY = "防御血量线(%)"

ICON_DIR = os.path.join("configs", "battle_icons")
INTENT_ATTACK, INTENT_DEFENSE, INTENT_BUFF = "攻击", "防御", "增益"

# ---- 画面坐标（相对屏幕，基准 16:9） ----
_REMAIN_REGION = (0.47, 0.88, 0.53, 0.96)          # 手牌下方中央的剩余费用
_AP_DIGIT_BOX = (0.488, 0.903, 0.513, 0.953)       # 剩余费用数字本身（比 _REMAIN_REGION 紧，不含上方卡面）
_HP_TEXT_REGION = (0.10, 0.0, 0.33, 0.07)          # 我方血量「当前/上限」
_SHIELD_REGION = (0.40, 0.0, 0.48, 0.09)           # 我方护盾数值
_HP_BAR_X = (0.015, 0.42)                          # 我方血条横向范围
_HP_BAR_ROWS = (0.034, 0.038, 0.042)               # 我方血条取样的行
_HP_BAR_LEFT = 0.022                               # 我方血条左端
_ENEMY_AREA = (0.30, 0.0, 1.0, 0.62)               # 敌人血条可能出现的范围
_CARD_COST_BOX = (-0.030, -0.016, 0.004, 0.050)    # 费用数字相对牌名左上角的范围
_CARD_TYPE_BOX = (-0.012, 0.004, 0.05, 0.055)     # 类型标签相对牌名左上角的范围；右边界太宽会拿到相邻牌的标签
_PANEL_REGION = (0.02, 0.02, 0.46, 0.46)           # 怪物信息面板
_WEAKNESS_REGION = (0.30, 0.07, 0.45, 0.15)        # 怪物信息面板标题栏右侧的「弱点」：看到它说明面板开着
_WEAKNESS = re.compile("弱[点點]")
_PLAYED_BANNER = (0.0, 0.40, 0.25, 0.52)           # 打出一张牌后左侧显示牌名的横幅

_DIGITS = re.compile(r"^\d{1,2}$")
_ONE_DIGIT = re.compile(r"^\d$")
_NUMBER = re.compile(r"^\d{1,6}$")
_ICON_THRESHOLD = 0.45     # 白色笔画重合度：已收集的图标里不同类别之间最高 0.31
_GLYPH_FRAC = 0.75
_GLYPH_MIN_PIXELS = 30
_AP_SHORT = re.compile(r"AP\s*不足", re.IGNORECASE)
# 牌名里带这些字的非攻击牌算保命牌（加护盾、回血）
_SHIELD_WORDS = ("盾", "格挡", "壁", "屏障", "防御", "防护", "守护")
_DEFENSE_WORDS = _SHIELD_WORDS + ("治", "愈", "疗", "恢复", "回复", "再生", "包扎")
_EXTRA_WAIT_CARDS = ("极光", "万众英雄")  # 打出后动画较长，沿用原逻辑额外等 2 秒
_STUCK_LIMIT = 3            # 同一张牌连续这么多次还在手里（又没提示 AP不足），本回合不再出它
_BUTTON_GONE_LIMIT = 30     # 「结束回合」按钮消失这么多秒还没回来，就不再当作敌人行动中干等
_DRAG_FAIL_LIMIT = 2        # 拖动连续这么多次没打出去，本场改用按键 + 回车打默认目标
_TURN_FAIL_LIMIT = 3        # 同一张牌一回合里不论按键、拖动，总共这么多次没打出去就不再出它（兜底，防止换着法子一直试）
_KEYS_DEAD_CARDS = 2        # 这么多张不同的牌按键没打出去、拖动打出去了，才算键盘失效（单张牌常是自身原因）
_DRAG_STEPS = 12            # 后台拖动中途发几次鼠标移动
_DRAG_STEP_INTERVAL = 0.02
_DRAG_HOVER = 0.3           # 拖到目标上停多久再松手，让游戏锁定目标
# 实跑 17:59~18:45：游戏不再理会后台发的按键（数字键出牌、E 都没反应），鼠标拖动、点击照常有效，卡了 45 分钟。
# 按键出牌没打出去时先拖动再试一次，拖动打出去了就认定本场键盘失效，之后出牌、结束回合都用鼠标
_FIELD_DROP = (0.5, 0.45)   # 不用选目标的牌拖到场地中间松手
_END_TURN_POINT = (0.89, 0.867)  # 右下角「结束回合」按钮
_E_KEY_LIMIT = 2            # 同一回合按了这么多次 E 按钮还在，之后每次再用鼠标点一下按钮
_WM_MOUSEMOVE, _WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON = 0x0200, 0x0201, 0x0202, 0x0001
_RED_SAMPLES = 4            # 读预计扣血时连续取样的帧数（红色段是闪烁的）
_RED_INTERVAL = 0.25
_HP_GREEN_MIN = 5           # 血条一行里绿色像素少于这个数算没有绿色（剩 5% 血时实测约 40 像素）
_ZERO_HP_LAST_RATIO = 0.15  # 读不到血量时，本场上一次读到的血量低于上限的这个比例，才可能是打到 0 了
_COLLAPSED_RATIO = 0.3     # 血条一行里紫色像素超过这个比例算崩溃（血量为 0）
_EGO_SLOTS = (("F1", 0.714), ("F2", 0.817), ("F3", 0.904))  # 左下角三个 Ego 头像上费用框的中心 y
_EGO_COST_X = (0.057, 0.070)   # 费用框横向范围（取框内底色，不含边框）
_EGO_READY = 0.4               # 费用框浅青色像素占比超过这个值：放得起（实测 0.60~0.84；放不起是灰色、空槽是暗的，都为 0）
_EGO_TRIES = 2                 # 同一个 Ego 每回合最多按几次（按了没放出去就不再反复按）
_EP_FULL_POINT = (0.032, 0.947)  # EP 条最下面一格：亮了就是满格
_AP_ZERO_GRAY = 0.3         # 剩余费用数字区域灰色像素超过这个比例：AP 为 0（灰色空心的「0」，实测 0.42~0.50，白色数字 ≤0.001）
# 有的牌打出后会把牌移回手牌、抽牌或生成新牌，新牌要过一两秒才到手。以前出牌后读得太早，
# 按旧的手牌排位按键，按到的是另一张牌或空位（实跑 15:40:27、15:43:09 都是读到的手牌数比实际少）。
# 只看手牌数「N/10」：手牌区画面平时一直有动画，比截图判断静止时每张牌都等满上限（实跑 16:02 一局 33 次），
# 出牌前比截图也常误报变化 10%~26%
_COUNT_BOX = (0.470, 0.950, 0.560, 0.995)  # 手牌数「N/10」
_COUNT_TEXT = re.compile(r"(\d+)\s*/\s*1[0O]")
_SETTLE_MIN = 1.0           # 出牌后至少等这么久（原来固定等 1 秒）
_SETTLE_STABLE = 0.6        # 手牌数连续这么久没变才算停稳
_SETTLE_MAX = 2.5           # 最多等这么久
_SETTLE_POLL = 0.15
_SETTLE_MISSING = 3         # 连续几次读不到手牌数：多半弹出了选择页面，不再等
_STALE_LIMIT = 2            # 出牌前发现手牌数变了，最多连续重读几次，之后照常出牌
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="出牌识别")  # 几块区域的裁剪识别并行跑
_OCR_LOCK = threading.Lock()


def install(task):
    """给出击模式加上出牌相关配置项。"""
    task.default_config[DEFENSE_KEY] = []
    task.default_config[COLLECT_KEY] = False
    task.default_config[DANGER_KEY] = 25
    task.config_description[DEFENSE_KEY] = "保命牌（加护盾、回血）：牌名带「盾」「格挡」「壁」「治」「疗」「恢复」等字的会自动认出，这里只需补充认不出的牌；预计这回合会被打死、或挨打后血量低于「防御血量线」时先出这些牌"
    task.config_description[DANGER_KEY] = "预计敌人这一轮打完后我方血量低于上限的百分之几，就先出保命牌；0 为只在会被打死时才出"
    task.config_description[COLLECT_KEY] = "前期收集数据用：遇到没见过的意图图标时点开怪物信息面板读出意图并记下图标，会变慢；图标收集够后关闭"
    config_io.UI_ONLY_CONFIG_KEYS.add(COLLECT_KEY)


# ======================================================================
# 读画面
# ======================================================================

def _rel(task, box):
    """文字框中心的相对坐标。"""
    return (box.x + box.width / 2) / task.width, (box.y + box.height / 2) / task.height


def _crop(frame, region):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = region
    return frame[max(0, int(y1 * h)):max(0, int(y2 * h)), max(0, int(x1 * w)):max(0, int(x2 * w))]


def _variants(crop):
    """同一块区域的几种预处理：费用、倒计时这类数字压在彩色背景上，不同画面下各有读得出的处理方式。"""
    if crop is None or crop.size == 0:
        return
    yield cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    yield cv2.cvtColor(cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC), cv2.COLOR_GRAY2BGR)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 70)).astype(np.uint8) * 255
    white = cv2.resize(white, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    yield cv2.cvtColor(255 - white, cv2.COLOR_GRAY2BGR)


def _ocr_texts(task, image):
    try:
        # OpenVINO 的识别模型同一时间只能处理一个请求（并发会抛 Infer Request is busy），
        # 所以裁剪、预处理在线程池里并行，调用 OCR 这一步排队
        with _OCR_LOCK:
            boxes = task.ocr(frame=image)
        return [box.name.strip() for box in boxes]
    except Exception as e:  # OCR 失败不影响出牌，按读不到处理
        task.log_info(f"战斗识别 OCR 失败：{e}")
        return []


def _read_digit(task, frame, region, pattern=_DIGITS, votes=1):
    """对一块区域依次尝试几种预处理，读出整段都是数字的读数；读不到返回 None。
    votes > 1 时要有这么多种预处理读出同一个数才算数（单张牌的费用读错代价大：出得起的牌会被整回合跳过）。"""
    readings = []
    for image in _variants(_crop(frame, region)):
        value = next((int(t) for t in (s.replace(" ", "") for s in _ocr_texts(task, image)) if pattern.match(t)), None)
        if value is None:
            continue
        if votes <= 1:
            return value
        readings.append(value)
        if readings.count(value) >= votes:
            return value
    return None


def read_remaining_cost(task, frame):
    return _read_digit(task, frame, _REMAIN_REGION)


def ap_zero_outline(frame):
    """剩余费用为 0 时游戏把数字画成灰色空心的「0」，中间透出后面的卡面，OCR 怎么处理都读不出来；
    1 以上是白色实心数字。按颜色判断：数字区域里灰色像素占比高就是 0。"""
    crop = _crop(frame, _AP_DIGIT_BOX)
    if crop is None or crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gray = (hsv[:, :, 2] > 95) & (hsv[:, :, 2] < 185) & (hsv[:, :, 1] < 35)
    return gray.mean() >= _AP_ZERO_GRAY


def in_danger(hp, after, line):
    """挨完这一轮打，剩余血量是否低于上限的 line%。after 是 incoming_lethal 读出的「剩余血量 / 当前血量」。"""
    if not hp or not line or after >= 1:
        return False
    return after * hp[0] < hp[1] * line / 100


def ap_insufficient(task):
    return any(_AP_SHORT.search(box.name) for box in task.all_texts)


def read_hp(task):
    """我方血量 (当前, 上限)；读不到返回 None。"""
    x1, y1, x2, y2 = _HP_TEXT_REGION
    for box in task.all_texts:
        cx, cy = _rel(task, box)
        match = re.search(r"(\d+)\s*/\s*(\d+)", box.name)
        if match and x1 <= cx <= x2 and y1 <= cy <= y2:
            current, maximum = int(match.group(1)), int(match.group(2))
            if 0 < maximum and current <= maximum:  # 实跑中读出过 6177/1768、16657/1667（多读了一位）
                return current, maximum
    return None


def read_shield(task):
    x1, y1, x2, y2 = _SHIELD_REGION
    for box in task.all_texts:
        cx, cy = _rel(task, box)
        if _NUMBER.match(box.name.strip()) and x1 <= cx <= x2 and y1 <= cy <= y2:
            return int(box.name.strip())
    return 0


def hp_bar_red(frame):
    """量我方血条：返回 (红色段起点, 当前血量末端)，都是相对屏幕的 x；没有红色段时起点为 None。"""
    h, w = frame.shape[:2]
    x0 = int(_HP_BAR_X[0] * w)
    red_start, end = None, None
    for row_y in _HP_BAR_ROWS:
        row = cv2.cvtColor(frame[int(row_y * h):int(row_y * h) + 1, x0:int(_HP_BAR_X[1] * w)], cv2.COLOR_BGR2HSV)[0]
        green = (row[:, 0] > 35) & (row[:, 0] < 90) & (row[:, 1] > 80) & (row[:, 2] > 80)
        red = ((row[:, 0] < 12) | (row[:, 0] > 165)) & (row[:, 1] > 60) & (row[:, 2] > 80)
        gi, ri = np.where(green)[0], np.where(red)[0]
        row_end = max([i.max() for i in (gi, ri) if len(i)], default=None)
        if row_end is not None:
            end = max(end or 0, (row_end + x0) / w)
        if len(ri) >= 3:
            start = (ri.min() + x0) / w
            red_start = start if red_start is None else min(red_start, start)
    return red_start, end


def hp_bar_empty(frame):
    """我方血条上一格绿色都没有（血量为 0）。"""
    h, w = frame.shape[:2]
    x0, x1 = int(_HP_BAR_X[0] * w), int(_HP_BAR_X[1] * w)
    for row_y in _HP_BAR_ROWS:
        row = cv2.cvtColor(frame[int(row_y * h):int(row_y * h) + 1, x0:x1], cv2.COLOR_BGR2HSV)[0]
        green = (row[:, 0] > 35) & (row[:, 0] < 90) & (row[:, 1] > 80) & (row[:, 2] > 80)
        if green.sum() >= _HP_GREEN_MIN:
            return False
    return True


def hp_bar_collapsed(frame):
    """我方血条变成紫色乱码（血量为 0 的崩溃状态）：至少两行大半是紫色。实测崩溃时每行紫色占 75% 以上，其余截图不到 5%。"""
    h, w = frame.shape[:2]
    x0, x1 = int(_HP_BAR_X[0] * w), int(_HP_BAR_X[1] * w)
    rows = 0
    for row_y in _HP_BAR_ROWS:
        row = cv2.cvtColor(frame[int(row_y * h):int(row_y * h) + 1, x0:x1], cv2.COLOR_BGR2HSV)[0]
        purple = (row[:, 0] >= 130) & (row[:, 0] <= 165) & (row[:, 1] > 100) & (row[:, 2] > 120)
        rows += purple.sum() >= _COLLAPSED_RATIO * (x1 - x0)
    return rows >= 2


def is_zero_hp(hp, last_hp, frame):
    """我方血量是否为 0：读到 0 就是；实跑中血量打到 0 后血量文字读不出来（记录里 hp 为 null），
    这时要本场上一次读到的血量已经很低、且血条上没有绿色，才当成 0（文字被别的界面挡住时不误判）。
    带着 0 血进入下一场战斗时本场没有读到过血量（实跑 17:48 战斗 4），血条变紫也算 0。"""
    if hp:
        return hp[0] == 0
    if frame is None or not hp_bar_empty(frame):
        return False
    if last_hp and last_hp[0] <= last_hp[1] * _ZERO_HP_LAST_RATIO:
        return True
    return hp_bar_collapsed(frame)


def incoming_lethal(task):
    """预计扣血会不会把我方打死。红色段是闪烁的，连续取几帧，只要有一帧红色段一直延伸到血条左端就算会死。
    返回 (是否致命, 预计剩余血量比例)；一帧红色都没看到时比例为 1。"""
    worst = 1.0
    for i in range(_RED_SAMPLES):
        if i:
            task.sleep(_RED_INTERVAL)
            task.next_frame()
        frame = task.frame
        if frame is None:
            break
        red_start, end = hp_bar_red(frame)
        if red_start is not None and end and end > _HP_BAR_LEFT:
            worst = min(worst, max(0.0, (red_start - _HP_BAR_LEFT) / (end - _HP_BAR_LEFT)))
    return bool(worst <= 0.01), float(worst)


_HAND_CENTER = 0.471        # 手牌扇形正中那张牌的牌名左端 x
_HAND_AREA = (0.15, 0.68, 0.86, 0.905)
_HAND_STRIP = (0.15, 0.66, 0.86, 0.95)   # 补读牌名时裁剪的手牌区
_LOWERED_LABEL_Y = 0.83     # 类型标签最高的一个都在这以下：手牌沉下去了（实测正常 ≤0.79，沉下去 ≥0.87）
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿"


def hand_slots(count):
    """手牌按张数排成固定的扇形：返回每张牌牌名左端的 x（从左到右，第 i 张的按键是 i+1，第 10 张是 0）。
    间距按实测（3/5/7/10 张为 0.1375/0.113/0.0808/0.0561）拟合：张数少时不重叠，多了按 1/(1.79n-0.1) 收紧。"""
    if not count:
        return []
    spacing = min(0.1375, 1 / (1.79 * count - 0.1))
    return [_HAND_CENTER + (i - (count - 1) / 2) * spacing for i in range(count)]


def _hand_strip_boxes(task):
    """手牌区裁出来放大两倍 OCR，文字框换回整屏坐标、繁体转简体（与 task.all_texts 一致）。"""
    x1, y1, x2, y2 = _HAND_STRIP
    image = cv2.resize(_crop(task.frame, _HAND_STRIP), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    sx, sy = task.width / task.frame.shape[1] / 2, task.height / task.frame.shape[0] / 2
    try:
        with _OCR_LOCK:
            found = task.ocr(frame=image)
    except Exception as e:  # OCR 失败不影响出牌，按读不到处理
        task.log_info(f"手牌区补读 OCR 失败：{e}")
        return []
    boxes = [Box(int(b.x * sx + x1 * task.width), int(b.y * sy + y1 * task.height), int(b.width * sx),
                 int(b.height * sy), confidence=b.confidence, name=b.name) for b in found]
    return _simplify_texts(boxes)


def hand_lowered(task):
    """AP 用完后整排手牌会沉下去并变暗：牌上的类型标签从 y≈0.78 降到 0.87 以下。
    这时剩余费用那个灰色的「0」OCR 怎么处理都读不出来，靠这个判断 AP 已经用完。"""
    x1, _, x2, _ = _HAND_AREA
    labels = [(b.y + b.height / 2) / task.height for b in task.all_texts
              if x1 <= b.x / task.width <= x2 and b.y / task.height > 0.6 and len(b.name.strip()) <= 8
              and _type_of(b.name)]
    return bool(labels) and min(labels) > _LOWERED_LABEL_Y


def read_hand(task, count):
    """按位置读手牌：第几个位置就是按键几，不依赖 OCR 读出牌上方的按键数字（经常漏读，漏一个后面的就全错位）。
    每张牌的名字、类型取落在该位置上的文字；名字没读到时仍保留这张牌（名字记为「未识别N」），照样可以按键出。
    返回 [{name, key, x, y, type, cost_hint}]。"""
    slots = hand_slots(count)
    if not slots:
        return []
    spacing = slots[1] - slots[0] if len(slots) > 1 else 0.1375
    x1, y1, x2, y2 = _HAND_AREA
    names = [[] for _ in slots]
    types = [[] for _ in slots]
    progress = [None for _ in slots]

    def collect(boxes, only=None):
        for box in boxes:
            left, top = box.x / task.width, box.y / task.height
            cy = (box.y + box.height / 2) / task.height
            if not (x1 <= left <= x2 and y1 <= cy <= y2):
                continue
            index = min(range(len(slots)), key=lambda i: abs(slots[i] - left))
            if abs(slots[index] - left) > 0.45 * spacing + 0.012 or (only is not None and index not in only):
                continue
            text = box.name.strip()
            # 崩溃牌在费用位置显示「已打张数/所需张数」，已打张数常被漏读（只读到「/5」），读不到记为 None
            # 「1/5」常被读成「17/5」「175」：只有干净的「数字/数字」才采信已打张数，所需张数取「崩」字前那一位
            clean = re.match(r"^(\d)\s*/\s*(\d)(?!\d)", text)
            total = re.search(r"(\d)\s*崩", text)
            if clean:
                progress[index] = (int(clean.group(1)), int(clean.group(2)))
            elif total:
                progress[index] = (None, int(total.group(1)))
            card_type = _type_of(text) if len(text) <= 8 else None
            if card_type:
                types[index].append(card_type)
                continue
            hint = re.match(r"^(\d)(?=\D)", text)  # 「3禿鷹髮」：费用数字和牌名连成了一个框
            cleaned = re.sub(rf"^[\d{_CIRCLED}\s]+", "", text)
            # 牌名后的括号词条「（极强）」「（屠戮）」和等级「Lv.3」不是牌名；括号词条常被单独读成一个框，
            # 以前按「最长的文字」取牌名，「破碎」被同一位置上的「（屠戮)」顶掉，出牌优先级就匹配不上了
            cleaned = re.sub(r"[（(][^）)]*[）)]?|\s*L[Vv]\.?\s*\d*$", "", cleaned)
            cleaned = re.sub(r"[A-Za-z\s]+$", "", cleaned).strip()  # 牌名都是中文，末尾的字母是杂字（「鞭J」）
            # 「攻」「技」开头的是没认出来的类型标签（「技育」「攻雪」），不是牌名
            if len(cleaned) >= 2 and not re.search(r"\d+\s*/\s*\d+", cleaned) and cleaned[:1] not in ("攻", "技") \
                    and cleaned not in ("基本", "基础"):  # 「基本攻击」被拆开时剩下的「基本」不是牌名
                names[index].append((len(cleaned), cleaned, top, int(hint.group(1)) if hint else None))

    collect(task.all_texts)
    missing = {i for i in range(len(slots)) if not names[i]}
    if missing and getattr(task, "frame", None) is not None:
        # 整屏 OCR 漏读的牌名（压在亮色卡图上、或 AP 用完后整排手牌变暗）：手牌区单独裁出来放大再读一次，只补没读到的位置
        collect(_hand_strip_boxes(task), missing)
    cards = []
    for i, x in enumerate(slots):
        best = max(names[i]) if names[i] else None
        cards.append({
            "name": best[1] if best else f"未识别{i + 1}",
            "key": str((i + 1) % 10),
            "x": x, "left_x": x,
            "y": best[2] if best else None,
            "type": types[i][0] if types[i] else None,
            "cost_hint": best[3] if best else None,
            "progress": progress[i],
        })
    return cards


def _remember_type(task, card):
    """类型标签时有时无（实跑：电浆飞弹上一帧读到「攻击」，下一帧没读到，被当成不用选目标的牌拖到场地中间打不出去）。
    读到就按牌名记下，读不到时沿用本次运行读到过的；牌名读残了（「飞弹」）按包含关系找。"""
    name = card["name"]
    if name.startswith("未识别"):
        return
    types = _session(task).setdefault("types", {})
    if card["type"] is not None:
        types[name] = card["type"]
    elif name in types:
        card["type"] = types[name]
    elif len(name) >= 2:
        found = {t for known, t in types.items() if name in known or known in name}
        if len(found) == 1:
            card["type"] = found.pop()


def _card_type(task, card):
    """牌名右下方的类型标签：攻击 / 技能 / 强化 / 咒术 / 状态异常；读不到返回 None。"""
    dx1, dy1, dx2, dy2 = _CARD_TYPE_BOX
    found = []
    for box in task.all_texts:
        dx, dy = box.x / task.width - card["x"], box.y / task.height - card["y"]
        if not (dx1 <= dx <= dx2 and dy1 <= dy <= dy2):
            continue
        card_type = _type_of(box.name)
        if card_type:
            found.append((abs(dx) + abs(dy - 0.03), card_type))
    # 手牌挤在一起时相邻牌的标签也可能落进范围，取离牌名最近的
    return min(found)[1] if found else None


def _type_of(text):
    """类型标签文字 → 卡牌类型。要求完整的词：「破碎（极强）」里的「强」不是「强化」。「攻」单独成立是因为「攻击」常被读成「攻雪」「攻撃」。"""
    if "崩" in text:  # 「崩潰」常被读成「崩清」「崩潢」，只认「崩」字
        return "崩溃"
    if "攻" in text:
        return "攻击"
    for word, card_type in (("技能", "技能"), ("强化", "强化"), ("咒术", "咒术"), ("状态", "状态异常"), ("异常", "状态异常")):
        if word in text:
            return card_type
    return None


def _card_cost(task, frame, card):
    if card.get("y") is None:
        return card.get("cost_hint")
    dx1, dy1, dx2, dy2 = _CARD_COST_BOX
    region = (card["x"] + dx1, card["y"] + dy1, card["x"] + dx2, card["y"] + dy2)
    cost = _read_digit(task, frame, region, _ONE_DIGIT, votes=2)
    return cost if cost is not None else card.get("cost_hint")


def enemy_bars(frame):
    """按颜色找敌人血条（细长的洋红色横条），返回 [(左端 x, 中心 y, 宽度)]，从左到右排列。"""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = _ENEMY_AREA
    area = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
    mask = cv2.inRange(cv2.cvtColor(area, cv2.COLOR_BGR2HSV), (150, 120, 120), (178, 255, 255))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    bars = []
    for i in range(1, n):
        x, y, bw, bh, pixels = stats[i]
        # 血条填充率约 0.8~0.9（压缩后边缘发虚会略低）；Boss 身上的洋红色碎块只有 0.3 左右
        if bh < 0.003 * h or bh > 0.013 * h or bw < 3 * bh or pixels < 0.7 * bw * bh:
            continue
        bars.append(((x + int(x1 * w)) / w, (y + int(y1 * h) + bh / 2) / h, bw / w))
    return sorted(bars)


def _looks_like_infinity(crop):
    """菱形里的白色字形：∞ 横着宽（宽高比约 2），8 竖着高（约 0.6）。"""
    if crop is None or crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 70)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(white)
    if n <= 1:
        return False
    x, y, w, h, _ = stats[1:][np.argmax(stats[1:, cv2.CC_STAT_AREA])]
    return h > 0 and w / h > 1.2


def _numbers_near(task, x1, y1, x2, y2):
    found = []
    for box in task.all_texts:
        text = box.name.strip()
        if not _NUMBER.match(text):
            continue
        cx, cy = _rel(task, box)
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            found.append((cx, cy, int(text)))
    return found


def read_enemies(task, frame):
    """每个敌人：血量、护盾、行动倒计时（∞ 或读不到为 None）、意图、拖牌落点、意图图标的位置。"""
    enemies = []
    blocked = [_rel(task, b) for b in task.all_texts if "无法攻击" in b.name]
    used = set()
    for left, bar_y, bar_w in enemy_bars(frame):
        if any(left - 0.02 <= bx <= left + bar_w + 0.02 and bar_y - 0.05 <= by <= bar_y + 0.01 for bx, by in blocked):
            continue  # 「无法攻击」的单位（如地上的捕兽夹）不能当目标
        near = _numbers_near(task, left - 0.005, bar_y - 0.05, left + 0.17, bar_y + 0.005)
        if not near:
            # 全屏 OCR 偶尔漏掉血量数字（如 Boss 的 7441）：单独裁出血条上方再读一次
            hp_value = _read_digit(task, frame, (left, bar_y - 0.045, left + 0.14, bar_y - 0.002), _NUMBER)
            if hp_value is None:
                continue
            near = [(left + 0.055, bar_y - 0.012, hp_value)]
        hp = min(near, key=lambda n: abs(n[0] - (left + 0.055)) + abs(n[1] - (bar_y - 0.012)))
        if (hp[0], hp[1]) in used:
            continue  # Boss 的长血条会被切成几段，都挨着同一个血量数字：只算最左边那段（血条是从左往右排的）
        used.add((hp[0], hp[1]))
        shields = [n for n in near if n is not hp and n[0] > hp[0] + 0.03]
        diamond = (left - 0.021, bar_y + 0.006)
        diamond_region = (diamond[0] - 0.026, diamond[1] - 0.036, diamond[0] + 0.024, diamond[1] + 0.036)
        countdown = _read_digit(task, frame, diamond_region)
        infinite = countdown == 8 and _looks_like_infinity(_crop(frame, diamond_region))
        if infinite:
            countdown = None  # ∞（常见于 Boss）会被 OCR 读成 8：看字形宽高比区分
        icon_region = (diamond[0] - 0.02, diamond[1] + 0.028, diamond[0] + 0.02, diamond[1] + 0.085)
        enemies.append({
            "x": round(left, 3), "y": round(bar_y, 3), "hp": hp[2],
            "shield": shields[0][2] if shields else 0,
            "countdown": countdown,
            "infinite": infinite,
            "icon_region": icon_region,
            "intent": match_intent(_crop(frame, icon_region)),
            "drop": (min(0.97, hp[0]), min(0.62, bar_y + 0.2)),
            "hp_pos": (hp[0], hp[1]),
        })
    return enemies


# ---- 意图图标库 ----

_icon_cache = {"stamp": None, "icons": []}


def _load_icons():
    paths = sorted(glob.glob(os.path.join(ICON_DIR, "*", "*.png")))
    stamp = tuple((p, os.path.getmtime(p)) for p in paths)
    if stamp != _icon_cache["stamp"]:
        icons = []
        for path in paths:
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                icons.append((os.path.basename(os.path.dirname(path)), image))
        _icon_cache.update(stamp=stamp, icons=icons)
    return _icon_cache["icons"]


def intent_glyph(crop):
    """意图图标的白色笔画（剑、盾等），只取截图正中的一块：图标周围常常挤着别的状态图标、血条和背景，
    整块比对时同一个图标两次截下来相似度只有 -0.1~0.5。返回 32x32 布尔图；白色笔画太少（截偏了）返回 None。"""
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    side = int(min(h, w) * _GLYPH_FRAC)
    x0, y0 = (w - side) // 2, (h - side) // 2
    hsv = cv2.cvtColor(crop[y0:y0 + side, x0:x0 + side], cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 2] > 190) & (hsv[:, :, 1] < 60)).astype(np.uint8)
    glyph = cv2.resize(white, (32, 32), interpolation=cv2.INTER_AREA) > 0.3
    return glyph if glyph.sum() >= _GLYPH_MIN_PIXELS else None


def match_intent(crop):
    """拿意图图标去比对本地图标库（白色笔画的重合度），返回类别（攻击/防御/增益）；认不出返回 None。"""
    glyph = intent_glyph(crop)
    if glyph is None:
        return None
    best, best_score = None, _ICON_THRESHOLD
    for category, icon in _load_icons():
        known = intent_glyph(icon)
        if known is None:
            continue
        union = (glyph | known).sum()
        score = (glyph & known).sum() / union if union else 0.0
        if score >= best_score:
            best, best_score = category, score
    return best


def save_intent_icon(crop, category, move_name):
    """存进图标库；截偏了（中间没有白色笔画）的不存，返回 None。"""
    if intent_glyph(crop) is None:
        return None
    os.makedirs(os.path.join(ICON_DIR, category), exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]', "", move_name or "未知")[:20]
    path = os.path.join(ICON_DIR, category, f"{safe}_{int(time.time() * 1000)}.png")
    ok, data = cv2.imencode(".png", crop)
    if ok:
        data.tofile(path)
    return path


def classify_intent_panel(lines):
    """从怪物信息面板的文字里找出意图：写着触发条件（「行动次数N次后触发」「回合结束时触发」等）的那一块。
    lines 为 [(文字, 相对 y)]，文字已转简体。返回 (类别, 招式名, 触发次数, 伤害)，不按次数触发时触发次数为 None；
    找不到意图返回 None。"""
    trigger = next(((t, y) for t, y in lines if "触发" in t), None)
    if trigger is None:
        return None
    times = re.search(r"行动次数\s*(\d+)", trigger[0])
    count = int(times.group(1)) if times else None
    above = [(t, y) for t, y in lines if trigger[1] - 0.06 <= y < trigger[1] - 0.005]
    move = above[-1][0] if above else ""
    block = " ".join(t for t, y in lines if trigger[1] - 0.005 <= y <= trigger[1] + 0.07)
    damage = re.search(r"\((\d+)\)\s*的?伤害", block)
    if "伤害" in block:
        category = INTENT_ATTACK
    elif "护盾" in block or "防御" in block:
        category = INTENT_DEFENSE
    else:
        category = INTENT_BUFF
    return category, move, count, int(damage.group(1)) if damage else None


def collect_intent(task, enemy):
    """意图采集：点开怪物信息面板读出意图，把图标存进图标库，再关掉面板。
    返回 True 表示这个怪物本回合已行动（面板上没有意图）。"""
    frame = task.frame
    crop = _crop(frame, enemy["icon_region"]).copy()
    _move_and_click(task, *enemy["drop"])
    task.sleep(0.8)
    task.next_frame()
    x1, y1, x2, y2 = _PANEL_REGION
    lines = []
    for box in task.ocr(x1, y1, x2, y2):
        lines.append((_normalize_text(box.name), (box.y + box.height / 2) / task.height))
    lines.sort(key=lambda item: item[1])
    result = classify_intent_panel(lines)
    acted = result is None and any("已行动" in text or "行动完成" in text for text, _ in lines)
    if acted:
        task.log_info("意图采集：该怪物本回合已行动，面板上没有意图，跳过")
    elif result is None:
        battle_log.anomaly(task, "意图采集失败", f"点开敌人后没读到意图：{[t for t, _ in lines]}")
    else:
        category, move, count, damage = result
        path = save_intent_icon(crop, category, move)
        enemy["intent"] = category
        trigger = f"行动次数{count}次后触发" if count is not None else "非行动次数触发"
        task.log_info(f"意图采集：「{move}」{category}，{trigger}，伤害={damage}，图标存到 {path}")
        battle_log.record(task, "意图采集", move=move, category=category, trigger=count, damage=damage, icon=path)
    # 面板没打开就不点「关闭」：大 Boss 的身体会伸到关闭位置，点下去反而把面板点开（实跑中因此卡了两个小时）。
    # 关完再看一眼，还开着就换 ESC；剩下的交给 handle_weakness_info 轮流用两种办法关
    if not any(_WEAKNESS.search(text) for text, _ in lines):
        return acted
    for tries in range(2):
        close_monster_panel(task, tries)
        task.sleep(0.5)
        task.next_frame()
        if not any(_WEAKNESS.search(box.name) for box in task.ocr(*_WEAKNESS_REGION)):
            break
    return acted


# ======================================================================
# 出牌决策（纯函数，便于测试）
# ======================================================================

def _matches(name, candidates):
    return any(c and (c in name or name in c) for c in candidates)


def is_defense(card, defense):
    """保命牌：给我方加护盾或回血的牌。游戏里它们都标「基本技能/技能」，和别的技能分不开，
    所以按牌名认：带「盾」「格挡」「治」「疗」这类字的非攻击牌自动算，「防御卡牌列表」只用来补充认不出的牌。"""
    if _matches(card["name"], defense):
        return True
    return card["type"] != "攻击" and any(word in card["name"] for word in _DEFENSE_WORDS)


def _blocked(card, unplayable):
    """这张牌是否已被记为本回合出不起。按牌名比对时允许一方包含另一方：
    OCR 会在牌名前后多读出杂字（「黑暗斩击」「日黑暗斩击」「B黑暗斩击」是同一张牌）。
    牌名没读到的牌按「按键/手牌数」这个位置比对。"""
    if card.get("slot") in unplayable:
        return True
    name = card["name"]
    return not name.startswith("未识别") and _matches(name, [u for u in unplayable if len(u) >= 2 and "/" not in u])


def _one_char_off(a, b):
    """两个牌名是否只差一个字（同一位置读错，或多读/少读一个字）。"""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _priority_rank(card, priority):
    """在「出牌优先级」里的位置，越小越先出；不在列表里排最后。
    先按互相包含比对；都不中再允许错一个字（实跑中「苍白流星」读成「奢白流星」，被强化牌抢先），
    只对 3 个字以上的牌名放宽，免得短名误配。"""
    name_read = card["name"]
    for rank, name in enumerate(priority):
        if name and (name in name_read or name_read in name):
            return rank, name
    if not name_read.startswith("未识别"):
        for rank, name in enumerate(priority):
            if name and min(len(name), len(name_read)) >= 3 and _one_char_off(name, name_read):
                return rank, name
    return len(priority), None


def is_shield(card):
    """加护盾的牌（牌名带「盾」「格挡」「壁」等字的非攻击牌）。"""
    return card["type"] != "攻击" and any(word in card["name"] for word in _SHIELD_WORDS)


def choose_play(cards, remaining, priority, defense, danger, unplayable, zero_hp=False):
    """挑这一次要出的牌。cards 为 [{name, key, type, cost}]，cost 读不到为 None；remaining 读不到为 None。
    unplayable 里是本回合出不起的牌名或位置（「按键/手牌数」）。返回 (牌, 理由)；没有能出的牌时返回 (None, 理由)。

    顺序：崩溃牌 → 预计会被打死或血量过低（danger）时先出防御牌 → 「出牌优先级」里的牌（按列表顺序）→ 0 费牌 → 强化牌 → 攻击牌 → 技能/防御/其余牌。
    「出牌优先级」里的牌不看类型：类型常被读错（实跑中「破碎」读不出类型，排到了其余牌里，被普通攻击牌抢先）。
    zero_hp（我方血量为 0）：游戏进入特殊状态，护盾无效、崩溃牌打不出，再挨一次打就输。
    这时不出崩溃牌和加护盾的牌，也不走「先出防御牌」，AP 全留给其余的牌。"""
    def affordable(card):
        if _blocked(card, unplayable) or card.get("key") is None:
            return False
        if remaining is None:
            return True
        if card["cost"] is None:
            return remaining > 0  # 读不到费用：还有费用就试着出，出不去时会被记为出不起
        return card["cost"] <= remaining

    def pick(group, reason):
        """同一类牌里按出牌优先级、再按费用从低到高挑一张。"""
        if not group:
            return None
        card = min(group, key=lambda c: (_priority_rank(c, priority)[0], 99 if c["cost"] is None else c["cost"]))
        matched = _priority_rank(card, priority)[1]
        return card, reason + (f"，出牌优先级「{matched}」" if matched else "")

    # 1. 崩溃牌不花 AP、不计入敌人的行动次数：手里有就最先出，打够张数才能觉醒
    for card in cards:
        if zero_hp:
            break
        if card["type"] == "崩溃" and not _blocked(card, unplayable) and card.get("key") is not None:
            done = card.get("progress")
            shown = f"，进度 {'?' if done[0] is None else done[0]}/{done[1]}" if done else ""
            return card, f"崩溃牌（不花 AP，打够张数觉醒）{shown}"

    playable = [c for c in cards if affordable(c) and c["type"] != "崩溃" and not (zero_hp and is_shield(c))]
    if not playable:
        return None, "没有出得起的牌" + ("（血量为 0，不出护盾牌和崩溃牌）" if zero_hp else "")
    steps = (
        # 2. 这回合会被打死或打残：先把防御牌出了
        ([c for c in playable if is_defense(c, defense)] if danger and not zero_hp else [],
         "预计会被打死或血量过低，先出防御牌"),
        # 3. 用户在「出牌优先级」里指定的牌
        ([c for c in playable if _priority_rank(c, priority)[1]], "指定优先出的牌"),
        # 4. 0 费牌白出，可能带增益或抽牌
        ([c for c in playable if c["cost"] == 0], "0 费牌"),
        # 5. 强化牌先上，后面的攻击才吃得到加成
        ([c for c in playable if c["type"] == "强化"], "强化牌"),
        # 6. 攻击牌
        ([c for c in playable if c["type"] == "攻击" and not is_defense(c, defense)], "攻击牌"),
        # 7. 剩下的：技能、防御和认不出类型的牌
        (playable, "其余牌"),
    )
    for group, reason in steps:
        chosen = pick(group, reason)
        if chosen:
            return (chosen[0], "血量为 0，" + chosen[1]) if zero_hp else chosen
    return None, "没有出得起的牌"


def update_head(state, enemies):
    """Boss 战记住 Boss 的位置（Boss 不移动，打残后血量可能比新召唤的小怪还少，只看血量会转去打小怪）。
    认准行动倒计时是 ∞ 的那个；还没认出 ∞ 时先记见过血最多的，之后认出 ∞ 或出现血更多的再改过来。
    认出 ∞ 以后不再按血量改认：精英战几个敌人血量相近时，曾按血量在几个敌人之间来回换目标。"""
    if not enemies:
        return
    boss = [e for e in enemies if e.get("infinite")]
    if boss:
        top = max(boss, key=lambda e: e["hp"])
        state.update(head=(top["x"], top["y"]), head_hp=top["hp"], head_sure=True)
        return
    if state.get("head_sure"):
        return
    top = max(enemies, key=lambda e: e["hp"])
    if state.get("head") is None or top["hp"] > state.get("head_hp", 0):
        state["head"], state["head_hp"] = (top["x"], top["y"]), top["hp"]


def choose_target(enemies, boss_battle, sticky, head=None, urgent=False):
    """攻击牌的目标，返回 (敌人, 理由)。
    Boss 战打 Boss；否则集火：沿用上一个目标直到它死，没有时挑有攻击意图（认不出按攻击算）的敌人里血量 + 护盾最少的，
    少打死一个就少挨一个的打。urgent（我方血量为 0）：先打有攻击意图、行动倒计时小的。"""
    if not enemies:
        return None, "没有识别到敌人"
    if urgent:
        return min(enemies, key=_threat_rank), "攻击意图、行动倒计时小、血少的优先"
    if boss_battle:
        if head is not None:
            same = [e for e in enemies if abs(e["x"] - head[0]) < 0.04 and abs(e["y"] - head[1]) < 0.04]
            if same:
                return same[0], "Boss 战继续打 Boss"
        return max(enemies, key=lambda e: e["hp"]), "Boss 战优先打血量最多的 Boss"
    if sticky is not None:
        same = [e for e in enemies if abs(e["x"] - sticky[0]) < 0.04 and abs(e["y"] - sticky[1]) < 0.04]
        if same:
            return same[0], "继续打同一个敌人"
    return min(enemies, key=_focus_rank), "集火：攻击意图、血量加护盾最少的优先"


def _attacking(e):
    return e["intent"] in (None, INTENT_ATTACK)


def _toughness(e):
    return e["hp"] + (e.get("shield") or 0)


def _threat_rank(e):
    countdown = e["countdown"] if e["countdown"] is not None else 99
    return (0 if _attacking(e) else 1, countdown, e["hp"])


def _focus_rank(e):
    countdown = e["countdown"] if e["countdown"] is not None else 99
    return (0 if _attacking(e) else 1, _toughness(e), countdown)


# ======================================================================
# 执行
# ======================================================================

def _state(task):
    state = getattr(task, "_battle", None)
    if state is None:
        state = {}
        task._battle = state
    return state


def _new_turn(state):
    # collected：本回合已经点开看过意图的敌人（意图每回合会变，所以每回合每个敌人最多采集一次）
    # ego_tries：本回合每个 Ego 按过几次；collect_off：本回合不再采集意图
    # key_retry：本回合按键没打出去、改用拖动再试的牌位；e_presses：本回合按了几次 E
    # turn_fails：本回合每张牌（按牌名，没读到牌名按位置）一共失败了几次
    state.update(unplayable=set(), attempts={}, costs={}, last=None, collected=set(), ego_tries={},
                 collect_off=False, key_retry=set(), e_presses=0, turn_fails={})


def _start_turn_if_new(state):
    """敌人行动完、「结束回合」按钮重新出现：新回合。放 Ego 和出牌都在我方回合开头调用，只会复位一次。
    不能只靠「剩余 AP 变多」判断——上回合末尾 AP 常读不到，曾因此没认出新回合，
    上回合记下的「出不起」一直留着，新回合一张牌都不出就结束了。"""
    if state.get("enemy_phase"):
        _new_turn(state)
        state.update(ended=False, enemy_phase=False)


def _ensure_battle(task):
    """超过 20 秒没看到战斗页面就当作新的一场战斗。"""
    state = _state(task)
    if not state or time.time() - state.get("last_seen", 0) > 20:
        start_battle(task)
    state["last_seen"] = time.time()
    return state


def node_type(task):
    """当前节点类型（boss / 精英 / 小怪 …）。不用 final_boss_battle：它在路线图上看到 Boss 节点时就被置上，
    实跑中第 7 节点的小怪、之后各层的小怪和精英都被当成了 Boss 战。"""
    return (getattr(task, "node_status", None) or {}).get("node_type") or ""


def _session(task):
    """跨战斗保留的状态：本次运行拖动出牌成功过几次等。后台拖动（PostMessage）可能不被游戏当成出牌；
    drag_disabled 只管当前这场战斗，每场开始时清掉重新试（实跑 19:30 窗口在前台时开头两次拖动失败，
    之后十几分钟、两场精英战都只能按键打默认目标；窗口切到后台时按键又失效，两头都没了）。"""
    session = getattr(task, "_battle_session", None)
    if session is None:
        # zero_cost：本次运行读到过 0 费的牌名，见 _read_costs；types：读到过的牌名 → 类型，见 _remember_type
        session = {"drag_ok": 0, "drag_fail": 0, "drag_disabled": False, "zero_cost": set(),
                   "types": {}}
        task._battle_session = session
    session.setdefault("types", {})  # 热重载前建的 session 没有这一项
    return session


def start_battle(task):
    state = _state(task)
    state.clear()
    _new_turn(state)
    state.update(sticky=None, drag_fail={}, last_remaining=None, last_seen=time.time(), zero_frames=0)
    session = _session(task)
    session.update(drag_disabled=False, drag_fail=0)
    state["key_fail_cards"] = set()  # 本场按键没打出去、拖动打出去了的牌，见 _KEYS_DEAD_CARDS
    # 战斗编号和「战斗开始」记录由 battle_log.battle_frame 统一写（两个模式相同）


def _slot(card, hand_count):
    """牌的位置标识「按键/手牌数」：手牌数不变时同一个位置就是同一张牌，不受牌名读法影响。"""
    return f"{card['key']}/{hand_count}"


def _mark_unplayable(state, last, by_name=True):
    """by_name 为 False 时只记这个位置：同名的其他牌（比如击破后从墓地召回的）照样可以出。"""
    state["unplayable"].add(last["slot"])
    if by_name and not last["name"].startswith("未识别"):
        state["unplayable"].add(last["name"])


def _short_of_ap(last):
    """没打出去可能是 AP 不够吗：费用读不到、剩余 AP 读不到、或费用确实比剩余 AP 多。
    0 费牌、费用明明够的牌没打出去，多半是打出去了但没看出来（实跑中定位雷射击破后回到手牌，手牌数、AP 都没变），
    不能按牌名封掉：同名牌本回合全出不了，击破后召回的定位雷射都留在手里就结束了回合。"""
    cost, remaining = last.get("cost"), last.get("remaining")
    if cost is None and last.get("twin_ok"):
        return False
    return cost is None or remaining is None or cost > remaining


def _check_last_play(task, state, hand_count, remaining):
    """上一张牌打出去了吗：手牌数减少、或剩余 AP 减少就算打出去了（抽牌的牌打出后手牌数可能不变，但 AP 会减少）。
    同名牌可能有好几张，所以不看牌名还在不在。"""
    last = state.get("last")
    if not last or hand_count is None:
        return
    state["last"] = None
    # 0 费又抽牌的牌（如逆转之刃）打出后手牌数、AP 都不变：看左侧有没有出现这张牌的牌名横幅。
    # 手牌数变多也算打出去了：我方回合只有出牌的效果会加牌（击破后从墓地召回、抽牌）
    played = hand_count != last["hand"] or (
        remaining is not None and last.get("remaining") is not None and remaining < last["remaining"]) \
        or _played_banner(task, last["name"])
    if not played:
        # 出牌动画慢时这一帧手牌数还没变（实跑 20:01:59 读到 6、紧接着重读是 5）：再截一帧看看
        fresh = _fresh_hand_count(task)
        played = fresh is not None and fresh != last["hand"]
    fails = state["attempts"]
    fails[last["slot"]] = 0 if played else fails.get(last["slot"], 0) + 1
    if last["method"] == "拖动":
        session = _session(task)
        if played:
            session["drag_ok"] += 1
        else:
            session["drag_fail"] += 1
            drag_fail = state["drag_fail"]
            drag_fail[last["name"]] = drag_fail.get(last["name"], 0) + 1
            if drag_fail[last["name"]] >= _DRAG_FAIL_LIMIT and not last.get("once"):
                # 改用按键后重新计数，不让拖动的失败次数算到按键头上。只试一次的拖动（按键失败后的重试、拖到场地中间）
                # 不清零：实跑中苍白流星的每次重试都把计数清掉，同一回合按键、拖动轮流试了 12 次
                fails[last["slot"]] = 0
            if not session["drag_ok"] and session["drag_fail"] >= _DRAG_FAIL_LIMIT and not session["drag_disabled"]:
                # 本次运行还没拖成功过、这场又连着失败：本场改用按键，下一场重新试
                session["drag_disabled"] = True
                fails[last["slot"]] = 0
                battle_log.anomaly(task, "拖动出牌无效",
                                   f"拖动出牌连续 {session['drag_fail']} 次都没打出去，本场战斗改用按键打默认目标")
    if last.get("key_retry") and played and not state.get("keys_dead"):
        # 单张牌按键没打出去常是这张牌自己的原因（实跑：闪耀核心开局按键失败，同一场后来按键又能出），
        # 不同的牌都这样才算键盘失效、整场改用鼠标
        key_fail_cards = state.setdefault("key_fail_cards", set())
        key_fail_cards.add(last["name"] if not last["name"].startswith("未识别") else last["slot"])
        if len(key_fail_cards) >= _KEYS_DEAD_CARDS:
            state["keys_dead"] = True
            battle_log.anomaly(task, "按键无效", f"{'、'.join(sorted(key_fail_cards))} 按键没打出去、拖动打出去了，"
                                                 "本场战斗改用鼠标出牌和结束回合")
        else:
            task.log_info(f"「{last['name']}」按键没打出去、拖动打出去了，先只对这张牌改用拖动")
    if last["method"] == "按键" and not played and remaining != 0 and last["slot"] not in state["key_retry"] \
            and not _session(task)["drag_disabled"]:
        # 键盘可能失效了：先不记出不起，下一帧拖动再试一次（AP 确实不够时多花一次拖动）
        state["key_retry"].add(last["slot"])
        fails[last["slot"]] = 0
        task.log_info(f"「{last['name']}」按键没打出去，改用拖动再试一次")
        return
    # 按键出的牌没打出去多半是 AP 不够（「AP不足」提示一闪而过常常读不到）：失败一次本回合就不再出它，
    # 不再每张试 3 次（实跑中回合末尾每张牌白按 3 遍，每场战斗浪费 20~30 秒）。
    # 拖到场地中间的牌、按键失败后改拖动重试的牌同样只试一次
    limit = 1 if last["method"] == "按键" or last.get("once") else _STUCK_LIMIT
    turn_fails = state.setdefault("turn_fails", {})
    card_key = last["name"] if not last["name"].startswith("未识别") else last["slot"]
    if not played:
        turn_fails[card_key] = turn_fails.get(card_key, 0) + 1
        if turn_fails[card_key] >= _TURN_FAIL_LIMIT:
            fails[last["slot"]] = max(fails[last["slot"]], limit)  # 换着法子也一直出不掉：本回合不再出它
    if not played and fails[last["slot"]] >= limit:
        by_name = _short_of_ap(last)
        _mark_unplayable(state, last, by_name=by_name)
        battle_log.anomaly(task, "出不掉牌", f"「{last['name']}」{last['method']}出牌 {limit} 次没打出去，"
                           + ("本回合不再出它" if by_name else "费用够，只跳过这个位置，同名牌照样出"))


def _played_banner(task, name):
    if name.startswith("未识别") or len(name) < 2:
        return False
    x1, y1, x2, y2 = _PLAYED_BANNER
    for box in task.all_texts:
        cx, cy = _rel(task, box)
        text = box.name.strip()
        if x1 <= cx <= x2 and y1 <= cy <= y2 and len(text) >= 2 and (name in text or text in name):
            return True
    return False


def _use_drag(task, state, card):
    if card["type"] != "攻击" or _session(task)["drag_disabled"]:
        return False
    failed = [n for n, count in state["drag_fail"].items() if count >= _DRAG_FAIL_LIMIT]
    return not (card["name"] in failed or (not card["name"].startswith("未识别") and _matches(card["name"], failed)))


def _read_costs(task, state, frame, cards, pending):
    """并行读还没读过费用的牌；牌名读到的按牌名缓存到本回合结束。
    手里有同名牌时每张都现读、不缓存：同名牌费用可能不同（实跑 11:44 两张暗黑之刃一张 1 费、一张 3 费，
    3 费那张按缓存当成 1 费，AP 只剩 2 时反复去出它，卡了 3 分钟）。"""
    counts = collections.Counter(c["name"] for c in cards)

    def by_name(card):
        return not card["name"].startswith("未识别") and counts[card["name"]] == 1

    jobs = {}
    for card in cards:
        cached = by_name(card) and card["name"] in state["costs"]
        if not cached:
            ready = pending.get(card["name"]) if by_name(card) else None
            jobs[id(card)] = ready or _POOL.submit(_card_cost, task, frame, card)
    for card in cards:
        if id(card) in jobs:
            card["cost"] = jobs[id(card)].result()
            # 没读到牌名的牌不缓存：出掉一张后后面的牌会往前挪，同一个「未识别N」可能已经是另一张牌
            if by_name(card):
                state["costs"][card["name"]] = card["cost"]
        else:
            card["cost"] = state["costs"][card["name"]]
    # 0 费牌的「0」大多读不出来（逆转之刃在有 AP 时读到 0 的不到一成），AP 用完后手牌变暗更读不到，
    # 而 AP 为 0 时读不到费用的牌一律不出，0 费牌就被留在手里结束了回合。
    # 所以记住本次运行读到过 0 费的牌名，之后读不到费用时按 0 费算；读到过别的费用就不再这样算
    zero = _session(task)["zero_cost"]
    for card in cards:
        name = card["name"]
        if name.startswith("未识别") or len(name) < 2:
            continue
        if card["cost"] == 0:
            zero.add(name)
        elif card["cost"] is not None:
            zero.difference_update({n for n in zero if n in name or name in n})
        elif _matches(name, zero):
            card["cost"] = 0


def _raw_capture(task):
    """截一帧新画面。加速模式包装过的 next_frame 会先补足欠下的等待，这里用原来的；没有截图方法（测试替身）时返回 None。"""
    executor = getattr(task, "executor", None)
    return getattr(executor, "_speedup_original_next_frame", None) or getattr(task, "next_frame", None)


def read_hand_count(task, frame):
    """读手牌数「N/10」；OCR 常把前面的图标读成 1（「106」），只取后两位。读不到返回 None。"""
    crop = _crop(frame, _COUNT_BOX)
    if crop.size == 0:
        return None
    for text in _ocr_texts(task, cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)):
        match = _COUNT_TEXT.search(text.replace(" ", ""))
        if match:
            count = int(match.group(1)[-2:])
            return count if count <= 10 else count % 10
    return None


def _wait_hand_settled(task):
    """出牌后等手牌数停稳：至少 _SETTLE_MIN 秒，且连续 _SETTLE_STABLE 秒没变，最多 _SETTLE_MAX 秒。
    移回手牌、抽牌、生成新牌都要等出牌动画和效果结算完才到手，手牌数会先少一张再变多。"""
    capture = _raw_capture(task)
    if capture is None:
        task.sleep(1)
        return
    start = time.time()
    value, since, missing = None, None, 0
    while True:
        frame = capture()
        if frame is None:
            break
        count = read_hand_count(task, frame)
        now = time.time()
        missing = missing + 1 if count is None else 0
        if missing >= _SETTLE_MISSING:
            break  # 手牌数被盖住：多半弹出了选择页面，交给下一轮处理
        if count is None or count != value:
            value, since = count, now
        elif now - start >= _SETTLE_MIN and now - since >= _SETTLE_STABLE:
            break
        if now - start >= _SETTLE_MAX:
            break
        time.sleep(_SETTLE_POLL)
    speed = getattr(task, "_speedup", None)
    if speed is not None:
        # 已经等到手牌停稳：加速模式记着的出牌等待不用再补
        speed.update(owed_until=0.0, pay_hook=None)


def _fresh_hand_count(task):
    capture = _raw_capture(task)
    frame = capture() if capture is not None else None
    return read_hand_count(task, frame) if frame is not None else None


def _hand_changed(task, state, hand_count):
    """识别完手牌、准备出牌前再读一次手牌数：和识别时不一样，说明新牌刚到手，
    按旧的排位出牌会按错，这一帧放弃，下一帧重新识别。连续 _STALE_LIMIT 次都这样就照常出牌，免得卡住。"""
    count = _fresh_hand_count(task)
    if count is None or count == hand_count or state.get("stale", 0) >= _STALE_LIMIT:
        state["stale"] = 0
        return False
    state["stale"] = state.get("stale", 0) + 1
    task.log_info(f"识别完手牌后手牌数从 {hand_count} 变成了 {count}（新牌刚到手），重新识别")
    battle_log.record(task, "手牌变化重读", before=hand_count, after=count)
    return True


def play_turn(task, hand_count, finish_turn_visible):
    """战斗页面一帧：看一眼、出一张牌或结束回合。返回 True 表示本帧已处理。"""
    state = _ensure_battle(task)
    frame = task.frame

    # 上一张牌提示「AP不足」：记为本回合出不起，这一帧不再出牌
    if ap_insufficient(task):
        last = state.get("last")
        if last:
            _mark_unplayable(state, last, by_name=not last.get("twin_ok"))
            state["last"] = None  # 提示会停留几帧，只记一次
            battle_log.anomaly(task, "AP不足", f"「{last['name']}」费用不够，本回合不再出它")
        return True
    if not finish_turn_visible:
        if state.get("ended"):
            state["enemy_phase"] = True  # 按过结束回合后「结束回合」按钮消失：轮到敌人行动
        return _wait_for_button(task, state)
    state.update(button_gone=None, button_reported=False)
    _start_turn_if_new(state)

    cards = read_hand(task, hand_count)
    if not cards:
        return _end_turn(task, "手牌数为 0")
    for card in cards:
        card["slot"] = _slot(card, hand_count)
        if card["type"] is None and card["y"] is not None:
            card["type"] = _card_type(task, card)
        _remember_type(task, card)

    # 几块区域各自裁剪识别，互不相干，并行跑：剩余费用、敌人、没缓存的牌费用
    remaining_job = _POOL.submit(read_remaining_cost, task, frame)
    enemies_job = _POOL.submit(read_enemies, task, frame)
    pending = {c["name"]: _POOL.submit(_card_cost, task, frame, c) for c in cards
               if not c["name"].startswith("未识别") and c["name"] not in state["costs"]}
    remaining = remaining_job.result()
    if remaining is None and (ap_zero_outline(frame) or hand_lowered(task)):
        # 实跑中 AP 用完后读不到灰色的「0」，把每张牌都按一遍才结束回合。按键后手牌要过一会儿才沉下去，
        # 所以先看数字颜色，手牌沉下去作为第二个判断
        remaining = 0
        task.log_info("AP 已用完（灰色的 0 或手牌沉下去）")
    if remaining is not None and state["last_remaining"] is not None and remaining > state["last_remaining"]:
        _new_turn(state)  # 费用回满：新回合（也可能是某张牌加了费用，同样应该重新判断）
    state["last_remaining"] = remaining
    _check_last_play(task, state, hand_count, remaining)
    _read_costs(task, state, frame, cards, pending)
    for card in cards:
        if card["type"] == "崩溃":
            card["cost"] = 0  # 费用位置显示的是崩溃进度，不是费用
    enemies = enemies_job.result()

    unnamed = [c["key"] for c in cards if c["name"].startswith("未识别")]
    if len(unnamed) == len(cards):
        state["zero_frames"] += 1
        if state["zero_frames"] >= 3:
            battle_log.anomaly(task, "手牌识别失败", f"手牌数 {hand_count} 但连续 {state['zero_frames']} 帧一张牌名都没读到")
    else:
        state["zero_frames"] = 0
        if unnamed and remaining != 0:  # AP 用完时手牌变暗，牌名本来就读不全，不算异常
            battle_log.anomaly(task, "牌名没读到", f"按键 {unnamed} 位置上的牌名没读到，仍按位置出牌")

    # Boss 战始终打 Boss：它大多会不断召唤小怪，小怪打不完，Boss 死了就过关。精英战按普通战斗集火
    boss_battle = node_type(task) == "boss"
    if _get_config_value(task, COLLECT_KEY, False) and not state.get("collect_off"):
        # 每回合每个敌人最多点开一次：读不出意图（比如没见过的写法）也不会反复点，卡在同一个画面
        unknown = next((e for e in enemies if e["intent"] is None
                        and (round(e["x"] * 20), round(e["y"] * 20)) not in state["collected"]), None)
        if unknown is not None:
            state["collected"].add((round(unknown["x"] * 20), round(unknown["y"] * 20)))
            if collect_intent(task, unknown):
                # 实跑 16:38:14：击杀后连点 4 个敌人都是「本回合已行动」，白花 10 秒。
                # 看到一个已行动，其余多半也行动过了，这回合不再点
                state["collect_off"] = True
            return True

    priority = _get_config_value(task, "出牌优先级", [])
    defense = _get_config_value(task, DEFENSE_KEY, [])
    hp = read_hp(task)
    # 血量为 0：再挨一次打就输，不出护盾牌、崩溃牌，攻击先打马上要动手的敌人
    zero_hp = is_zero_hp(hp, state.get("last_hp"), frame)
    if hp:
        state["last_hp"] = hp
    lethal, after = False, 1.0
    if remaining and not zero_hp and any(is_defense(c, defense) for c in cards):
        lethal, after = incoming_lethal(task)  # 只有手里有防御牌时才值得花时间读预计扣血
    danger = lethal or in_danger(hp, after, _get_config_value(task, DANGER_KEY, 25))

    card, reason = choose_play(cards, remaining, priority, defense, danger, state["unplayable"], zero_hp=zero_hp)
    observed = {
        "hand_count": hand_count, "remaining": remaining, "hp": hp, "shield": read_shield(task),
        "hp_after_ratio": round(after, 3), "lethal": lethal, "danger": danger, "zero_hp": zero_hp,
        "cards": [{k: c.get(k) for k in ("name", "key", "type", "cost")} for c in cards],
        "enemies": [{k: e[k] for k in ("x", "y", "hp", "shield", "countdown", "intent")} for e in enemies],
        "unplayable": sorted(state["unplayable"]),
    }
    if card is None:
        # AP 确实用完（读到 0 或手牌沉下去）且没有 0 费牌可出：加速模式不必再等 3 秒、再确认一轮
        return _end_turn(task, reason, sure=remaining == 0, observed=observed)
    if _hand_changed(task, state, hand_count):
        return True

    target, target_reason = None, None
    # 本回合按键没打出去的牌位；本场按键失败、拖动打出去过的牌以后直接拖
    retry = card["slot"] in state["key_retry"] or card["name"] in state.get("key_fail_cards", ())
    use_drag = _use_drag(task, state, card)
    # 要改用拖动、但类型没读到的牌按攻击牌拖到敌人身上：多半是标签没读到的攻击牌，拖到场地中间打不出去
    untyped = card["type"] is None and (state.get("keys_dead") or retry) and not _session(task)["drag_disabled"]
    if use_drag or untyped:
        use_drag = True
        if zero_hp:
            # 不再优先打 Boss、也不沿用上一个目标：先打有攻击意图、行动倒计时小、血少的
            target, target_reason = choose_target(enemies, False, None, urgent=True)
            target_reason = target_reason and "血量为 0，" + target_reason
        else:
            if boss_battle:
                update_head(state, enemies)
            target, target_reason = choose_target(enemies, boss_battle, state["sticky"], state.get("head"))
        use_drag = target is not None
        if use_drag and untyped:
            target_reason = f"类型没读到，按攻击牌拖到敌人身上（{target_reason}）"
    # 本场键盘失效、或这张牌按键没打出去：不用选目标（或没找到目标）的牌也拖到场地中间打出
    field = not use_drag and (state.get("keys_dead") or retry) and not _session(task)["drag_disabled"]
    if field:
        target_reason = "按键无效，拖到场地中间" if state.get("keys_dead") else "按键没打出去，改用拖动再试"
    method = "拖动" if use_drag or field else "按键"
    battle_log.record(task, "出牌", card=card["name"], key=card["key"], cost=card["cost"], card_type=card["type"],
                      reason=reason, target=target and {k: target[k] for k in ("x", "y", "hp", "countdown", "intent")},
                      target_reason=target_reason, method=method, **observed)
    task.log_info(f"出牌「{card['name']}」（{reason}）"
                  + (f" → 拖到敌人 hp={target['hp']}（{target_reason}）" if use_drag
                     else f" → {target_reason}" if field else f" → 按键 {card['key']}"))
    # 手里有同名、费用读得到且出得起的牌：这张出不掉时只封位置，别连它一起封掉（实跑中 3 费暗黑之刃读不到费用，把 1 费的也封了）
    twin_ok = any(c is not card and c["name"] == card["name"] and c["cost"] is not None
                  and remaining is not None and c["cost"] <= remaining for c in cards)
    state["last"] = {"name": card["name"], "slot": card["slot"], "hand": hand_count, "method": method,
                     "remaining": remaining, "cost": card["cost"], "twin_ok": twin_ok,
                     "key_retry": retry, "once": retry or field}
    if use_drag:
        _drag_card(task, card, target)
        state["sticky"] = (target["x"], target["y"])
    elif field:
        _drag_card(task, card, {"drop": _FIELD_DROP})
    else:
        task.send_key(card["key"])
        task.sleep(0.5)
        task.send_key("enter")
        _wait_hand_settled(task)
    if any(word in card["name"] for word in _EXTRA_WAIT_CARDS):
        task.sleep(2)
    return True


def _post_message_interaction(task):
    """框架的后台交互对象（PostMessage）；不是后台交互（或测试替身）时返回 None。"""
    interaction = getattr(getattr(task, "executor", None), "interaction", None)
    if type(interaction).__name__ != "PostMessageInteraction":
        return None
    return interaction


def _post_drag(task, start, drop):
    """后台拖动，返回 False 表示当前不是后台交互、没拖。
    框架 PostMessageInteraction.swipe 在游戏里拖不出牌：松手消息的坐标固定是 (0, 0)（它的 mouse_pos 从不更新），
    牌在窗口左上角松手被游戏当成取消；中途也只移 3 步、到不了终点。这里自己发消息：
    按下 → 分步移到落点 → 停一下让游戏锁定目标 → 在落点松手。"""
    interaction = _post_message_interaction(task)
    if interaction is None:
        return False

    def pos(point):
        return interaction.update_mouse_pos(int(task.width * point[0]), int(task.height * point[1]))

    def move(a, b):
        lparam = None
        for i in range(1, _DRAG_STEPS + 1):
            t = i / _DRAG_STEPS
            lparam = pos((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            interaction.post(_WM_MOUSEMOVE, _MK_LBUTTON, lparam)
            time.sleep(_DRAG_STEP_INTERVAL)
        return lparam

    recorder = battle_log._recorder()
    if recorder is not None:
        recorder.note_action(task, "drag", start, drop)
    lparam = pos(start)
    interaction.post(_WM_MOUSEMOVE, 0, lparam)
    time.sleep(0.05)
    interaction.post(_WM_LBUTTONDOWN, _MK_LBUTTON, lparam)
    time.sleep(0.08)
    try:
        lparam = move(start, drop)
        time.sleep(_DRAG_HOVER)
        interaction.post(_WM_MOUSEMOVE, _MK_LBUTTON, lparam)
    finally:
        # 中途出错也必须松手：按住不放牌会一直拿在手里，结束回合按钮变灰，游戏一直等（兜底见 utils._esc_fallback）
        interaction.post(_WM_LBUTTONUP, 0, lparam)
    return True


def _drag_card(task, card, target):
    """把牌从牌身中部拖到目标身上松手。"""
    # 从牌身中部拖起；牌名没读到时没有 y，用手牌区牌身的大致高度
    start = (card["x"] + 0.035, min(0.93, card["y"] + 0.07) if card.get("y") is not None else 0.86)
    if not _post_drag(task, start, target["drop"]):
        task.swipe_relative(start[0], start[1], target["drop"][0], target["drop"][1], duration=0.35)
    speed = getattr(task, "_speedup", None)
    if speed is not None:
        # 加速模式按「数字键/回车」判断刚出过牌；拖动出牌也要告诉它，免得刚出完牌就被判定可以结束回合
        speed["last_play_key"], speed["end_turn_seen"] = time.time(), 0.0
    _wait_hand_settled(task)


def _wait_for_button(task, state):
    """「结束回合」按钮不在：一般是敌人在行动，这一帧什么也不做。
    实跑中出现过按钮一直不回来（画面上能读到手牌数，但盖着别的页面）：以前每帧都默默等，卡了 13 分钟只能手动撤退。
    按钮消失超过 _BUTTON_GONE_LIMIT 秒就不再占着这一帧，让后面的页面处理函数（弹窗、选择页等）接手，
    并存一张截图、记下画面上的文字，方便查是什么页面。"""
    now = time.time()
    if not state.get("button_gone"):
        state["button_gone"] = now
    waited = now - state["button_gone"]
    if waited < _BUTTON_GONE_LIMIT:
        return True
    if not state.get("button_reported"):
        state["button_reported"] = True
        battle_log.anomaly(task, "结束回合按钮一直不出现",
                           f"已 {waited:.0f} 秒没看到结束回合按钮，交给其他页面处理函数",
                           texts=[b.name for b in task.all_texts][:120])
    return False


def ep_full(frame):
    """EP 条满格：最下面一格亮成浅青色（RGB≈193,255,255）。"""
    h, w = frame.shape[:2]
    x, y = int(_EP_FULL_POINT[0] * w), int(_EP_FULL_POINT[1] * h)
    region = frame[max(0, y - 2):y + 3, max(0, x - 2):x + 3, :3]
    if region.size == 0:
        return False
    b, g, r = cv2.mean(region)[:3]
    return abs(b - 255) <= 15 and abs(g - 255) <= 15 and abs(r - 193) <= 15


def ego_slots(frame):
    """三个 Ego 是否放得起：费用框是浅青色的放得起，灰色的 EP 不够，暗的是空槽。返回 [{key, y, ready}]。"""
    slots = []
    for key, cy in _EGO_SLOTS:
        crop = _crop(frame, (_EGO_COST_X[0], cy - 0.012, _EGO_COST_X[1], cy + 0.012))
        ready = False
        if crop.size:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            lit = (hsv[:, :, 0] > 80) & (hsv[:, :, 0] < 105) & (hsv[:, :, 1] > 40) & (hsv[:, :, 2] > 180)
            ready = lit.mean() >= _EGO_READY
        slots.append({"key": key, "y": cy, "ready": ready})
    return slots


def use_ego(task, finish_turn_visible):
    """我方回合、出牌之前放 Ego。EP 会带到下一场战斗：Boss 战、精英战放得起就放；
    其余战斗等 EP 满格再放（不放就溢出浪费）。放得起的里挑费用最高的；目标用回车选默认目标（与按键出牌相同）。
    返回 True 表示本帧已按键。"""
    if not finish_turn_visible:
        return False
    frame = task.frame
    kind = node_type(task)
    full = ep_full(frame)
    if kind not in ("boss", "精英") and not full:
        return False
    state = _ensure_battle(task)
    _start_turn_if_new(state)
    tries = state.setdefault("ego_tries", {})
    ready = [s for s in ego_slots(frame) if s["ready"] and tries.get(s["key"], 0) < _EGO_TRIES]
    if not ready:
        return False
    for slot in ready:
        slot["cost"] = _read_digit(task, frame, (0.054, slot["y"] - 0.017, 0.073, slot["y"] + 0.017), _ONE_DIGIT)
    chosen = max(ready, key=lambda s: -1 if s["cost"] is None else s["cost"])
    tries[chosen["key"]] = tries.get(chosen["key"], 0) + 1
    reason = f"{kind or '普通'}战斗" + ("，EP 满格" if full else "，放得起就放")
    task.log_info(f"释放 Ego {chosen['key']}（费用 {chosen['cost'] if chosen['cost'] is not None else '?'}，{reason}）")
    battle_log.record(task, "释放Ego", key=chosen["key"], cost=chosen["cost"], reason=reason, ep_full=full,
                      ready=[s["key"] for s in ready], tries=tries[chosen["key"]])
    task.send_key(chosen["key"])
    task.sleep(1)
    task.send_key("enter")
    task.sleep(4)  # 与原逻辑相同：等 Ego 动画播完
    return True


def _end_turn(task, reason, sure=False, observed=None):
    task.log_info(f"结束回合：{reason}")
    _state(task)["ended"] = True
    speed = getattr(task, "_speedup", None)
    if speed is not None:
        speed["end_turn_sure"] = sure
        speed["end_turn_deferred"] = False
    task.send_key("e")
    pressed = not (speed is not None and speed.get("end_turn_deferred"))
    if pressed:
        state = _state(task)
        state["e_presses"] = state.get("e_presses", 0) + 1
        if state.get("keys_dead") or state["e_presses"] > _E_KEY_LIMIT:
            # E 可能没反应（见 _FIELD_DROP 上方的说明）：再用鼠标点一下按钮
            if state["e_presses"] == _E_KEY_LIMIT + 1 and not state.get("keys_dead"):
                battle_log.anomaly(task, "按 E 无效", f"本回合按了 {_E_KEY_LIMIT} 次 E 还没结束回合，改用鼠标点按钮")
            _move_and_click(task, *_END_TURN_POINT)
    # 加速模式可能这一轮先不按 E（刚出过牌等手牌刷新）：只在真正按下时记一条，免得战斗记录里每回合结束两次
    if observed is not None and pressed:
        battle_log.record(task, "结束回合", reason=reason, **observed)
    task.sleep(1)
    return True
