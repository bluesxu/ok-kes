"""
自动出击模式的战斗出牌：读画面（手牌费用/类型、剩余费用、我方血量与预计扣血、敌人血量/护盾/行动倒计时/意图），
按规则挑一张牌，按键或拖到目标身上打出。术语见仓库根目录 CONTEXT.md。

出牌规则（每次只出一张，出完下一帧重新观察）：
1. 只考虑出得起的牌：读到费用的按费用判断；读不到的先试着出，弹出「AP不足」就记为本回合出不起。
2. 预计扣血会把我方打死时，先给防御牌留出费用：非防御牌只能用「剩余费用 - 防御牌费用」。
3. 「出牌优先级」里的牌先出；然后是其余攻击牌、防御卡牌列表里的牌、其他牌。
4. 没有出得起的牌就按 E 结束回合。
攻击牌拖到目标身上打出（见 docs/adr/0001）：Boss 战优先打 Boss；否则集中打同一个敌人直到它死，
第一个目标按「攻击意图优先、行动倒计时小的优先、血少的优先」挑。非攻击牌仍用数字键 + 回车。

注意：本文件不能定义顶层类，框架会把 ok_tasks 下含类的 .py 当作任务加载。
"""
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
from utils import _get_config_value, _move_and_click, _normalize_text

DEFENSE_KEY = "防御卡牌列表"
FINE_KEY = "精细化战斗"
COLLECT_KEY = "意图采集"

ICON_DIR = os.path.join("configs", "battle_icons")
INTENT_ATTACK, INTENT_DEFENSE, INTENT_BUFF = "攻击", "防御", "增益"

# ---- 画面坐标（相对屏幕，基准 16:9） ----
_REMAIN_REGION = (0.47, 0.88, 0.53, 0.96)          # 手牌下方中央的剩余费用
_HP_TEXT_REGION = (0.10, 0.0, 0.33, 0.07)          # 我方血量「当前/上限」
_SHIELD_REGION = (0.40, 0.0, 0.48, 0.09)           # 我方护盾数值
_HP_BAR_X = (0.015, 0.42)                          # 我方血条横向范围
_HP_BAR_ROWS = (0.034, 0.038, 0.042)               # 我方血条取样的行
_HP_BAR_LEFT = 0.022                               # 我方血条左端
_ENEMY_AREA = (0.30, 0.0, 1.0, 0.62)               # 敌人血条可能出现的范围
_CARD_COST_BOX = (-0.030, -0.016, 0.004, 0.050)    # 费用数字相对牌名左上角的范围
_CARD_TYPE_BOX = (-0.012, 0.004, 0.05, 0.055)     # 类型标签相对牌名左上角的范围；右边界太宽会拿到相邻牌的标签
_PANEL_REGION = (0.02, 0.02, 0.46, 0.46)           # 怪物信息面板
_PANEL_CLOSE = (0.502, 0.092)                      # 关闭怪物信息面板（与 handle_weakness_info 相同）

_DIGITS = re.compile(r"^\d{1,2}$")
_ONE_DIGIT = re.compile(r"^\d$")
_NUMBER = re.compile(r"^\d{1,6}$")
_ICON_THRESHOLD = 0.45     # 白色笔画重合度：已收集的图标里不同类别之间最高 0.31
_GLYPH_FRAC = 0.75
_GLYPH_MIN_PIXELS = 30
_AP_SHORT = re.compile(r"AP\s*不足", re.IGNORECASE)
_EXTRA_WAIT_CARDS = ("极光", "万众英雄")  # 打出后动画较长，沿用原逻辑额外等 2 秒
_STUCK_LIMIT = 3            # 同一张牌连续这么多次还在手里（又没提示 AP不足），本回合不再出它
_DRAG_FAIL_LIMIT = 2        # 拖动连续这么多次没打出去，改用按键 + 回车打默认目标
_RED_SAMPLES = 4            # 读预计扣血时连续取样的帧数（红色段是闪烁的）
_RED_INTERVAL = 0.25
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="出牌识别")  # 几块区域的裁剪识别并行跑
_OCR_LOCK = threading.Lock()


