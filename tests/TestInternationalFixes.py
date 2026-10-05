# 繁中服（国际服）页面修正：BOSS 选择页标题、休息区读不到生命值/信用点、休息后等「確認」
import os
import sys
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ok_tasks"))

from ok import Box  # noqa: E402
import utils  # noqa: E402
import utils_sortie  # noqa: E402
import utils_chaos  # noqa: E402
import speedup  # noqa: E402

WIDTH, HEIGHT = 2560, 1440
BOSS_POINTS = ["(0.358, 0.706)", "(0.641, 0.706)"]
REST_READ_RETRIES = getattr(utils, "_REST_READ_RETRIES", 3)


def text(name, cx, cy, w=0.06, h=0.03, raw=False):
    """中心在 (cx, cy) 的文字框。主循环的 OCR 结果会经 _simplify_texts 转为简体；raw=True 表示不转（如 wait_ocr）。"""
    return Box((cx - w / 2) * WIDTH, (cy - h / 2) * HEIGHT, to_x=(cx + w / 2) * WIDTH, to_y=(cy + h / 2) * HEIGHT,
               confidence=0.9, name=name if raw else utils._normalize_text(name))


class PageTask:
    """页面处理函数用到的任务接口；文字、模板匹配结果、确认按钮由用例指定。"""
    name = "测试"

    def __init__(self, texts, features=(), confirm=()):
        self.width, self.height = WIDTH, HEIGHT
        self.config = {"游戏语言": "繁体中文", "生命值大于多少优先闪光(百分比)": "60", "多少信用点以上冥想": 100,
                       "闪光卡牌列表": ["破碎"]}
        self.node_status = {"flash_or_rest": True, "shop": False}
        self.member_status = {"deck": {"冥想": {"剑雨": True}}}
        self.all_texts = [text(*t) for t in texts]
        self.features = {name: text(name, x, y) for name, x, y in features}
        self.confirm = [text(c, 0.570, 0.669, raw=True) for c in confirm]  # wait_ocr 的原始结果（不转简体）
        self.clicked, self.logs = [], []

    def show(self, texts):
        """下一帧：页面上的文字变成 texts。"""
        self.all_texts = [text(*t) for t in texts]

    def wait_ocr(self, *args, match=None, time_out=0, **kwargs):
        return [b for b in self.confirm if match.search(b.name)]

    def find_one(self, feature_name=None, box=None, threshold=0, **kwargs):
        return self.features.get(feature_name)

    def box_of_screen(self, x, y, to_x=1.0, to_y=1.0, **kwargs):
        return Box(x * WIDTH, y * HEIGHT, to_x=to_x * WIDTH, to_y=to_y * HEIGHT)

    def click_box(self, box=None, *args, **kwargs):
        self.clicked.append(box.name)

    def click(self, x=-1, y=-1, *args, **kwargs):
        self.clicked.append(f"({x:.3f}, {y:.3f})")

    def send_key(self, key, *args, **kwargs):
        self.clicked.append(key)

    def move_relative(self, x, y):
        pass

    def sleep(self, timeout):
        pass

    def log_info(self, message):
        self.logs.append(message)


# 出击模式休息区：闪光（费用 20）和免费休息都可选
SORTIE_REST = [("閃光", 0.83, 0.50), ("20", 0.83, 0.55), ("免費", 0.25, 0.70)]
SORTIE_FEATURES = [("flash_in_sortie_safezoom", 0.83, 0.50), ("rest", 0.30, 0.70)]
# 卡厄思模式休息区：免费休息和冥想（费用 50）都可选
CHAOS_REST = [("免費", 0.25, 0.70), ("50", 0.80, 0.70)]
CHAOS_FEATURES = [("rest", 0.30, 0.70), ("meditate", 0.80, 0.60)]
CREDIT = ("300", 0.794, 0.054)


def hp(current, maximum=1200):
    return (f"{current}/{maximum}", 0.209, 0.040)


class TestBossSelection(unittest.TestCase):

    def test_traditional_title_selects_a_boss(self):
        task = PageTask([("請選擇在核心遭遇的BOSS。", 0.484, 0.928, 0.25), ("靈魂收割者", 0.358, 0.706, 0.10),
                         ("瘟神", 0.641, 0.706)])
        self.assertTrue(utils_sortie.handle_boss_selection(task))
        self.assertEqual(1, len(task.clicked))
        self.assertIn(task.clicked[0], BOSS_POINTS)

    def test_simplified_title_still_works(self):
        task = PageTask([("请选择在核心遇见的首领", 0.484, 0.928, 0.25), ("灵魂收割者", 0.358, 0.706, 0.10)])
        self.assertTrue(utils_sortie.handle_boss_selection(task))
        self.assertEqual(["(0.358, 0.706)"], task.clicked)

    def test_other_page_is_ignored(self):
        self.assertFalse(utils_sortie.handle_boss_selection(PageTask([("請選擇功能。", 0.484, 0.928, 0.25)])))


class TestRestConfirm(unittest.TestCase):

    def test_traditional_and_simplified_confirm(self):
        self.assertTrue(utils._wait_for_rest_confirm(PageTask([], confirm=["確認"])))
        self.assertTrue(utils._wait_for_rest_confirm(PageTask([], confirm=["确认"])))
        task = PageTask([])
        self.assertFalse(utils._wait_for_rest_confirm(task))
        self.assertIn("等待休息确认按钮超时", task.logs)


class TestFlashButton(unittest.TestCase):

    def test_grant_flash_button_without_yi(self):
        # 实跑 19:20：选中卡牌后按钮亮起，OCR 读成「赋豫灵光闪」，没人点按钮，卡在选牌页
        task = PageTask([("賦豫靈光閃", 0.945, 0.918, 0.10)])
        task.config["游戏语言"] = "繁体中文"
        with unittest.mock.patch.object(utils, "is_button_active", lambda task, box: True):
            self.assertTrue(utils.handle_flash(task))
        self.assertEqual(1, len(task.clicked))

    def test_other_button_is_ignored(self):
        self.assertFalse(utils.handle_flash(PageTask([("跳過", 0.945, 0.918)])))


class TestGetCardEnhance(unittest.TestCase):

    def test_enhance_feature_on_name_row(self):
        # 实跑 19:35：强化图标模板匹配在牌名那一行（0.311），牌名框落空，三张强化牌全被排除，
        # 交给未知页面随机点击，拿到了意料之外的卡
        task = PageTask([("閃耀核心", 0.2135, 0.311), ("強化", 0.165, 0.350), ("獲得AP1", 0.16, 0.55)])
        enhance = text("enhance", 0.1159, 0.311, 0.012, 0.028)
        enhance.confidence = 0.80
        task.find_feature = lambda feature_name=None, **kwargs: [enhance] if feature_name == "enhance" else []
        cards = utils.recognize_cards(task, page="获得卡牌页面")
        self.assertEqual(["闪耀核心"], [c["name"] for c in cards])
        self.assertEqual("强化", cards[0]["type"])

    def test_shift_ignores_type_word(self):
        task = PageTask([("強化", 0.2135, 0.311)])
        enhance = text("enhance", 0.1159, 0.311, 0.012, 0.028)
        task.find_feature = lambda feature_name=None, **kwargs: [enhance] if feature_name == "enhance" else []
        self.assertEqual([], utils.recognize_cards(task))