def install(task):
    """给出击模式加上出牌相关配置项。"""
    task.default_config[DEFENSE_KEY] = []
    task.default_config[FINE_KEY] = True
    task.default_config[COLLECT_KEY] = False
    task.config_description[DEFENSE_KEY] = "给我方加护盾的牌；预计这回合会被打死时，先给这些牌留出费用"
    task.config_description[FINE_KEY] = "出牌前用击杀预览挑目标（当前版本尚未实现预览，行为与关闭相同）；关闭时集中打同一个敌人直到它死"
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


def ap_insufficient(task):
    return any(_AP_SHORT.search(box.name) for box in task.all_texts)


def read_hp(task):
    """我方血量 (当前, 上限)；读不到返回 None。"""
    x1, y1, x2, y2 = _HP_TEXT_REGION
    for box in task.all_texts:
        cx, cy = _rel(task, box)
        match = re.search(r"(\d+)\s*/\s*(\d+)", box.name)
        if match and x1 <= cx <= x2 and y1 <= cy <= y2:
            return int(match.group(1)), int(match.group(2))
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
    return worst <= 0.01, worst


_HAND_CENTER = 0.471        # 手牌扇形正中那张牌的牌名左端 x
_HAND_AREA = (0.15, 0.68, 0.86, 0.905)
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿"