class TestTreasureRoom(unittest.TestCase):

    def test_clicks_labeled_chests_then_gives_up(self):
        # 实跑 19:40：中间宝箱点开后，两边带 F1/F2 标记的宝箱没点就选了「离开」
        task = PageTask([("F1", 0.523, 0.387, 0.02), ("F2", 0.797, 0.392, 0.02), ("F", 0.20, 0.40, 0.02)])
        task.node_status["node_count"] = 5
        clicks = []
        with unittest.mock.patch.object(utils, "_move_and_click", lambda t, x, y: clicks.append((round(x, 3), round(y, 3)))):
            results = [utils._click_labeled_treasure(task) for _ in range(5)]
        self.assertEqual([True, True, True, True, False], results)
        self.assertEqual([(0.523, 0.427), (0.523, 0.427), (0.797, 0.432), (0.797, 0.432)], clicks)
        task.node_status["node_count"] = 6  # 换了节点重新计数
        with unittest.mock.patch.object(utils, "_move_and_click", lambda t, x, y: None):
            self.assertTrue(utils._click_labeled_treasure(task))


class TestSortieRest(unittest.TestCase):

    def rest_page(self, *texts):
        return PageTask(SORTIE_REST + list(texts), SORTIE_FEATURES, confirm=["確認"])

    def test_unreadable_hp_waits_for_next_frame(self):
        task = self.rest_page(CREDIT)
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual([], task.clicked)  # 读不到生命值：本帧不做选择
        task.show(SORTIE_REST + [CREDIT, hp(150)])  # 下一帧读到 12%
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["rest"], task.clicked)
        self.assertFalse(task.node_status["flash_or_rest"])  # 等到了「確認」，状态已复位

    def test_hp_never_readable_rests(self):
        task = self.rest_page(CREDIT)
        for _ in range(REST_READ_RETRIES):
            self.assertTrue(utils_sortie.handle_rest_sortie(task))
            self.assertEqual([], task.clicked)
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["rest"], task.clicked)  # 原来按 100% 处理会闪光

    def test_unreadable_credit_waits_for_next_frame(self):
        task = self.rest_page(hp(1100))
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual([], task.clicked)
        task.show(SORTIE_REST + [CREDIT, hp(1100)])
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["闪光"], task.clicked)  # 92% ≥ 60%，信用点够

    def test_readable_values_decide_immediately(self):
        task = self.rest_page(CREDIT, hp(1100))
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["闪光"], task.clicked)
        task = self.rest_page(CREDIT, hp(300))
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["rest"], task.clicked)

    def test_flash_list_done_rests_even_at_full_hp(self):
        # 本局列表里的牌都已不在选牌页：不再花信用点进闪光
        task = self.rest_page(CREDIT, hp(1200))
        task.node_status["flash_done_cards"] = ["破碎"]
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["rest"], task.clicked)

    def test_waits_for_flash_text_before_choosing_rest(self):
        # 实跑 09:47:58：闪光文字还没显示就点了休息，下一帧又点了闪光
        task = PageTask([("免費", 0.25, 0.70), CREDIT, hp(1200)], SORTIE_FEATURES, confirm=[])
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual([], task.clicked)
        task.show(SORTIE_REST + [CREDIT, hp(1200)])
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["闪光"], task.clicked)
        # 没等到确认按钮（点闪光后直接进选牌页），页面还没切走：不再点第二次
        self.assertTrue(utils_sortie.handle_rest_sortie(task))
        self.assertEqual(["闪光"], task.clicked)


class TestChaosRest(unittest.TestCase):

    def rest_page(self, *texts):
        return PageTask(CHAOS_REST + list(texts), CHAOS_FEATURES, confirm=["確認"])

    def test_unreadable_hp_retries_then_rests(self):
        task = self.rest_page(CREDIT)
        for _ in range(REST_READ_RETRIES):
            self.assertTrue(utils.handle_rest(task))
            self.assertEqual([], task.clicked)
        self.assertTrue(utils.handle_rest(task))
        self.assertEqual(["rest"], task.clicked)  # 原来会选冥想

    def test_readable_hp_decides_as_before(self):
        task = self.rest_page(CREDIT, hp(300))
        self.assertTrue(utils.handle_rest(task))
        self.assertEqual(["rest"], task.clicked)  # 25% < 50%
        task = self.rest_page(CREDIT, hp(1100))
        self.assertTrue(utils.handle_rest(task))
        self.assertEqual(["meditate"], task.clicked)


# 零式系统初始页面（2026-09-30 更新后的法典卡片：不再显示存储数据价值层级，改为存档储存上限）
ZERO_SYSTEM = [("零號系統", 0.120, 0.046), ("進入", 0.930, 0.910)]
REROLL = "(0.968, 0.153)"


def capacity(pt):
    return (f"存檔資料儲存上限{pt}pt", 0.800, 0.558, 0.20)


class TestZeroSystemCapacity(unittest.TestCase):

    def page(self, *texts, required=None):
        task = PageTask(ZERO_SYSTEM + list(texts))
        if required is not None:
            task.config[utils_chaos.STORAGE_CAPACITY_KEY] = required
        return task

    def test_capacity_meets_requirement_enters(self):
        task = self.page(capacity(170), required=150)
        self.assertFalse(utils_chaos.handle_zero_system_initial_page(task))
        self.assertEqual([], task.clicked)

    def test_capacity_below_requirement_rerolls(self):
        task = self.page(capacity(170), required=200)
        self.assertTrue(utils_chaos.handle_zero_system_initial_page(task))
        self.assertEqual([REROLL], task.clicked)

    def test_no_requirement_by_default(self):
        self.assertFalse(utils_chaos.handle_zero_system_initial_page(self.page(capacity(120))))

    def test_old_value_format_still_uses_level(self):
        task = self.page(("存檔資料價值11", 0.800, 0.558, 0.20))
        self.assertTrue(utils_chaos.handle_zero_system_initial_page(task))
        self.assertEqual([REROLL], task.clicked)

    def test_unreadable_waits_then_enters_without_reroll(self):
        task = self.page(required=200)
        for _ in range(utils_chaos._STORAGE_READ_RETRIES - 1):
            self.assertTrue(utils_chaos.handle_zero_system_initial_page(task))
        self.assertFalse(utils_chaos.handle_zero_system_initial_page(task))
        self.assertEqual([], task.clicked)  # 原来每帧都点重新合成


class TestZeroSystemCapacityPatch(unittest.TestCase):
    """装进官方版时由 speedup 接管：官方原函数读不到存储数据价值就一直重新合成。"""

    def setUp(self):
        self.original_calls = 0

        def official(task):
            self.original_calls += 1
            task.clicked.append(REROLL)
            return True

        self.handler = speedup._storage_capacity_wrapper(official)

    def test_capacity_decides_without_official_logic(self):
        task = PageTask(ZERO_SYSTEM + [capacity(170)])
        task.config[speedup.STORAGE_CAPACITY_KEY] = 150
        self.assertFalse(self.handler(task))
        task.config[speedup.STORAGE_CAPACITY_KEY] = 200
        self.assertTrue(self.handler(task))
        self.assertEqual([REROLL], task.clicked)
        self.assertEqual(0, self.original_calls)

    def test_old_format_and_other_pages_use_official_logic(self):
        self.handler(PageTask(ZERO_SYSTEM + [("存檔資料價值11", 0.800, 0.558, 0.20)]))
        self.handler(PageTask([("審判之沼", 0.740, 0.220)]))
        self.assertEqual(2, self.original_calls)

    def test_unreadable_does_not_reroll_forever(self):
        task = PageTask(ZERO_SYSTEM)
        results = [self.handler(task) for _ in range(speedup._STORAGE_READ_RETRIES)]
        self.assertEqual([True] * (speedup._STORAGE_READ_RETRIES - 1) + [False], results)
        self.assertEqual([], task.clicked)
        self.assertEqual(0, self.original_calls)


# 会合选主战员页面（国际服截图 2560x1440）：三张卡的等级、职能标签、名字、重新探索按钮
MEMBER_X = (0.130, 0.402, 0.674)          # 「等級」的位置；名字在它右边 0.188、下面 0.042
MEMBER_PROMPT = ("請選擇第2位加入的戰鬥員。", 0.49, 0.931, 0.25)


def member_page(*members):
    """members：每张卡的 (名字, 职能)。"""
    texts = [MEMBER_PROMPT]
    for x, (name, role) in zip(MEMBER_X, members):
        texts += [("等級", x, 0.683, 0.02), (role, x + 0.192, 0.675, 0.04), (name, x + 0.19, 0.722, 0.08),
                  ("重新探索", x + 0.04, 0.806, 0.07)]
    return texts


class TestMemberSelection(unittest.TestCase):
    """会合选主战员：优先级里的角色没有时，按本局已选队友的职能保持盾奶平衡
    （保护少优先保护、治疗少优先治疗，一样多优先保护），刷新时保留本轮优先的职能。"""

    def run_page(self, before, after=None, config=None, recruited=None):
        task = PageTask(member_page(*before))
        task.config.update({"主战员优先级": ["蕾伊"], "拉黑主战员": ["黛安娜"], **(config or {})})
        if recruited is not None:
            task.member_status["recruit_roles"] = dict(recruited)
        task.ocr = lambda *a, **k: [text(*t, raw=True) for t in member_page(*(after or before))]
        self.assertTrue(utils_sortie.handle_member_selection(task))
        return task.clicked

    @staticmethod
    def pick(index):
        return f"({MEMBER_X[index] + 0.188:.3f}, {0.683 + 0.042:.3f})"

    def test_keeps_one_survival_member_when_refreshing(self):
        # 截图：核心「菲」、支援「妮雅」、保护「瑪莉貝爾」，都不在优先级里：只刷新前两个，刷新后仍没有就选保护
        clicks = self.run_page([("菲", "核心"), ("妮雅", "支援"), ("瑪莉貝爾", "保護")],
                               [("凛", "核心"), ("海德瑪麗", "核心"), ("瑪莉貝爾", "保護")])
        self.assertEqual(3, len(clicks))  # 刷新 2 次 + 选人
        self.assertEqual(self.pick(2), clicks[-1])

    def test_protector_before_healer(self):
        clicks = self.run_page([("菲", "核心"), ("凛", "核心"), ("九", "核心")],
                               [("米卡", "治療"), ("凛", "核心"), ("麥格納", "保護")])
        self.assertEqual(4, len(clicks))  # 没有治疗/保护：三个都刷新
        self.assertEqual(self.pick(2), clicks[-1])

    def test_blacklisted_survival_member_skipped(self):
        clicks = self.run_page([("菲", "核心"), ("凛", "核心"), ("麥格納", "保護")],
                               [("米卡", "治療"), ("凛", "核心"), ("麥格納", "保護")], {"拉黑主战员": ["麦格纳"]})
        self.assertEqual(self.pick(0), clicks[-1])  # 保护角色被拉黑：选治疗

    def test_priority_still_first(self):
        clicks = self.run_page([("米卡", "治療"), ("蕾伊", "支援"), ("凛", "核心")])
        self.assertEqual([self.pick(1)], clicks)

    def test_second_pick_prefers_healer_after_shield(self):
        # 本局已选到保护（玛莉贝尔）：这一轮治疗优先，保留治疗（米卡）、刷掉保护和核心
        clicks = self.run_page([("菲", "核心"), ("米卡", "治療"), ("瑪莉貝爾", "保護")],
                               [("凛", "核心"), ("米卡", "治療"), ("海德瑪麗", "核心")],
                               recruited={"玛莉贝尔": "保护"})
        self.assertEqual(3, len(clicks))  # 刷新 2 次 + 选人
        self.assertEqual(self.pick(1), clicks[-1])

    def test_third_pick_back_to_shield_when_even(self):
        # 保护和治疗各一个：一样多优先保护，保留保护（玛莉贝尔）、刷掉治疗和核心
        clicks = self.run_page([("米卡", "治療"), ("凛", "核心"), ("瑪莉貝爾", "保護")],
                               [("凛", "核心"), ("海德瑪麗", "核心"), ("瑪莉貝爾", "保護")],
                               recruited={"玛莉贝尔": "保护", "米卡": "治疗"})
        self.assertEqual(3, len(clicks))  # 刷新 2 次 + 选人
        self.assertEqual(self.pick(2), clicks[-1])

    def test_blacklisted_preferred_role_falls_back(self):
        # 本轮偏好治疗但治疗（米卡）被拉黑：退回保留保护（玛莉贝尔）
        clicks = self.run_page([("米卡", "治療"), ("凛", "核心"), ("瑪莉貝爾", "保護")],
                               [("凛", "核心"), ("海德瑪麗", "核心"), ("瑪莉貝爾", "保護")],
                               config={"拉黑主战员": ["米卡"]},
                               recruited={"玛莉贝尔": "保护"})
        self.assertEqual(3, len(clicks))  # 刷新 2 次 + 选人
        self.assertEqual(self.pick(2), clicks[-1])

    def test_records_recruited_role_once(self):
        # 兜底选到治疗会记进 recruit_roles；页面停留再跑一遍不会重复计数
        page = (("米卡", "治療"), ("凛", "核心"), ("九", "核心"))
        task = PageTask(member_page(*page))
        task.config.update({"主战员优先级": ["蕾伊"], "拉黑主战员": ["黛安娜"]})
        task.member_status["recruit_roles"] = {"玛莉贝尔": "保护"}
        task.ocr = lambda *a, **k: [text(*t, raw=True) for t in member_page(*page)]
        self.assertTrue(utils_sortie.handle_member_selection(task))
        self.assertEqual({"玛莉贝尔": "保护", "米卡": "治疗"}, task.member_status["recruit_roles"])
        self.assertTrue(utils_sortie.handle_member_selection(task))
        self.assertEqual({"玛莉贝尔": "保护", "米卡": "治疗"}, task.member_status["recruit_roles"])

    def test_unreadable_role_not_counted(self):
        # 候选都认不出职能时随机选，不记账
        page = (("菲", ""), ("凛", ""), ("九", ""))
        task = PageTask(member_page(*page))
        task.config.update({"主战员优先级": ["蕾伊"], "拉黑主战员": ["黛安娜"]})
        task.member_status["recruit_roles"] = {}
        task.ocr = lambda *a, **k: [text(*t, raw=True) for t in member_page(*page)]
        self.assertTrue(utils_sortie.handle_member_selection(task))
        self.assertEqual({}, task.member_status["recruit_roles"])