def hand_slots(count):
    """手牌按张数排成固定的扇形：返回每张牌牌名左端的 x（从左到右，第 i 张的按键是 i+1，第 10 张是 0）。
    间距按实测（3/5/7/10 张为 0.1375/0.113/0.0808/0.0561）拟合：张数少时不重叠，多了按 1/(1.79n-0.1) 收紧。"""
    if not count:
        return []
    spacing = min(0.1375, 1 / (1.79 * count - 0.1))
    return [_HAND_CENTER + (i - (count - 1) / 2) * spacing for i in range(count)]


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
    for box in task.all_texts:
        left, top = box.x / task.width, box.y / task.height
        cy = (box.y + box.height / 2) / task.height
        if not (x1 <= left <= x2 and y1 <= cy <= y2):
            continue
        index = min(range(len(slots)), key=lambda i: abs(slots[i] - left))
        if abs(slots[index] - left) > 0.45 * spacing + 0.012:
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
        cleaned = re.sub(r"[（(]?\s*极强\s*[）)]?|\s*L[Vv]\.?\s*\d*$", "", cleaned).strip()
        if len(cleaned) >= 2 and not re.search(r"\d+\s*/\s*\d+", cleaned) and "攻" not in cleaned[:1]                 and cleaned not in ("基本", "基础"):  # 「基本攻击」被拆开时剩下的「基本」不是牌名
            names[index].append((len(cleaned), cleaned, top, int(hint.group(1)) if hint else None))
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
        shields = [n for n in near if n is not hp and n[0] > hp[0] + 0.03]
        diamond = (left - 0.021, bar_y + 0.006)
        diamond_region = (diamond[0] - 0.026, diamond[1] - 0.036, diamond[0] + 0.024, diamond[1] + 0.036)
        countdown = _read_digit(task, frame, diamond_region)
        if countdown == 8 and _looks_like_infinity(_crop(frame, diamond_region)):
            countdown = None  # ∞（常见于 Boss）会被 OCR 读成 8：看字形宽高比区分
        icon_region = (diamond[0] - 0.02, diamond[1] + 0.028, diamond[0] + 0.02, diamond[1] + 0.085)
        enemies.append({
            "x": round(left, 3), "y": round(bar_y, 3), "hp": hp[2],
            "shield": shields[0][2] if shields else 0,
            "countdown": countdown,
            "icon_region": icon_region,
            "intent": match_intent(_crop(frame, icon_region)),
            "drop": (min(0.97, hp[0]), min(0.62, bar_y + 0.2)),
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
    """意图采集：点开怪物信息面板读出意图，把图标存进图标库，再关掉面板。"""
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
    if result is None and any("已行动" in text or "行动完成" in text for text, _ in lines):
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
    _move_and_click(task, *_PANEL_CLOSE)
    task.sleep(0.5)


# ======================================================================
# 出牌决策（纯函数，便于测试）
# ======================================================================

def _matches(name, candidates):
    return any(c and (c in name or name in c) for c in candidates)


def _blocked(card, unplayable):
    """这张牌是否已被记为本回合出不起。按牌名比对时允许一方包含另一方：
    OCR 会在牌名前后多读出杂字（「黑暗斩击」「日黑暗斩击」「B黑暗斩击」是同一张牌）。
    牌名没读到的牌按「按键/手牌数」这个位置比对。"""
    if card.get("slot") in unplayable:
        return True
    name = card["name"]
    return not name.startswith("未识别") and _matches(name, [u for u in unplayable if len(u) >= 2 and "/" not in u])


def choose_play(cards, remaining, priority, defense, lethal, unplayable):
    """挑这一次要出的牌。cards 为 [{name, key, type, cost}]，cost 读不到为 None；remaining 读不到为 None。
    unplayable 里是本回合出不起的牌名或位置（「按键/手牌数」）。返回 (牌, 理由)；没有能出的牌时返回 (None, 理由)。"""
    def affordable(card, budget):
        if _blocked(card, unplayable) or card.get("key") is None:
            return False
        if budget is None:
            return True
        if card["cost"] is None:
            return budget > 0  # 读不到费用：还有费用就试着出，AP不足时会被记为出不起
        return card["cost"] <= budget

    is_defense = [_matches(c["name"], defense) for c in cards]
    reserve = 0
    if lethal and remaining is not None:
        costs = [c["cost"] for c, d in zip(cards, is_defense)
                 if d and not _blocked(c, unplayable) and c["cost"] is not None and c["cost"] <= remaining]
        if costs:
            reserve = min(costs)
        elif any(d and not _blocked(c, unplayable) and c["cost"] is None for c, d in zip(cards, is_defense)):
            reserve = 1  # 防御牌费用读不到：至少留 1 费
    # 预计会被打死时，非防御牌只能用预留之外的费用；防御牌照常可以用全部剩余费用
    budget = None if remaining is None else remaining - reserve

    # 崩溃牌不花 AP、不计入敌人的行动次数：手里有就最先出，打够张数才能觉醒
    for card in cards:
        if card["type"] == "崩溃" and not _blocked(card, unplayable) and card.get("key") is not None:
            done = card.get("progress")
            shown = f"，进度 {'?' if done[0] is None else done[0]}/{done[1]}" if done else ""
            return card, f"崩溃牌（不花 AP，打够张数觉醒）{shown}"

    candidates = [(c, d) for c, d in zip(cards, is_defense) if affordable(c, remaining if d else budget)]
    if not candidates:
        return None, "没有出得起的牌"
    suffix = f"（预计会被打死，已给防御牌预留 {reserve} 费）" if reserve else ""
    for name in priority:
        for card, d in candidates:
            if name and (name in card["name"] or card["name"] in name):
                return card, f"命中出牌优先级「{name}」{suffix}"
    for card, d in candidates:
        if card["type"] == "攻击" and not d:
            return card, f"其余攻击牌{suffix}"
    for card, d in candidates:
        if d:
            return card, "防御牌（攻击牌出完后的剩余费用）"
    return candidates[0][0], f"其余牌{suffix}"


def choose_target(enemies, boss_battle, sticky):
    """攻击牌的目标：Boss 战打 Boss（血量最多的敌人）；否则优先沿用上一个目标，
    没有时按攻击意图（认不出按攻击算）、行动倒计时、血量挑。返回 (敌人, 理由)。"""
    if not enemies:
        return None, "没有识别到敌人"
    if boss_battle:
        return max(enemies, key=lambda e: e["hp"]), "Boss 战优先打 Boss"
    if sticky is not None:
        same = [e for e in enemies if abs(e["x"] - sticky[0]) < 0.04 and abs(e["y"] - sticky[1]) < 0.04]
        if same:
            return same[0], "继续打同一个敌人"

    def rank(e):
        attacking = e["intent"] in (None, INTENT_ATTACK)
        countdown = e["countdown"] if e["countdown"] is not None else 99
        return (0 if attacking else 1, countdown, e["hp"])
    return min(enemies, key=rank), "攻击意图、行动倒计时小、血少的优先"


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
    state.update(unplayable=set(), attempts={}, costs={}, last=None, collected=set())


def _session(task):
    """跨战斗保留的状态：这台机器上拖动出牌是否有效。后台拖动（PostMessage）可能不被游戏当成出牌。"""
    session = getattr(task, "_battle_session", None)
    if session is None:
        session = {"drag_ok": 0, "drag_fail": 0, "drag_disabled": False}
        task._battle_session = session
    return session


def start_battle(task):
    state = _state(task)
    state.clear()
    _new_turn(state)
    state.update(sticky=None, drag_fail={}, last_remaining=None, last_seen=time.time(), zero_frames=0)
    battle_log.new_battle(task)
    battle_log.record(task, "战斗开始")


def _slot(card, hand_count):
    """牌的位置标识「按键/手牌数」：手牌数不变时同一个位置就是同一张牌，不受牌名读法影响。"""
    return f"{card['key']}/{hand_count}"


def _mark_unplayable(state, last):
    state["unplayable"].add(last["slot"])
    if not last["name"].startswith("未识别"):
        state["unplayable"].add(last["name"])


def _check_last_play(task, state, hand_count, remaining):
    """上一张牌打出去了吗：手牌数减少、或剩余 AP 减少就算打出去了（抽牌的牌打出后手牌数可能不变，但 AP 会减少）。
    同名牌可能有好几张，所以不看牌名还在不在。"""
    last = state.get("last")
    if not last or hand_count is None:
        return
    state["last"] = None
    played = hand_count < last["hand"] or (
        remaining is not None and last.get("remaining") is not None and remaining < last["remaining"])
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
            if drag_fail[last["name"]] >= _DRAG_FAIL_LIMIT:
                fails[last["slot"]] = 0  # 改用按键后重新计数，不让拖动的失败次数算到按键头上
            if not session["drag_ok"] and session["drag_fail"] >= _DRAG_FAIL_LIMIT and not session["drag_disabled"]:
                session["drag_disabled"] = True
                fails[last["slot"]] = 0
                battle_log.anomaly(task, "拖动出牌无效",
                                   f"拖动出牌连续 {session['drag_fail']} 次都没打出去，本次运行改用按键打默认目标")
    # 按键出的牌没打出去多半是 AP 不够（「AP不足」提示一闪而过常常读不到）：失败一次本回合就不再出它，
    # 不再每张试 3 次（实跑中回合末尾每张牌白按 3 遍，每场战斗浪费 20~30 秒）
    limit = 1 if last["method"] == "按键" else _STUCK_LIMIT
    if not played and fails[last["slot"]] >= limit:
        _mark_unplayable(state, last)
        battle_log.anomaly(task, "出不掉牌", f"「{last['name']}」{last['method']}出牌 {limit} 次没打出去，本回合不再出它")


def _use_drag(task, state, card):
    if card["type"] != "攻击" or _session(task)["drag_disabled"]:
        return False
    failed = [n for n, count in state["drag_fail"].items() if count >= _DRAG_FAIL_LIMIT]
    return not (card["name"] in failed or (not card["name"].startswith("未识别") and _matches(card["name"], failed)))


def _read_costs(task, state, frame, cards, pending):
    """并行读还没读过费用的牌；牌名读到的按牌名缓存到本回合结束。"""
    jobs = {}
    for card in cards:
        cached = not card["name"].startswith("未识别") and card["name"] in state["costs"]
        if not cached:
            jobs[id(card)] = pending.get(card["name"]) or _POOL.submit(_card_cost, task, frame, card)
    for card in cards:
        if id(card) in jobs:
            card["cost"] = jobs[id(card)].result()
            # 没读到牌名的牌不缓存：出掉一张后后面的牌会往前挪，同一个「未识别N」可能已经是另一张牌
            if not card["name"].startswith("未识别"):
                state["costs"][card["name"]] = card["cost"]
        else:
            card["cost"] = state["costs"][card["name"]]


def play_turn(task, hand_count, finish_turn_visible):
    """战斗页面一帧：看一眼、出一张牌或结束回合。返回 True 表示本帧已处理。"""
    state = _state(task)
    if not state or time.time() - state.get("last_seen", 0) > 20:
        start_battle(task)
    state["last_seen"] = time.time()
    frame = task.frame

    # 上一张牌提示「AP不足」：记为本回合出不起，这一帧不再出牌
    if ap_insufficient(task):
        last = state.get("last")
        if last:
            _mark_unplayable(state, last)
            state["last"] = None  # 提示会停留几帧，只记一次
            battle_log.anomaly(task, "AP不足", f"「{last['name']}」费用不够，本回合不再出它")
        return True
    if not finish_turn_visible:
        return True  # 不是我方可操作的时候

    cards = read_hand(task, hand_count)
    if not cards:
        return _end_turn(task, "手牌数为 0")
    for card in cards:
        card["slot"] = _slot(card, hand_count)
        if card["type"] is None and card["y"] is not None:
            card["type"] = _card_type(task, card)

    # 几块区域各自裁剪识别，互不相干，并行跑：剩余费用、敌人、没缓存的牌费用
    remaining_job = _POOL.submit(read_remaining_cost, task, frame)
    enemies_job = _POOL.submit(read_enemies, task, frame)
    pending = {c["name"]: _POOL.submit(_card_cost, task, frame, c) for c in cards
               if not c["name"].startswith("未识别") and c["name"] not in state["costs"]}
    remaining = remaining_job.result()
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
        if unnamed:
            battle_log.anomaly(task, "牌名没读到", f"按键 {unnamed} 位置上的牌名没读到，仍按位置出牌")

    boss_battle = bool((getattr(task, "node_status", None) or {}).get("final_boss_battle"))
    if _get_config_value(task, COLLECT_KEY, False):
        # 每回合每个敌人最多点开一次：读不出意图（比如没见过的写法）也不会反复点，卡在同一个画面
        unknown = next((e for e in enemies if e["intent"] is None
                        and (round(e["x"] * 20), round(e["y"] * 20)) not in state["collected"]), None)
        if unknown is not None:
            state["collected"].add((round(unknown["x"] * 20), round(unknown["y"] * 20)))
            collect_intent(task, unknown)
            return True

    priority = _get_config_value(task, "出牌优先级", [])
    defense = _get_config_value(task, DEFENSE_KEY, [])
    hp = read_hp(task)
    lethal, after = False, 1.0
    if remaining and any(_matches(c["name"], defense) for c in cards):
        lethal, after = incoming_lethal(task)  # 只有手里有防御牌时才值得花时间读预计扣血

    card, reason = choose_play(cards, remaining, priority, defense, lethal, state["unplayable"])
    observed = {
        "hand_count": hand_count, "remaining": remaining, "hp": hp, "shield": read_shield(task),
        "hp_after_ratio": round(after, 3), "lethal": lethal,
        "cards": [{k: c.get(k) for k in ("name", "key", "type", "cost")} for c in cards],
        "enemies": [{k: e[k] for k in ("x", "y", "hp", "shield", "countdown", "intent")} for e in enemies],
        "unplayable": sorted(state["unplayable"]),
    }
    if card is None:
        battle_log.record(task, "结束回合", reason=reason, **observed)
        return _end_turn(task, reason)

    target, target_reason = None, None
    use_drag = _use_drag(task, state, card)
    if use_drag:
        target, target_reason = choose_target(enemies, boss_battle, state["sticky"])
        use_drag = target is not None
    method = "拖动" if use_drag else "按键"
    battle_log.record(task, "出牌", card=card["name"], key=card["key"], cost=card["cost"], card_type=card["type"],
                      reason=reason, target=target and {k: target[k] for k in ("x", "y", "hp", "countdown", "intent")},
                      target_reason=target_reason, method=method, **observed)
    task.log_info(f"出牌「{card['name']}」（{reason}）"
                  + (f" → 拖到敌人 hp={target['hp']}（{target_reason}）" if use_drag else f" → 按键 {card['key']}"))
    state["last"] = {"name": card["name"], "slot": card["slot"], "hand": hand_count, "method": method,
                     "remaining": remaining}
    if use_drag:
        _drag_card(task, card, target)
        state["sticky"] = (target["x"], target["y"])
    else:
        task.send_key(card["key"])
        task.sleep(0.5)
        task.send_key("enter")
        task.sleep(1)
    if any(word in card["name"] for word in _EXTRA_WAIT_CARDS):
        task.sleep(2)
    return True


def _drag_card(task, card, target):
    # 从牌身中部拖起；牌名没读到时没有 y，用手牌区牌身的大致高度
    start = (card["x"] + 0.035, min(0.93, card["y"] + 0.07) if card.get("y") is not None else 0.86)
    task.swipe_relative(start[0], start[1], target["drop"][0], target["drop"][1], duration=0.35)
    speed = getattr(task, "_speedup", None)
    if speed is not None:
        # 加速模式按「数字键/回车」判断刚出过牌；拖动出牌也要告诉它，免得刚出完牌就被判定可以结束回合
        speed["last_play_key"], speed["end_turn_seen"] = time.time(), 0.0
    task.sleep(1)


def _end_turn(task, reason):
    task.log_info(f"结束回合：{reason}")
    task.send_key("e")
    task.sleep(1)
    return True