# 事件剧情对话（截图 20260930-164914）：自动对话被关掉时停在同一句
DIALOG = [("1078/1193", 0.165, 0.030), ("CTRD", 0.880, 0.063), ("ALT", 0.950, 0.063),
          ("稍微往肉塊後方一瞧，地板上掉了一個小皮袋。", 0.483, 0.835, 0.40),
          ("似乎是某人急著躲藏時掉落的。", 0.483, 0.880, 0.25), ("SPACE", 0.935, 0.910)]


class TestEventDialog(unittest.TestCase):
    def run_frames(self, texts, times):
        task = PageTask(texts)
        task.keys = []
        task.send_key = task.keys.append
        now = [0]
        original = utils_sortie.time.time
        utils_sortie.time.time = lambda: now[0]
        try:
            handled = []
            for t in times:
                now[0] = t
                handled.append(utils_sortie.handle_event_dialog(task))
        finally:
            utils_sortie.time.time = original
        return task, handled

    def test_stalled_dialog_presses_space(self):
        task, handled = self.run_frames(DIALOG, [0, 1, 2.6, 3])
        self.assertEqual([False, False, True, False], handled)
        self.assertEqual(["space"], task.keys)

    def test_other_pages_ignored(self):
        texts = [t for t in DIALOG if t[0] != "SPACE"]
        task, handled = self.run_frames(texts, [0, 3, 6])
        self.assertEqual([False] * 3, handled)
        self.assertEqual([], task.keys)


# 获得法典页（2026-09-30 更新后）：选项 1、2 无法获得存档资料，选项 3 显示存档资料储存上限
def discovery(*capacities):
    texts = [("獲得法典", 0.50, 0.10, 0.08)]
    for cx, value in zip((0.19, 0.50, 0.81), capacities):
        texts.append((f"存檔資料儲存上限{value}pt" if value else "無法獲得存檔資料", cx, 0.59, 0.14))
    return texts


class TestDiscoverySelect(unittest.TestCase):

    def run_page(self, *capacities, required=140):
        task = PageTask(discovery(*capacities))
        task.config[utils_chaos.STORAGE_CAPACITY_KEY] = required
        with unittest.mock.patch.object(utils_chaos, "_move_and_click",
                                        lambda t, x, y: task.clicked.append((round(x, 2), round(y, 2)))):
            self.assertTrue(utils_chaos.handle_discovery_select(task))
        return task.clicked

    def test_picks_qualified_capacity(self):
        self.assertEqual([(0.81, 0.58)], self.run_page(None, None, 150))

    def test_picks_lowest_when_several_qualify(self):
        # 门槛 140，选项 160 / 150 / 170：只要达标就够，选最低的 150
        self.assertEqual([(0.5, 0.58)], self.run_page(160, 150, 170))
        self.assertEqual([(0.19, 0.58)], self.run_page(145, 170, 150))
        self.assertEqual([(0.81, 0.58)], self.run_page(120, 160, 150))   # 不达标的不算

    def test_rerolls_with_chaos_synthesis_when_none_qualify(self):
        self.assertEqual([(0.46, 0.92)], self.run_page(None, 120, 130))  # 「卡厄思合成」


# 「选择刻印的记忆」页（2145×1207 实跑截图）：标题被切成两行，三张记忆卡，确认按钮在右下角
MEMORY_PAGE = [("選擇刻印的記", 0.50, 0.09, 0.14), ("憶", 0.50, 0.14, 0.03),
               ("記憶：壓抑", 0.25, 0.24, 0.10), ("持續1回合觸發2次打擊次數為3次以上的攻擊卡牌時", 0.25, 0.60, 0.16),
               ("記憶：貪婪", 0.50, 0.24, 0.10), ("持續1回合觸發3次全體攻擊卡牌時", 0.50, 0.62, 0.16),
               ("記憶：女子", 0.75, 0.24, 0.10), ("持續1回合觸發3次快速攻擊卡牌時", 0.75, 0.62, 0.16),
               ("確認", 0.92, 0.93)]


# 实跑截图：第二张「渴望」的描述是「触发韧性伤害增加的攻击卡牌时…」，每张卡下方有「重新搜索 3/3」
MEMORY_PAGE_2 = [("選擇刻印的記", 0.50, 0.09, 0.14), ("憶", 0.50, 0.14, 0.03),
                 ("記憶：萬花筒", 0.25, 0.24, 0.10), ("持續1回合觸發3次持續傷害效果時，自身攻擊力", 0.25, 0.63, 0.16),
                 ("記憶：渴望", 0.50, 0.24, 0.10), ("觸發韌性傷害增加的攻擊卡牌時，自身攻擊力", 0.50, 0.63, 0.16),
                 ("記憶：女子", 0.75, 0.24, 0.10), ("持續1回合觸發3次快速攻擊卡牌時，自身攻擊力", 0.75, 0.63, 0.16),
                 ("重新搜索", 0.20, 0.824), ("3/3", 0.333, 0.824), ("重新搜索", 0.45, 0.824), ("3/3", 0.584, 0.824),
                 ("重新搜索", 0.70, 0.824), ("3/3", 0.835, 0.824), ("確認", 0.92, 0.93)]


class TestMemoryImprint(unittest.TestCase):

    def run_page(self, priority=(), default="1", active=False, page=MEMORY_PAGE):
        task = PageTask(page)
        task.config.update({speedup.MEMORY_PRIORITY_KEY: list(priority), speedup.MEMORY_DEFAULT_KEY: default})
        with unittest.mock.patch.object(utils, "is_button_active", lambda t, b: active), \
                unittest.mock.patch.object(utils, "_move_and_click", lambda t, x, y: task.clicked.append(
                    round(x, 2) if y == speedup._MEMORY_CARD_Y else ("重新搜索", round(x, 2)))):
            handled = speedup.handle_memory_imprint(task)
        return handled, task.clicked

    def test_priority_keyword_in_description(self):
        self.assertEqual((True, [0.5]), self.run_page(["全体攻击", "打击次数"]))

    def test_priority_by_name(self):
        self.assertEqual((True, [0.75]), self.run_page(["女子"]))

    def test_default_when_nothing_matches(self):
        self.assertEqual((True, [0.25]), self.run_page(["不存在"]))
        self.assertEqual((True, [0.75]), self.run_page([], default="3"))

    def test_default_priority_is_toughness_damage(self):
        speedup_default = list(speedup._MEMORY_DEFAULT_PRIORITY)
        self.assertEqual(["触发韧性伤害"], speedup_default)
        self.assertEqual((True, [0.5]), self.run_page(speedup_default, page=MEMORY_PAGE_2))

    def test_refresh_when_nothing_matches(self):
        self.assertEqual((True, [("重新搜索", 0.25)]), self.run_page(["贪婪"], page=MEMORY_PAGE_2))
        used_up = [t if t[0] != "3/3" or t[1] != 0.333 else ("0/3", 0.333, 0.824) for t in MEMORY_PAGE_2]
        self.assertEqual((True, [("重新搜索", 0.5)]), self.run_page(["贪婪"], page=used_up))

    def test_default_after_refreshes_used_up(self):
        used_up = [("0/3",) + t[1:] if t[0] == "3/3" else t for t in MEMORY_PAGE_2]
        self.assertEqual((True, [0.75]), self.run_page(["贪婪"], default="3", page=used_up))

    def test_selected_leaves_confirm_to_handle_confirm(self):
        self.assertEqual((False, []), self.run_page(["女子"], active=True))

    def test_other_page_ignored(self):
        self.assertFalse(speedup.handle_memory_imprint(PageTask([("裝備", 0.50, 0.13)])))

    def test_inserted_before_confirm(self):
        import utils_chaos
        handlers = list(utils_chaos.PAGE_HANDLERS)
        try:
            speedup._patch_memory_imprint()
            speedup._patch_memory_imprint()
            names = [h.__name__ for h in utils_chaos.PAGE_HANDLERS]
            self.assertEqual(1, names.count("handle_memory_imprint"))
            self.assertEqual(names.index("handle_confirm") - 1, names.index("handle_memory_imprint"))
        finally:
            utils_chaos.PAGE_HANDLERS[:] = handlers


class TestThreatDetection(unittest.TestCase):
    """实跑 9/30 23:15：零式系统选完记忆卡后弹出「威胁侦测」，点屏幕关不掉，卡了 4 分多钟。"""

    def test_traditional_title_presses_esc(self):
        task = PageTask([("威脅偵測", 0.500, 0.845, 0.08), ("記憶的盡頭BOSS怪物已變更為渴望的啟動。", 0.500, 0.910, 0.36)])
        self.assertTrue(utils_chaos.handle_threat_detection(task))
        self.assertEqual(["esc"], task.clicked)

    def test_other_page_is_ignored(self):
        self.assertFalse(utils_chaos.handle_threat_detection(PageTask([("記憶的盡頭", 0.500, 0.910)])))


class TestEquipmentSlotColor(unittest.TestCase):
    """主战员装备格取格子右上角的底色（实跑中取色点在格子上方的卡片背景上，杂色全被算成传说，装备全给了别人）。"""

    def quality(self, rgb):
        import numpy as np
        frame = np.zeros((1207, 2145, 3), np.uint8)
        frame[:, :] = rgb[::-1]  # BGR
        task = unittest.mock.Mock(frame=frame, width=2145, height=1207)
        return utils._slot_quality(task, (0.759, 0.320))[0]

    def test_colors(self):
        self.assertEqual("", self.quality((30, 33, 36)))       # 空槽：暗灰
        self.assertEqual("", self.quality((90, 92, 96)))       # 空槽透出立绘：灰
        self.assertEqual("稀有", self.quality((61, 76, 138)))
        self.assertEqual("传说", self.quality((160, 88, 69)))
        self.assertEqual("独特", self.quality((136, 96, 184)))  # 紫底，实跑读数
        self.assertEqual("独特", self.quality((103, 77, 144)))
        self.assertIsNone(self.quality((40, 160, 60)))          # 认不出的颜色不再算成最高品质

    def test_slots_level_with_level_tag(self):
        points = []
        with unittest.mock.patch.object(utils, "_slot_quality", lambda task, p: points.append(p) or ("", None)):
            task = unittest.mock.Mock(width=2145, height=1207, _equipment_page_shot=True)
            utils._member_equipment_qualities(task, Box(1330, 377, 40, 20))
        self.assertAlmostEqual(0.321, points[0][1], places=2)
        self.assertEqual([0.759, 0.829, 0.899], [round(p[0], 3) for p in points])



QUALITY_RGB = {"": (20, 22, 21), "稀有": (72, 89, 160), "传说": (165, 110, 86), "独特": (136, 96, 184)}
MEMBER_Y = (0.33, 0.55, 0.77)   # 三个主战员「等级」标签的纵坐标
LEVEL_X = 0.63
EMPTY = ("", "", "")


class EquipmentTask(PageTask):
    """安装装备页：画面按 slots（三个主战员各三格的品质）和新装备品质上色，刷存档主战员默认是第 2 号。"""

    def __init__(self, name, kind, quality, slots, target=1, config=None, extra=(), purchase=False):
        texts = [("装备", 0.499, 0.126), ("请选择主战员", 0.921, 0.135, 0.10),
                 (name, 0.30, 0.405, 0.10), (kind, 0.25, 0.466), ("战斗开始时获得护盾", 0.35, 0.55, 0.20),
                 *extra]
        if purchase:
            texts += [("取消", 0.70, 0.94), ("购买", 0.90, 0.94), ("100", 0.80, 0.94)]
        else:
            texts += [("提炼", 0.65, 0.94), ("确认", 0.90, 0.94)]
        super().__init__(texts)
        self.config = {"游戏语言": "简体中文", "装备1号位优先级": [], "装备2号位优先级": [], "装备3号位优先级": [],
                       **(config or {})}
        self.default_config = {"刷存档主战员": True}
        self._equipment_page_shot = True
        self.level_tags = [Box(LEVEL_X * WIDTH - 20, y * HEIGHT - 10, 40, 20, confidence=0.9, name="leveltag")
                           for y in MEMBER_Y]
        self.target = Box(0.67 * WIDTH, MEMBER_Y[target] * HEIGHT, 30, 30, confidence=0.9, name="target")
        import numpy as np
        self.frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        self.paint(0.117, 0.409, 0.004, 0.004, QUALITY_RGB.get(quality, quality))
        for member_y, member_slots in zip(MEMBER_Y, slots):
            for offset, slot_quality in zip((0.130, 0.200, 0.270), member_slots):
                self.paint(LEVEL_X + offset + 0.019, member_y - 0.044, 0.010, 0.008, QUALITY_RGB[slot_quality])

    def paint(self, cx, cy, half_w, half_h, rgb):
        self.frame[int((cy - half_h) * HEIGHT):int((cy + half_h) * HEIGHT),
                   int((cx - half_w) * WIDTH):int((cx + half_w) * WIDTH)] = rgb[::-1]

    def find_feature(self, feature_name=None, **kwargs):
        return list(self.level_tags) if feature_name == "leveltag" else []

    def feature_exists(self, name):
        return name == "target_member_tiny"

    def find_one(self, feature_name=None, **kwargs):
        return self.target if feature_name == "target_member_tiny" else None

    def chosen(self):
        """点了第几号主战员（1 起），或点的按钮文字。"""
        members = [f"(0.756, {y:.3f})" for y in MEMBER_Y]
        return [members.index(c) + 1 if c in members else c for c in self.clicked]


class TestEquipmentAssign(unittest.TestCase):
    """卡厄思模式安装装备页：独特名额、分给别人、提炼、购买、读不出品质、同一页停好几帧。"""

    def setUp(self):
        self.now = 1000.0
        patcher = unittest.mock.patch.object(utils.time, "time", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_page(self, *args, **kwargs):
        task = EquipmentTask(*args, **kwargs)
        return task, utils.handle_equipment(task)

    def test_teammate_empty_beats_target_upgrade(self):
        # 用户 10/03 定的顺序：队友这一格空着时先补队友，再轮到给主战员的品质升级
        task, _ = self.run_page("战斗服", "防御力", "传说", [EMPTY, ("", "稀有", ""), EMPTY])
        self.assertEqual([1], task.chosen())

    def test_target_upgrade_when_teammates_cannot_take_it(self):
        task, _ = self.run_page("战斗服", "防御力", "传说",
                                [("", "传说", ""), ("", "稀有", ""), ("", "传说", "")])
        self.assertEqual([2], task.chosen())

    def test_target_upgrade_beats_lower_quality_teammate(self):
        # 队友只是「这一格比它差」而不是空的：升级仍归主战员（空位才压升级）
        task, _ = self.run_page("战斗服", "防御力", "传说",
                                [("", "稀有", ""), ("", "稀有", ""), ("", "稀有", "")])
        self.assertEqual([2], task.chosen())

    def test_empty_slot_tie_goes_to_target(self):
        # 空位撞空位仍归主战员（用户 10/03 选 b）
        task, _ = self.run_page("战斗服", "防御力", "稀有", [EMPTY, EMPTY, EMPTY])
        self.assertEqual([2], task.chosen())

    def test_configured_beats_teammate_empty(self):
        # 装备列表永远第一优先：配置里的装备给主战员，哪怕队友这一格空着
        task, _ = self.run_page("短刀", "攻击力", "稀有", [EMPTY, ("传说", "", ""), ("稀有", "", "")],
                                config={"装备1号位优先级": ["短刀"]})
        self.assertEqual([2], task.chosen())

    def test_configured_unique_goes_to_target(self):
        task, _ = self.run_page("拷问工具箱", "防御力", "独特", [EMPTY, ("", "传说", ""), EMPTY],
                                config={"装备2号位优先级": ["拷问工具箱"]})
        self.assertEqual([2], task.chosen())

    def test_unconfigured_unique_kept_for_configured(self):
        task, _ = self.run_page("深绿桎梏", "生命值", "独特", [("传说", "", ""), EMPTY, ("", "", "传说")],
                                config={"装备2号位优先级": ["拷问工具箱"]})
        self.assertEqual([1], task.chosen())   # 刷存档主战员不要；第 1 号这一格空着

    def test_unique_free_for_all_without_config(self):
        task, _ = self.run_page("深绿桎梏", "生命值", "独特", [EMPTY, EMPTY, EMPTY])
        self.assertEqual([2], task.chosen())

    def test_second_configured_unique_goes_elsewhere(self):
        task, _ = self.run_page("异象石碑", "生命值", "独特", [EMPTY, ("独特", "", ""), ("", "", "稀有")],
                                config={"装备1号位优先级": ["蚀化臂铠"], "装备3号位优先级": ["异象石碑"]})
        self.assertEqual([1], task.chosen())

    def test_others_get_empty_then_lowest_slot(self):
        task, _ = self.run_page("短刀", "攻击力", "传说", [("稀有", "", ""), ("传说", "", ""), EMPTY])
        self.assertEqual([3], task.chosen())   # 第 3 号这一格空着
        task, _ = self.run_page("短刀", "攻击力", "传说", [("传说", "", ""), ("独特", "", ""), ("稀有", "", "")])
        self.assertEqual([3], task.chosen())   # 品质比它低的只有第 3 号

    def test_tie_goes_to_recommended(self):
        task, _ = self.run_page("短刀", "攻击力", "稀有", [EMPTY, ("传说", "", ""), EMPTY],
                                extra=[("推荐", 0.92, 0.70)])
        self.assertEqual([3], task.chosen())
        task, _ = self.run_page("短刀", "攻击力", "稀有", [EMPTY, ("传说", "", ""), EMPTY])
        self.assertEqual([1], task.chosen())   # 没有「推荐」时给站位靠前的

    def test_nobody_needs_it_refines(self):
        task, result = self.run_page("短刀", "攻击力", "稀有", [("传说", "", ""), ("传说", "", ""), ("稀有", "", "")])
        self.assertTrue(result)
        self.assertEqual(["提炼"], task.chosen())

    def test_unique_not_given_to_member_with_unique(self):
        task, _ = self.run_page("深绿桎梏", "生命值", "独特", [("独特", "", ""), EMPTY, ("", "独特", "")],
                                config={"装备2号位优先级": ["拷问工具箱"]})
        self.assertEqual(["提炼"], task.chosen())

    def sortie_task(self, name, kind, quality, slots):
        """出击模式：2 人队、没有「刷存档主战员」，主角是第一主战员。"""
        task = EquipmentTask(name, kind, quality, slots)
        task.default_config = {}
        task.level_tags = task.level_tags[:2]
        return task

    def test_sortie_rare_does_not_replace_unique(self):
        # 实跑 10/03 13:56：出击模式里稀有的「角斗士头盔」被随机装给第 2 主战员，顶掉了她 2 号位
        # 刚装上的独特装备。修后品质更低不顶掉，走提炼
        task = self.sortie_task("变异：角斗士头盔", "防御力", "稀有",
                                [("", "稀有", ""), ("", "独特", "")])
        self.assertTrue(utils.handle_equipment(task))
        self.assertEqual(["提炼"], task.chosen())

    def test_sortie_better_quality_replaces(self):
        # 品质更高才顶掉：传说替换第 2 人 2 号位的稀有（装给其他人后返回 False，走同页决策缓存）
        task = self.sortie_task("变异：角斗士头盔", "防御力", "传说",
                                [("", "传说", ""), ("", "稀有", "")])
        utils.handle_equipment(task)
        self.assertEqual([2], task.chosen())

    def test_sortie_fills_empty_slot_first(self):
        # 这一格空着最优先：第 2 人 2 号位空着，直接装进去
        task = self.sortie_task("变异：角斗士头盔", "防御力", "稀有",
                                [("", "传说", ""), ("", "", "")])
        utils.handle_equipment(task)
        self.assertEqual([2], task.chosen())

    def test_sortie_teammate_empty_beats_first_member_upgrade(self):
        # 出击（无刷存档主战员）：队友这一格空着时先补队友，再轮到第一主战员升级
        task = self.sortie_task("变异：角斗士头盔", "防御力", "传说",
                                [("", "稀有", ""), ("", "", "")])
        utils.handle_equipment(task)
        self.assertEqual([2], task.chosen())

    def test_purchase_cancelled_when_nobody_needs_it(self):
        with unittest.mock.patch.object(utils, "_get_current_credit", lambda task: 300):
            task, result = self.run_page("短刀", "攻击力", "稀有",
                                         [("稀有", "", ""), ("传说", "", ""), ("传说", "", "")], purchase=True)
        self.assertTrue(result)
        self.assertEqual(["取消"], task.chosen())
        # 实跑 10/01 12:09：认出了主战员、确实不值得买，商店本轮不再点它，免得商店 ↔ 购买页来回
        self.assertEqual(["短刀"], task.node_status["shop_cancelled"])

    def test_purchase_buys_for_empty_teammate_slot(self):
        # 用户 10/03：能补队友空位的也买
        with unittest.mock.patch.object(utils, "_get_current_credit", lambda task: 300):
            task, _ = self.run_page("短刀", "攻击力", "稀有", [EMPTY, ("传说", "", ""), EMPTY], purchase=True)
        self.assertEqual([1, "购买"], task.chosen())

    def test_purchase_buys_for_target_upgrade(self):
        with unittest.mock.patch.object(utils, "_get_current_credit", lambda task: 300):
            task, _ = self.run_page("短刀", "攻击力", "传说",
                                    [("传说", "", ""), ("稀有", "", ""), ("传说", "", "")], purchase=True)
        self.assertEqual([2, "购买"], task.chosen())

    def test_accelerated_purchase_confirms_on_the_next_frame(self):
        # 加速时一帧只点主战员，下一帧再点购买，不再在同一帧里把人选重来一遍
        task = EquipmentTask("短刀", "攻击力", "稀有", [EMPTY, ("传说", "", ""), EMPTY], purchase=True)
        task.config["加速模式"] = True
        with unittest.mock.patch.object(utils, "_get_current_credit", lambda task: 300):
            self.assertTrue(utils.handle_equipment(task))
            self.assertEqual([1], task.chosen())
            self.now += 0.2
            self.assertTrue(utils.handle_equipment(task))
        self.assertEqual([1, "购买"], task.chosen())

    def test_unknown_target_refetches_then_gives_up(self):
        # 实跑 10/01 11:02：开局头像没取准，购买页认不出刷存档主战员一直取消，商店又一直去点同一件
        task = EquipmentTask("短刀", "攻击力", "传说", [EMPTY, EMPTY, EMPTY], purchase=True,
                             config={"装备1号位优先级": ["短刀"]})
        task.target = None
        task.node_status["save_target_member"] = True
        with unittest.mock.patch.object(utils, "_get_current_credit", lambda task: 300):
            for _ in range(utils._TARGET_MISS_GIVE_UP):
                self.assertTrue(utils.handle_equipment(task))
        self.assertEqual(["取消"] * (utils._TARGET_MISS_GIVE_UP - 1) + ["esc"], task.chosen())
        self.assertFalse(task.node_status["save_target_member"])     # 中途重新获取过头像
        self.assertEqual(["短刀"], task.node_status["shop_cancelled"])  # 商店本轮不再点它

    def test_fading_slot_is_read_again(self):
        # 实跑 10/01 11:53：页面刚出来卡片还在淡入，装着「非典型方块」的格子读成空，记录被清掉
        task = EquipmentTask("森林三叶草", "生命值", "稀有", [EMPTY, EMPTY, EMPTY])
        task.member_status = {"equipment": {"names": ["", "", "非典型方块"], "descriptions": ["", "", ""],
                                            "qualities": ["", "", "稀有"]}, "deck": {}}
        self.assertTrue(utils.handle_equipment(task))
        self.assertEqual([], task.chosen())                              # 先不信「空」，等下一帧
        task.paint(LEVEL_X + 0.270 + 0.019, MEMBER_Y[1] - 0.044, 0.010, 0.008, QUALITY_RGB["稀有"])
        utils.handle_equipment(task)
        self.assertEqual("非典型方块", task.member_status["equipment"]["names"][2])
        self.assertEqual([1], task.chosen())                             # 同品质不换，给 3 号位空着的第 1 号

    def test_unreadable_quality_waits_then_counts_as_rare(self):
        task = EquipmentTask("短刀", "攻击力", (31, 34, 33), [EMPTY, EMPTY, EMPTY])
        for _ in range(utils._EQUIPMENT_QUALITY_RETRIES - 1):
            self.assertTrue(utils.handle_equipment(task))
            self.assertEqual([], task.chosen())
        utils.handle_equipment(task)
        self.assertEqual([2], task.chosen())   # 按稀有，装进空格

    def test_same_page_decided_once(self):
        # 实跑 10/01 09:22：下一帧拿刚记下的自己比较，又把同一件装备转给了别人
        task = EquipmentTask("M85军用手榴弹", "攻击力", "稀有", [EMPTY, EMPTY, EMPTY])
        self.assertFalse(utils.handle_equipment(task))
        self.now += 1
        self.assertFalse(utils.handle_equipment(task))
        self.assertEqual([2], task.chosen())
        self.now += utils._EQUIPMENT_DECISION_KEEP
        task.paint(LEVEL_X + 0.130 + 0.019, MEMBER_Y[1] - 0.044, 0.010, 0.008, QUALITY_RGB["稀有"])  # 已装上
        utils.handle_equipment(task)   # 过了时限：当成新的一件重新判断
        self.assertEqual(2, len(task.chosen()))



class TestInfoOption(unittest.TestCase):
    """实跑 10/01 10:05：「询问方法 / 確認【盜獵者的樂趣】資訊」看过后变灰点不动，
    「页面中央确认」把切出来的「確認【」当成确认按钮，事件页又一直选它，卡了 5 分钟。"""

    OPTIONS = [("詢問方法", 0.16, 0.80), ("確認【", 0.15, 0.836), ("盜獵者的樂趣】資訊", 0.24, 0.836, 0.12),
               ("參加打賭", 0.42, 0.80), ("觸發【與盜獵者的打賭】事件", 0.49, 0.836, 0.16),
               ("用謊言來警告", 0.68, 0.80), ("信用點數80增加", 0.71, 0.836, 0.10)]

    def test_bracket_text_is_not_confirm_button(self):
        task = PageTask(self.OPTIONS)
        self.assertFalse(utils.handle_center_confirm(task))
        self.assertTrue(utils.handle_center_confirm(PageTask([("確認", 0.50, 0.70)])))

    def test_info_option_skipped(self):
        def option(description, x, y):
            return {"x": x, "y": y, "description": utils._normalize_text(description),
                    "description_region": (x - 0.1, 0.78, x + 0.1, 0.86), "feature_name": "event3", "confidence": 0.9}
        options = [option("詢問方法確認【盜獵者的樂趣】資訊", 0.24, 0.886),   # 灰色选项被当成已选中
                   option("參加打賭觸發【與盜獵者的打賭】事件", 0.50, 0.947),
                   option("用謊言來警告信用點數80增加", 0.76, 0.947)]
        task = PageTask(self.OPTIONS)
        task.find_feature = lambda *args, **kwargs: []
        with unittest.mock.patch.object(utils, "recognize_event_options", lambda *args, **kwargs: options):
            utils.handle_event_task(task)
        self.assertNotIn("(0.240, 0.820)", task.clicked)
        self.assertTrue(task.clicked)



class TestDiceReroll(unittest.TestCase):
    """掷骰失败：右上角持有数够付重新掷骰的费用就重掷，否则下一步。"""

    def page(self, owned, cost="2"):
        return PageTask([("擲骰", 0.50, 0.15), ("7", 0.50, 0.22), ("失敗", 0.498, 0.678),
                         (f"{owned}/20+", 0.92, 0.103, 0.06), ("重新擲骰", 0.267, 0.90, 0.07),
                         (cost, 0.466, 0.90, 0.01), ("下一步", 0.665, 0.899)])

    def test_rerolls_when_enough(self):
        task = self.page(20)
        self.assertTrue(utils.handle_negotiation(task))
        self.assertEqual(["重新掷骰"], task.clicked)

    def test_next_step_when_not_enough_or_unreadable(self):
        for task in (self.page(1), self.page(20, cost="")):
            self.assertTrue(utils.handle_negotiation(task))
            self.assertEqual(["(0.665, 0.899)"], task.clicked)


class TestDontShowAgain(unittest.TestCase):
    """带「今天不再显示」勾选框的确认框（如获得神之灵光一闪的卡牌）：先勾上，再点确认。"""

    TEXTS = [("獲得被賦予神之靈光一閃的卡牌時，模糊的記憶將增加20pt。", 0.50, 0.352, 0.48),
             ("今天不再顯示", 0.521, 0.582, 0.095), ("取消", 0.347, 0.689), ("確認", 0.665, 0.689)]

    def run_page(self, box_rgb):
        import numpy as np
        task = PageTask(self.TEXTS)
        task.frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        x, y = int(0.4445 * WIDTH), int(0.582 * HEIGHT)
        task.frame[y - 25:y + 25, x - 25:x + 25] = box_rgb[::-1]
        self.assertTrue(utils.handle_dont_show_again(task))
        return task.clicked

    def test_unchecked_box_is_ticked_then_confirmed(self):
        clicked = self.run_page((200, 200, 200))
        self.assertEqual(2, len(clicked))
        self.assertAlmostEqual(0.445, float(clicked[0].strip("()").split(",")[0]), places=2)
        self.assertEqual("确认", clicked[1])

    def test_checked_box_is_left_alone(self):
        self.assertEqual(["确认"], self.run_page((235, 125, 40)))   # 已勾上是橙色

    def test_other_pages_ignored(self):
        self.assertFalse(utils.handle_dont_show_again(PageTask([("確認", 0.665, 0.689)])))

    def test_enter_button(self):
        # 实跑 10/01 10:53：「确认准备战斗说明」的按钮是「進入」，只认「确认」时每帧都占住，卡住后被 ESC 关掉再重开
        import numpy as np
        task = PageTask([("確認準備戰鬥說明", 0.50, 0.17, 0.16), ("今天不再顯示", 0.521, 0.722, 0.095),
                         ("取消", 0.347, 0.83), ("進入", 0.665, 0.83)])
        task.frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        x, y = int(0.4445 * WIDTH), int(0.722 * HEIGHT)
        task.frame[y - 25:y + 25, x - 25:x + 25] = (41, 120, 218)   # 已勾上（BGR）
        self.assertTrue(utils.handle_dont_show_again(task))
        self.assertEqual(["进入"], task.clicked)

    def test_no_button_does_not_hold_the_frame(self):
        import numpy as np
        task = PageTask([("今天不再顯示", 0.521, 0.722, 0.095)])
        task.frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        x, y = int(0.4445 * WIDTH), int(0.722 * HEIGHT)
        task.frame[y - 25:y + 25, x - 25:x + 25] = (41, 120, 218)
        self.assertFalse(utils.handle_dont_show_again(task))



class TestMemoryCarving(unittest.TestCase):
    """零式系统 boss 后：事件页选「雕琢记忆」，记忆雕琢页一直点「记忆雕琢」，按钮变灰后离开。"""

    PAGE = [("記憶雕琢", 0.501, 0.128), ("記憶加工", 0.14, 0.33), ("記憶雕琢", 0.845, 0.33),
            ("雕琢成功機率30%", 0.852, 0.392, 0.12), ("記憶加工", 0.10, 0.795), ("1", 0.25, 0.795),
            ("記憶雕琢", 0.805, 0.795), ("1", 0.955, 0.795), ("離開", 0.95, 0.928)]

    def run_page(self, button_rgb):
        import numpy as np
        task = PageTask(self.PAGE)
        task.frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        x, y = int(0.86 * WIDTH), int(0.795 * HEIGHT)
        task.frame[y - 20:y + 20, x - 20:x + 20] = button_rgb[::-1]
        self.assertTrue(utils_chaos.handle_memory_carving(task))
        return task.clicked

    def test_carves_while_button_orange(self):
        self.assertEqual(["记忆雕琢"], self.run_page((235, 140, 50)))

    def test_leaves_when_button_gray(self):
        self.assertEqual(["离开"], self.run_page((200, 200, 200)))

    def run_finished_page(self, processed=False):
        """雕琢完成后：右边变成「雕琢完成」，左边「记忆加工」变橙色可点。"""
        import numpy as np
        page = [t for t in self.PAGE if t[0] != "記憶雕琢" or t[2] != 0.795] + [("雕琢完成", 0.85, 0.795)]
        task = PageTask(page)
        task.node_status["memory_processed"] = processed
        task.frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        for px in (0.86, 0.16):
            x, y = int(px * WIDTH), int(0.795 * HEIGHT)
            task.frame[y - 20:y + 20, x - 20:x + 20] = (50, 140, 235)
        self.assertTrue(utils_chaos.handle_memory_carving(task))
        return task

    def test_processes_once_after_carving_finished(self):
        task = self.run_finished_page()
        self.assertEqual(["记忆加工"], task.clicked)
        self.assertTrue(task.node_status["memory_processed"])

    def test_leaves_after_processing(self):
        task = self.run_finished_page(processed=True)
        self.assertEqual(["离开"], task.clicked)
        self.assertTrue(task.node_status["carve_done"])

    def run_processing(self, priority=None):
        """截图里的记忆加工页：伤害量+10% / 攻击力+4% / 伤害量+6%，返回选中的卡的 x。"""
        task = PageTask([("記憶加工", 0.501, 0.13),
                         ("觸發韌性傷害增加的攻擊", 0.255, 0.648), ("卡牌時，自身傷害量增", 0.255, 0.684),
                         ("加10%（每回合1次，每", 0.255, 0.72),
                         ("觸發韌性傷害增加的攻擊", 0.505, 0.648), ("卡牌時，攻擊力+4%（", 0.5, 0.684),
                         ("觸發韌性傷害增加的攻擊", 0.757, 0.648), ("卡牌時，傷害量增", 0.75, 0.684),
                         ("加6%（每回合1次，每", 0.75, 0.72), ("確認", 0.83, 0.928)])
        if priority is not None:
            task.config["记忆加工优先级"] = priority
        self.assertTrue(utils_chaos.handle_memory_processing(task))
        self.assertEqual("(0.830, 0.928)", task.clicked[-1])
        return task.clicked[0]

    def test_processing_picks_highest_percent_without_priority(self):
        self.assertEqual("(0.249, 0.500)", self.run_processing())

    def test_processing_picks_keyword_first(self):
        self.assertEqual("(0.500, 0.500)", self.run_processing(["攻击力", "伤害量"]))

    def test_processing_keyword_tie_picks_higher_percent(self):
        self.assertEqual("(0.249, 0.500)", self.run_processing(["暴击", "伤害量"]))

    def run_event(self, carve_done=False):
        def option(description, x, y):
            return {"x": x, "y": y, "description": description,
                    "description_region": (x - 0.1, 0.78, x + 0.1, 0.86), "feature_name": "event3", "confidence": 0.9}
        options = [option("雕琢记忆奉行既定的启示", 0.37, 0.947), option("离开事件结束", 0.63, 0.947)]
        task = PageTask([])
        task.node_status["carve_done"] = carve_done
        task.config["任务/装备优先级"] = ["结束"]
        task.find_feature = lambda *args, **kwargs: []
        with unittest.mock.patch.object(utils, "recognize_event_options", lambda *args, **kwargs: options):
            self.assertTrue(utils.handle_event_task(task))
        return task.clicked

    def test_event_page_picks_carving(self):
        self.assertEqual(["(0.370, 0.820)"], self.run_event())

    def test_event_page_leaves_after_carving(self):
        self.assertEqual(["(0.630, 0.820)"], self.run_event(carve_done=True))


if __name__ == '__main__':
    unittest.main()
