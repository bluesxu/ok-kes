# 自动出击模式出牌（ok_tasks/utils_battle.py）与战斗日志（ok_tasks/battle_log.py）测试
# 识别部分用 tests/images/battle 下的真实战斗截图 + 真实 OCR；决策部分是纯函数测试
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ok_tasks"))

from ok import Box  # noqa: E402

import battle_log  # noqa: E402
import utils  # noqa: E402
import utils_battle as battle  # noqa: E402

IMAGES = os.path.join(ROOT, "tests", "images", "battle")

# 截图里的真实值（人工核对）：按键 → (牌名, 费用, 类型)
TRUTH = {
    "boss_full_hp": {
        "hand": 7, "remaining": 4, "hp": (1869, 1869), "shield": 78, "red": False,
        "enemies": [(7441, None)],
        "cards": {"1": ("作战分析", 0, "强化"), "2": ("水之根源", 1, "技能"), "3": ("重新集结", 1, "技能"),
                  "4": ("秃鹰发射", 3, "攻击"), "5": ("逆转之刃", 1, "强化"), "6": ("破碎", 3, "攻击"),
                  "7": ("孢子采集器", 0, "技能")},
    },
    "boss_minions_red": {
        "hand": 10, "remaining": 4, "hp": (1869, 1869), "shield": 101, "red": True,
        "enemies": [(1254, 8), (1254, 8), (6078, None)],
        "cards": {"1": ("水之根源", 1, "技能"), "2": ("秃鹰发射", 3, "攻击"), "3": ("秃鹰发射", 3, "攻击"),
                  "4": ("秃鹰发射", 3, "攻击"), "5": ("秃鹰发射", 3, "攻击"), "6": ("斗志", 1, "技能"),
                  "7": ("斗志", 1, "技能"), "8": ("破碎", 3, "攻击"), "9": ("孢子", 0, "技能"),
                  "0": ("被污染的孢子", 0, "技能")},
    },
    "single_enemy": {
        "hand": 3, "remaining": 2, "hp": (1869, 1869), "shield": 319, "red": False,
        "enemies": [(990, 5)],
        "cards": {"1": ("泛滥", 1, "强化"), "2": ("水之根源", 1, "技能"), "3": ("破碎", 3, "攻击")},
    },
    "modified_cost": {
        "hand": 5, "remaining": 3, "hp": (1594, 1594), "shield": 0, "red": False,
        "enemies": [(599, 10), (599, 10)],
        "cards": {"1": ("鞭打", 2, "攻击"), "2": ("上斩", 2, "攻击"), "3": ("秃鹰发射", 4, "攻击"),
                  "4": ("斩击", 1, "攻击"), "5": ("破碎", 3, "攻击")},
    },
    # 角色崩溃：手里有两张崩溃牌「冲动」（费用位置显示 1/5）；场上另有 4 个「无法攻击」的捕兽夹，不算敌人
    "collapse_traps": {
        "hand": 6, "remaining": 3, "hp": (1389, 1560), "shield": 40, "red": False,
        "enemies": [(2622, 4)],
        "cards": {"1": ("扭曲：光荣的抵抗", 2, "强化"), "2": ("冲动", None, "崩溃"), "3": ("冲动", None, "崩溃"),
                  "4": ("斩击", 1, "攻击"), "5": ("斗志", 2, "技能"), "6": ("破碎", 3, "攻击")},
    },
}

_engine = None


def _ocr_boxes(image, ox=0, oy=0):
    global _engine
    if _engine is None:
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr
        _engine = ONNXPaddleOcr(use_angle_cls=False, use_openvino=False)
    boxes = []
    for points, (text, confidence) in _engine.ocr(image)[0] or []:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        boxes.append(Box(int(min(xs)) + ox, int(min(ys)) + oy, int(max(xs) - min(xs)), int(max(ys) - min(ys)),
                         confidence=confidence, name=text))
    return boxes


def screenshot_task(name):
    """用截图造一个任务：frame、全屏 OCR 结果、ocr() 与真实任务的用法一致。"""
    frame = cv2.imdecode(np.fromfile(os.path.join(IMAGES, name + ".jpg"), dtype=np.uint8), cv2.IMREAD_COLOR)
    height, width = frame.shape[:2]
    task = SimpleNamespace(frame=frame, width=width, height=height, log_info=lambda message: None)

    def ocr(x=0, y=0, to_x=1, to_y=1, frame=None, **kwargs):
        if frame is not None:
            return _ocr_boxes(frame)
        crop = task.frame[int(y * height):int(to_y * height), int(x * width):int(to_x * width)]
        return _ocr_boxes(crop, int(x * width), int(y * height))

    task.ocr = ocr
    task.all_texts = utils._simplify_texts(_ocr_boxes(frame))
    return task


class TestBattlePerception(unittest.TestCase):
    """识别：读错比读不到更糟（读不到还有「AP不足」兜底），所以读出来的值必须正确。"""

    def test_screens(self):
        costs_read = 0
        for name, truth in TRUTH.items():
            with self.subTest(name):
                task = screenshot_task(name)
                self.assertEqual(truth["remaining"], battle.read_remaining_cost(task, task.frame))
                self.assertEqual(truth["hand"], battle.read_hand_count(task, task.frame))
                self.assertEqual(truth["hp"], battle.read_hp(task))
                self.assertEqual(truth["shield"], battle.read_shield(task))
                red_start, _ = battle.hp_bar_red(task.frame)
                self.assertEqual(truth["red"], red_start is not None)
                enemies = battle.read_enemies(task, task.frame)
                self.assertEqual(sorted(truth["enemies"], key=str),
                                 sorted([(e["hp"], e["countdown"]) for e in enemies], key=str))

                cards = battle.read_hand(task, truth["hand"])
                self.assertEqual(list(truth["cards"]), [c["key"] for c in cards])  # 按键按位置推出，不会错位
                for card in cards:
                    true_name, true_cost, true_type = truth["cards"][card["key"]]
                    if card["type"] is None and card["y"] is not None:
                        card["type"] = battle._card_type(task, card)
                    cost = battle._card_cost(task, task.frame, card)
                    if not card["name"].startswith("未识别"):
                        self.assertTrue(set(card["name"]) & set(true_name), f"{card['key']}: {card['name']} ≠ {true_name}")
                    if card["type"] is not None:
                        self.assertEqual(true_type, card["type"], f"按键 {card['key']} 的类型")
                    if cost is not None and true_cost is not None:  # 崩溃牌的费用位置是进度，出牌时按 0 费处理
                        costs_read += 1
                        self.assertEqual(true_cost, cost, f"按键 {card['key']}「{true_name}」的费用")
        self.assertGreaterEqual(costs_read, 8)  # 读不到的由「AP不足」兜底，但大部分应该能读到

    def test_bracket_suffix_does_not_replace_name(self):
        # 「破碎（屠戮）」的「（屠戮)」被单独读成一个框，以前按最长文字取牌名，破碎变成「（屠戮)」，出牌优先级匹配不上
        task = screenshot_task("card_suffix")
        cards = battle.read_hand(task, 7)
        self.assertEqual("破碎", cards[5]["name"])
        chosen, reason = battle.choose_play([dict(c, cost=None) for c in cards], 3, ["斗志", "破碎"], [], False, set())
        self.assertEqual("6", chosen["key"])
        self.assertIn("破碎", reason)
        self.assertEqual("定位雷射", cards[3]["name"])  # 整屏 OCR 漏读，手牌区放大补读出来

    def test_hand_lowered_when_ap_used_up(self):
        # 实跑 09:08:10：AP 用完，手牌沉下去变暗，「0」读不出来，整屏 OCR 只读到第一张牌名
        task = screenshot_task("hand_lowered")
        self.assertTrue(battle.hand_lowered(task))
        self.assertIsNone(battle.read_remaining_cost(task, task.frame))
        for name in list(TRUTH) + ["card_suffix"]:
            with self.subTest(name):
                self.assertFalse(battle.hand_lowered(screenshot_task(name)))

    def test_gray_zero_ap_by_color(self):
        # 实跑 12:14:50：刚打出 3 费的破碎，AP 已是灰色空心的「0」但手牌还没沉下去，读不出 AP 又去试了下一张牌
        for name in ("ap_zero_raised", "hand_lowered"):
            with self.subTest(name):
                frame = cv2.imdecode(np.fromfile(os.path.join(IMAGES, name + ".jpg"), dtype=np.uint8), cv2.IMREAD_COLOR)
                self.assertTrue(battle.ap_zero_outline(frame))
        for name in list(TRUTH) + ["card_suffix"]:  # AP 1~4：白色实心数字
            with self.subTest(name):
                frame = cv2.imdecode(np.fromfile(os.path.join(IMAGES, name + ".jpg"), dtype=np.uint8), cv2.IMREAD_COLOR)
                self.assertFalse(battle.ap_zero_outline(frame))

    def test_hp_larger_than_max_is_ignored(self):
        # 实跑中读出过 6177/1768、16657/1667（多读了一位）
        task = SimpleNamespace(width=2560, height=1440, all_texts=[Box(400, 30, 300, 40, name="6177/1768")])
        self.assertIsNone(battle.read_hp(task))
        task.all_texts = [Box(400, 30, 300, 40, name="617/1768")]
        self.assertEqual((617, 1768), battle.read_hp(task))

    def test_hand_slots_match_measured_layout(self):
        # 截图里量出的牌名左端间距：3/5/7/10 张
        for count, spacing in ((3, 0.1375), (5, 0.113), (7, 0.0808), (10, 0.0561)):
            slots = battle.hand_slots(count)
            self.assertAlmostEqual(spacing, slots[1] - slots[0], delta=0.002)
            self.assertAlmostEqual(0.471, (slots[0] + slots[-1]) / 2, delta=0.001)

    def test_intent_panel(self):
        task = screenshot_task("intent_panel")
        x1, y1, x2, y2 = battle._PANEL_REGION
        lines = sorted((utils._normalize_text(b.name), (b.y + b.height / 2) / task.height)
                       for b in task.ocr(x1, y1, x2, y2))
        lines.sort(key=lambda item: item[1])
        category, move, count, damage = battle.classify_intent_panel(lines)
        self.assertEqual((battle.INTENT_ATTACK, "划击", 8, 72), (category, move, count, damage))


def card(name, cost, card_type="技能", key="1"):
    return {"name": name, "cost": cost, "type": card_type, "key": key}


class TestChoosePlay(unittest.TestCase):

    def test_priority_first_among_affordable(self):
        cards = [card("破碎", 3, "攻击"), card("斗志", 1), card("水之根源", 1)]
        chosen, reason = battle.choose_play(cards, 2, ["破碎", "斗志"], [], False, set())
        self.assertEqual("斗志", chosen["name"])  # 破碎 3 费出不起
        self.assertIn("出牌优先级", reason)

    def test_order_zero_cost_enhance_attack_then_rest(self):
        cards = [card("刀背格挡", 1), card("斩击", 1, "攻击"), card("扭曲", 2, "强化"), card("孢子", 0)]
        order = []
        for _ in range(4):
            chosen, _ = battle.choose_play(cards, 9, [], ["刀背格挡"], False, set())
            order.append(chosen["name"])
            cards.remove(chosen)
        self.assertEqual(["孢子", "扭曲", "斩击", "刀背格挡"], order)

    def test_priority_cards_before_zero_cost_enhance_and_attack(self):
        cards = [card("饥饿的枷锁", 1, "攻击"), card("扭曲：光荣的抵抗", None, "强化"), card("孢子", 0),
                 card("斗志", 2), card("破碎", 3, "攻击")]
        chosen, reason = battle.choose_play(cards, 3, ["斗志", "破碎"], [], False, set())
        self.assertEqual("斗志", chosen["name"])
        self.assertIn("出牌优先级「斗志」", reason)
        cards.remove(chosen)
        chosen, _ = battle.choose_play(cards, 3, ["斗志", "破碎"], [], False, set())
        self.assertEqual("破碎", chosen["name"])
        chosen, _ = battle.choose_play(cards, 1, ["斗志", "破碎"], [], False, set())
        self.assertEqual("孢子", chosen["name"])  # 优先级里的牌出不起：按原顺序出其余的牌

    def test_logged_priority_card_with_unknown_type(self):
        # 实跑 09:08:55：「破碎」没读出类型，被排到其余牌里，先出了普通攻击「脉冲打击」
        cards = [card("脉冲打击", None, "攻击"), card("脉冲打击", None, "攻击", key="2"), card("磁场", None, "技能", key="3"),
                 card("破碎", 2, None, key="4")]
        chosen, _ = battle.choose_play(cards, 2, ["斗志", "破碎", "水之根源"], [], False, set())
        self.assertEqual("破碎", chosen["name"])

    def test_priority_tolerates_one_misread_char(self):
        # 实跑 19:14:59：「苍白流星」读成「奢白流星」，匹配不上优先级，先出了强化牌「冰霜残」
        cards = [card("寒霜盾牌", None, "技能"), card("冰霜残", None, "强化", key="2"), card("奢白流星", 2, "攻击", key="3")]
        chosen, reason = battle.choose_play(cards, 3, ["定位雷射", "钴蓝之光", "苍白流星"], [], False, set())
        self.assertEqual("奢白流星", chosen["name"])
        self.assertIn("出牌优先级「苍白流星」", reason)
        self.assertEqual(0, battle._priority_rank({"name": "定位雷身"}, ["定位雷射"])[0])
        self.assertEqual(0, battle._priority_rank({"name": "钴蓝光"}, ["钴蓝之光"])[0])
        # 两个字的读数、差两个字的牌不放宽
        self.assertIsNone(battle._priority_rank({"name": "苍日"}, ["苍白"])[1])
        self.assertIsNone(battle._priority_rank({"name": "冰霜残片"}, ["寒霜盾牌"])[1])

    def test_priority_orders_within_same_type_then_cost(self):
        cards = [card("斩击", 1, "攻击"), card("破碎", 2, "攻击"), card("秃鹰发射", 1, "攻击")]
        self.assertEqual("破碎", battle.choose_play(cards, 3, ["破碎"], [], False, set())[0]["name"])
        self.assertEqual("斩击", battle.choose_play(cards, 3, [], [], False, set())[0]["name"])

    def test_lethal_plays_defense_first(self):
        cards = [card("破碎", 2, "攻击"), card("孢子", 0), card("刀背格挡", 1)]
        chosen, reason = battle.choose_play(cards, 3, ["破碎"], ["刀背格挡"], True, set())
        self.assertEqual("刀背格挡", chosen["name"])
        self.assertIn("会被打死", reason)
        self.assertEqual("破碎", battle.choose_play(cards, 3, ["破碎"], ["刀背格挡"], False, set())[0]["name"])
        self.assertEqual("孢子", battle.choose_play(cards, 3, [], ["刀背格挡"], False, set())[0]["name"])

    def test_danger_line(self):
        # 实跑 11:42:14：505/1768 挨了 494，不算致命所以没防御，带着 11 血进了 Boss 战
        self.assertTrue(battle.in_danger((505, 1768), 0.02, 25))
        self.assertFalse(battle.in_danger((1500, 1768), 0.7, 25))   # 挨完还剩 1050，高于 25%
        self.assertFalse(battle.in_danger((505, 1768), 1.0, 25))    # 没看到预计扣血
        self.assertFalse(battle.in_danger((505, 1768), 0.02, 0))    # 0：只在会被打死时才防御
        self.assertFalse(battle.in_danger(None, 0.02, 25))

    def test_defense_recognized_by_name_without_list(self):
        # 护盾、回血牌在游戏里都标「基本技能」，按牌名里的字自动认，不用把每张都填进防御卡牌列表
        for name in ("刀背格挡", "冰壁", "寒霜盾牌", "紧急治疗"):
            self.assertTrue(battle.is_defense(card(name, 1), []), name)
        self.assertFalse(battle.is_defense(card("盾击", 1, "攻击"), []))  # 攻击牌带「盾」字不算
        self.assertFalse(battle.is_defense(card("斗志", 1), []))
        self.assertTrue(battle.is_defense(card("斗志", 1), ["斗志"]))  # 列表用来补充认不出的
        cards = [card("破碎", 2, "攻击"), card("冰壁", 2, "技能", key="2")]
        self.assertEqual("冰壁", battle.choose_play(cards, 2, ["破碎"], [], True, set())[0]["name"])

    def test_unknown_cost_is_tried_but_not_when_no_cost_left(self):
        cards = [card("未识别1", None)]
        self.assertIsNotNone(battle.choose_play(cards, 1, [], [], False, set())[0])
        self.assertIsNone(battle.choose_play(cards, 0, [], [], False, set())[0])
        cards[0]["slot"] = "1/1"
        self.assertIsNone(battle.choose_play(cards, 1, [], [], False, {"1/1"})[0])  # 这个位置提示过 AP不足
        self.assertIsNotNone(battle.choose_play([card("孢子", 0)], 0, [], [], False, set())[0])  # 0 费照出

    def test_zero_hp_skips_shield_and_collapse(self):
        # 血量为 0：护盾无效、崩溃牌打不出，再挨一次打就输
        cards = [card("冲动", 0, "崩溃", key="1"), card("寒霜盾牌", 1, key="2"), card("斩击", 1, "攻击", key="3")]
        chosen, reason = battle.choose_play(cards, 3, ["寒霜盾牌"], [], True, set(), zero_hp=True)
        self.assertEqual("斩击", chosen["name"])
        self.assertIn("血量为 0", reason)
        chosen, reason = battle.choose_play(cards[:2], 3, [], [], True, set(), zero_hp=True)
        self.assertIsNone(chosen)

    def test_zero_hp_still_plays_heal(self):
        cards = [card("治疗", 1, key="1"), card("冰壁", 1, key="2")]
        chosen, _ = battle.choose_play(cards, 3, [], [], True, set(), zero_hp=True)
        self.assertEqual("治疗", chosen["name"])

    def test_collapse_card_first_even_without_ap(self):
        cards = [card("破碎", 3, "攻击"), card("冲动", 0, "崩溃", key="2")]
        cards[1]["progress"] = (1, 5)
        chosen, reason = battle.choose_play(cards, 0, ["破碎"], [], True, set())
        self.assertEqual("冲动", chosen["name"])
        self.assertIn("1/5", reason)

    def test_remaining_unknown_plays_anything(self):
        self.assertEqual("破碎", battle.choose_play([card("破碎", 3, "攻击")], None, [], [], False, set())[0]["name"])


def enemy(hp, countdown, intent=None, x=0.5, y=0.3, shield=0, infinite=False):
    return {"hp": hp, "shield": shield, "countdown": countdown, "intent": intent, "x": x, "y": y, "infinite": infinite}


class TestChooseTarget(unittest.TestCase):

    def test_boss_battle_hits_boss(self):
        enemies = [enemy(1254, 1, "攻击", x=0.43), enemy(6078, None, x=0.70)]
        self.assertEqual(6078, battle.choose_target(enemies, True, None)[0]["hp"])

    def test_boss_keeps_hitting_head_after_it_is_hurt(self):
        # Boss 会不断召唤小怪：Boss 打残后血比新小怪少，仍然打 Boss
        state = {}
        head, minion = enemy(1551, 6, "防御", x=0.66), enemy(728, 2, "攻击", x=0.49)
        battle.update_head(state, [head, minion])
        hurt = enemy(300, 6, "防御", x=0.66)
        battle.update_head(state, [hurt, minion])
        self.assertIs(hurt, battle.choose_target([hurt, minion], True, None, state["head"])[0])
        # 开场先只认到了小怪，头目出现后改认血更多的那个
        state = {}
        battle.update_head(state, [minion])
        battle.update_head(state, [minion, head])
        self.assertIs(head, battle.choose_target([minion, head], True, None, state["head"])[0])

    def test_infinity_decides_boss_over_hp(self):
        # 实跑 17:00~17:02：几个敌人血量相近，按「见过的最多血量」认头目，在三个敌人之间来回换
        state = {}
        boss = enemy(1847, None, "攻击", x=0.64, infinite=True)
        battle.update_head(state, [enemy(1984, 1, "攻击", x=0.46), boss])
        battle.update_head(state, [enemy(2100, 1, "攻击", x=0.82), dict(boss, hp=1600, infinite=False)])
        self.assertEqual((0.64, 0.3), state["head"])

    def test_focus_attackers_with_least_hp_and_shield(self):
        enemies = [enemy(300, 2, "增益", x=0.3), enemy(900, 1, "攻击", x=0.5), enemy(500, 5, "攻击", x=0.7)]
        self.assertEqual(500, battle.choose_target(enemies, False, None)[0]["hp"])  # 倒计时不再优先，先打容易死的
        enemies.append(enemy(400, 6, None, x=0.9, shield=200))  # 意图认不出按攻击算；护盾也要打穿
        self.assertEqual(500, battle.choose_target(enemies, False, None)[0]["hp"])
        enemies.append(enemy(450, 6, None, x=0.2))
        self.assertEqual(450, battle.choose_target(enemies, False, None)[0]["hp"])
        # 都没有攻击意图：打最容易死的
        calm = [enemy(700, 1, "防御", x=0.3), enemy(600, 3, "增益", x=0.6)]
        self.assertEqual(600, battle.choose_target(calm, False, None)[0]["hp"])

    def test_urgent_hits_next_attacker(self):
        enemies = [enemy(300, 2, "增益", x=0.3), enemy(900, 1, "攻击", x=0.5), enemy(500, 5, "攻击", x=0.7)]
        self.assertEqual(900, battle.choose_target(enemies, True, None, (0.3, 0.3), urgent=True)[0]["hp"])

    def test_sticky_target_until_gone(self):
        enemies = [enemy(300, 1, "攻击", x=0.3), enemy(900, 5, "攻击", x=0.7)]
        self.assertEqual(900, battle.choose_target(enemies, False, (0.71, 0.3))[0]["hp"])
        self.assertEqual(300, battle.choose_target(enemies[:1], False, (0.71, 0.3))[0]["hp"])


class TestKillPreview(unittest.TestCase):
    """精细化战斗：拖到敌人身上悬停时，血条上方的数字变成打完后剩下的血量，打得死时是 0。"""

    def test_reads_hp_after_hit_on_real_screens(self):
        # 拖牌时镜头推近，敌人比拖动前的位置往右下偏了约 0.03；位置是拖动前那一帧读到的
        for name, before, after in (("preview_hit", enemy(886, 2, x=0.507, y=0.361), 164),
                                    ("preview_lethal", enemy(492, 2, x=0.643, y=0.279), 0)):
            with self.subTest(name):
                task = screenshot_task(name)
                value = battle.read_preview_hp(task, task.frame, before)
                self.assertEqual(after, value)
                self.assertEqual(after == 0, battle.is_lethal(before, value))

    def test_boss_bar_split_counts_once(self):
        # 实跑 21:41：Boss 的长血条被切成两段，都挨着同一个数字，被当成两个 8644 血的敌人，击杀预览白白多停一次
        task = SimpleNamespace(width=2560, height=1440, all_texts=[Box(int(0.70 * 2560), int(0.08 * 1440), 150, 40, name="8644")])
        with mock.patch.object(battle, "enemy_bars", lambda frame: [(0.654, 0.102, 0.08), (0.733, 0.102, 0.05)]), \
                mock.patch.object(battle, "_read_digit", lambda *a, **k: 3), \
                mock.patch.object(battle, "match_intent", lambda crop: None):
            enemies = battle.read_enemies(task, np.zeros((1440, 2560, 3), np.uint8))
        self.assertEqual([(0.654, 8644)], [(e["x"], e["hp"]) for e in enemies])

    def test_lethal_needs_zero(self):
        self.assertTrue(battle.is_lethal(enemy(492, 2), 0))
        self.assertFalse(battle.is_lethal(enemy(492, 2), None))  # 读不到（出牌动画挡着）
        self.assertFalse(battle.is_lethal(enemy(492, 2), 164))

    def test_candidates_only_when_damage_may_kill(self):
        boss = enemy(8000, None, "增益", x=0.6)
        minions = [enemy(300, 3, "攻击", x=0.3), enemy(200, 1, "防御", x=0.4), enemy(400, 1, None, x=0.8, shield=100)]
        self.assertEqual([], battle.preview_candidates([boss] + minions, boss, None))  # 本场还没读到这张牌的伤害：不看
        # 伤害 300：打得死 200、300 的都看（有攻击意图的先看），400+100 护盾的不看
        self.assertEqual([300, 200], [e["hp"] for e in battle.preview_candidates([boss] + minions, boss, 300)])
        self.assertEqual([200], [e["hp"] for e in battle.preview_candidates([boss] + minions, boss, 150)])
        target = minions[0]
        self.assertEqual([], battle.preview_candidates([target], target, 9999))

    def test_hit_damage_counts_shield(self):
        self.assertEqual(722, battle.hit_damage(enemy(886, 2), 164))
        self.assertEqual(822, battle.hit_damage(enemy(886, 2, shield=100), 164))
        self.assertIsNone(battle.hit_damage(enemy(886, 2, shield=500), 886))  # 护盾没打穿，算不出
        self.assertIsNone(battle.hit_damage(enemy(886, 2), None))

    def _hover(self, readings, task=None, others=None):
        task = task or SimpleNamespace(log_info=lambda message: None)
        target = dict(enemy(900, 3, "攻击", x=0.5), drop=(0.5, 0.5))
        if others is None:
            others = [dict(enemy(300, 1, "攻击", x=0.3), drop=(0.3, 0.5)), dict(enemy(200, 2, None, x=0.7), drop=(0.7, 0.5))]
        values = dict(readings)
        events = []
        with mock.patch.object(battle, "_hover_reading", lambda task, e: (None, values.get(e["hp"]))), \
                mock.patch.object(battle_log, "record", lambda task, event, **f: events.append(f)):
            hover = battle._preview_hover(task, card("斩击", 1, "攻击"), target, others)
            moves = []
            while True:
                nxt = hover()
                if nxt is None:
                    break
                moves.append(nxt)
        return moves, events[-1]

    def test_releases_on_first_enemy_it_kills(self):
        moves, event = self._hover({300: 120, 200: 0, 900: 700})
        self.assertEqual([(0.7, 0.5)], moves)  # 第一个打不死，移到第二个：打得死，就在这里松手
        self.assertEqual((200, "打得死"), (event["chosen"]["hp"], event["reason"]))

    def test_back_to_default_target_when_nothing_dies(self):
        moves, event = self._hover({300: 120, 200: None, 900: 0})
        self.assertEqual([(0.7, 0.5), (0.5, 0.5)], moves)  # 最后回到默认目标上松手
        self.assertEqual(900, event["chosen"]["hp"])
        self.assertEqual([120, None, 0], [s["after"] for s in event["seen"]])

    def test_default_target_reading_remembers_damage(self):
        task = SimpleNamespace(log_info=lambda message: None)
        moves, event = self._hover({900: 700}, task, others=[])
        self.assertEqual([], moves)  # 只停在默认目标上
        self.assertEqual("打默认目标", event["reason"])
        self.assertEqual({"斩击": 200}, task._battle["damage"])

    def test_drag_moves_between_hovers_and_releases_at_last_point(self):
        class PostMessageInteraction:
            def __init__(self):
                self.messages = []

            def update_mouse_pos(self, x, y):
                return (x, y)

            def post(self, message, wparam, lparam):
                self.messages.append((message, wparam, lparam))

        interaction = PostMessageInteraction()
        task = SimpleNamespace(executor=SimpleNamespace(interaction=interaction), width=2560, height=1440,
                               log_info=lambda message: None)
        points = iter([(0.3, 0.4), None])
        with mock.patch.object(battle.time, "sleep", lambda seconds: None):
            battle._post_drag(task, (0.5, 0.86), (0.7, 0.4), on_hover=lambda: next(points))
        ups = [m for m in interaction.messages if m[0] == battle._WM_LBUTTONUP]
        self.assertEqual([(battle._WM_LBUTTONUP, 0, (768, 576))], ups)  # 只松手一次，在最后停的位置
        self.assertIn((battle._WM_MOUSEMOVE, battle._MK_LBUTTON, (1792, 576)), interaction.messages)


class TestPlayTurn(unittest.TestCase):
    """出牌流程：AP不足记为出不起、出不掉的牌本回合不再出、没牌可出按 E。"""

    def setUp(self):
        self.keys = []
        self.task = SimpleNamespace(
            name="自动出击模式", width=2560, height=1440, frame=np.zeros((1440, 2560, 3), np.uint8), all_texts=[],
            config={}, default_config={battle_log.LOG_KEY: False, "出牌优先级": [], battle.DEFENSE_KEY: []},
            node_status={}, log_info=lambda message: None, sleep=lambda seconds: None,
            send_key=lambda key: self.keys.append(key), swipe_relative=lambda *a, **k: self.keys.append("drag"))
        patches = {
            "read_hand": lambda task, count: [dict(card("斗志", None, "技能", key="1"), x=0.4, y=None),
                                              dict(card("破碎", 3, "攻击", key="2"), x=0.5, y=None)][:count],
            "read_remaining_cost": lambda task, frame: 3,
            "read_enemies": lambda task, frame: [dict(enemy(500, 3, "攻击"), drop=(0.6, 0.5))],
            "read_hp": lambda task: (1000, 1000), "read_shield": lambda task: 0,
            "_card_cost": lambda task, frame, c: c.get("cost"),
        }
        for name, fn in patches.items():
            patcher = mock.patch.object(battle, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_button_gone_too_long_hands_frame_to_other_handlers(self):
        # 实跑 10:36:45：结束回合按钮一直不出现，每帧都当成敌人行动中干等了 13 分钟
        now = [1000.0]
        with mock.patch.object(battle.time, "time", lambda: now[0]):
            for _ in range(battle._BUTTON_GONE_LIMIT - 1):  # 实际每秒左右调用一次
                self.assertTrue(battle.play_turn(self.task, 2, False))  # 敌人行动中：这一帧不做事
                now[0] += 1
            now[0] += 1
            self.assertFalse(battle.play_turn(self.task, 2, False))  # 太久了：交给后面的页面处理函数
            self.assertTrue(self.task._battle["button_reported"])
            now[0] += 1
            battle.play_turn(self.task, 2, True)  # 按钮回来了：重新计时
            self.assertIsNone(self.task._battle["button_gone"])
        self.assertEqual([], [k for k in self.keys if k == "e"])

    def test_deferred_end_turn_not_recorded(self):
        # 加速模式这一轮先不按 E 时，战斗记录里曾每回合出现两条「结束回合」
        events = []
        self.task._speedup = {}
        self.task.send_key = lambda key: self.task._speedup.update(end_turn_deferred=key == "e")
        with mock.patch.object(battle.battle_log, "record", lambda task, event, **f: event == "结束回合" and events.append(event)), \
                mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 0):
            battle.play_turn(self.task, 1, True)
            self.assertEqual([], events)                   # E 没真正发出去：不记
            self.task.send_key = lambda key: None
            battle.play_turn(self.task, 1, True)
        self.assertEqual(["结束回合"], events)

    def test_low_hp_after_hit_plays_defense_first(self):
        hand = [dict(card("破碎", 1, "攻击", key="1"), x=0.4, y=None), dict(card("刀背格挡", 1, key="2"), x=0.5, y=None)]
        self.task.default_config["出牌优先级"] = ["破碎"]
        with mock.patch.object(battle, "read_hand", lambda task, count: hand), \
                mock.patch.object(battle, "read_hp", lambda task: (505, 1768)), \
                mock.patch.object(battle, "incoming_lethal", lambda task: (False, 0.02)):
            battle.play_turn(self.task, 2, True)
        self.assertEqual(["2", "enter"], self.keys)

    def test_hand_changed_after_reading_waits_for_next_frame(self):
        # 实跑 15:40:27：出牌把牌移回手牌/抽牌，新牌还没到手就读了手牌，按旧排位按到了空位
        self.task.next_frame = lambda: self.task.frame
        with mock.patch.object(battle, "read_hand_count", lambda task, frame: 3):
            for _ in range(battle._STALE_LIMIT):
                self.assertTrue(battle.play_turn(self.task, 1, True))
                self.assertEqual([], self.keys)  # 手牌数变了：这一帧不出牌
            battle.play_turn(self.task, 1, True)  # 一直在变也不卡住：照常出牌
        self.assertEqual(["1", "enter"], self.keys)

    def test_hand_count_unchanged_plays_at_once(self):
        self.task.next_frame = lambda: self.task.frame
        with mock.patch.object(battle, "read_hand_count", lambda task, frame: 1):
            battle.play_turn(self.task, 1, True)
        self.assertEqual(["1", "enter"], self.keys)

    def test_wait_hand_settled_waits_for_new_cards(self):
        # 出牌后手牌数先少一张，过一会儿新牌到手又变多：要稳够 _SETTLE_STABLE 秒才继续
        clock = [0.0]
        self.task.next_frame = lambda: self.task.frame
        counts = lambda task, frame: 5 if clock[0] < 0.3 else 4 if clock[0] < 0.9 else 7
        self.task._speedup = {"owed_until": 5.0, "pay_hook": object()}
        fake_time = SimpleNamespace(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        with mock.patch.object(battle, "time", fake_time), mock.patch.object(battle, "read_hand_count", counts):
            battle._wait_hand_settled(self.task)
        self.assertGreaterEqual(clock[0], 0.9 + battle._SETTLE_STABLE)
        self.assertLess(clock[0], battle._SETTLE_MAX)
        self.assertEqual({"owed_until": 0.0, "pay_hook": None}, self.task._speedup)  # 已等过，加速模式不用再补

    def test_lowered_hand_ends_turn_without_trying_cards(self):
        labels = [Box(700, 1260, 150, 40, name="基本攻击"), Box(1200, 1270, 100, 40, name="攻击")]
        self.task.all_texts = labels
        with mock.patch.object(battle, "read_remaining_cost", lambda task, frame: None):
            battle.play_turn(self.task, 2, True)
        self.assertEqual(["e"], self.keys)  # 读不到费用的斗志、3 费的破碎都不试，直接结束回合

    def test_ap_insufficient_marks_card(self):
        battle.play_turn(self.task, 1, True)
        self.assertEqual(["1", "enter"], self.keys)
        self.task.all_texts = [Box(1200, 700, 200, 50, name="AP不足")]
        battle.play_turn(self.task, 1, True)
        self.assertIn("斗志", self.task._battle["unplayable"])
        self.task.all_texts = []
        self.keys.clear()
        battle.play_turn(self.task, 1, True)
        self.assertEqual(["e"], self.keys)  # 唯一的牌出不起：结束回合

    def _laser_hand(self, count):
        # 定位雷射（0 费）击破后从墓地召回到手牌；脉冲打击 3 费、AP 只剩 2
        hand = [dict(card("脉冲打击", 3, "攻击", key="1"), x=0.3, y=None)]
        hand += [dict(card("定位雷射", 0, "攻击", key=str(i)), x=0.3 + 0.1 * i, y=None) for i in range(2, count + 1)]
        return hand

    def test_zero_cost_card_not_blocked_by_name_after_missed_detection(self):
        # 实跑 16:58~20:05：定位雷射击破后回到手牌，手牌数、AP 都没变，被当成没打出去按牌名封掉，
        # 召回的定位雷射也跟着出不了，直接结束了回合
        hands = {2: self._laser_hand(2), 4: self._laser_hand(4)}
        with mock.patch.object(battle, "read_hand", lambda task, count: hands[count]), \
                mock.patch.object(battle, "_use_drag", lambda task, state, c: False), \
                mock.patch.object(battle, "_session", lambda task: dict(drag_ok=0, drag_fail=0, drag_disabled=True, preview_shots=0, zero_cost=set())), \
                mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 2):
            battle.play_turn(self.task, 2, True)
            self.assertEqual(["2", "enter"], self.keys)
            battle.play_turn(self.task, 2, True)  # 手牌数、AP 都没变：只跳过这个位置
            self.assertNotIn("定位雷射", self.task._battle["unplayable"])
            self.keys.clear()
            battle.play_turn(self.task, 4, True)  # 召回了两张：照样出
        self.assertEqual(["2", "enter"], self.keys)

    def test_hand_growing_after_play_counts_as_played(self):
        hands = {2: self._laser_hand(2), 4: self._laser_hand(4)}
        with mock.patch.object(battle, "read_hand", lambda task, count: hands[count]), \
                mock.patch.object(battle, "_use_drag", lambda task, state, c: False), \
                mock.patch.object(battle, "_session", lambda task: dict(drag_ok=0, drag_fail=0, drag_disabled=True, preview_shots=0, zero_cost=set())), \
                mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 3):
            battle.play_turn(self.task, 2, True)
            battle.play_turn(self.task, 4, True)
        self.assertEqual(set(), self.task._battle["unplayable"])

    def test_zero_hp_targets_next_attacker_not_boss(self):
        targets = []
        enemies = [dict(enemy(8000, 5, "增益", x=0.7), drop=(0.7, 0.5)), dict(enemy(900, 1, "攻击", x=0.5), drop=(0.5, 0.5))]
        self.task.default_config["出牌优先级"] = ["破碎"]
        self.task.node_status = {"node_type": "boss"}
        with mock.patch.object(battle, "read_hp", lambda task: (0, 1700)), \
                mock.patch.object(battle, "read_enemies", lambda task, frame: enemies), \
                mock.patch.object(battle, "_drag_card", lambda task, c, t, *rest, **kw: targets.append(t["hp"])):
            battle.play_turn(self.task, 2, True)
        self.assertEqual([900], targets)

    def test_attack_card_is_dragged(self):
        self.task.default_config["出牌优先级"] = ["破碎"]
        battle.play_turn(self.task, 2, True)
        self.assertEqual(["drag"], self.keys)

    def test_card_that_never_leaves_hand_is_skipped(self):
        battle.play_turn(self.task, 1, True)
        battle.play_turn(self.task, 1, True)  # 手牌数和 AP 都没减少：先拖动再试一次（键盘可能失效）
        self.assertNotIn("斗志", self.task._battle["unplayable"])
        battle.play_turn(self.task, 1, True)  # 拖动也没打出去：本回合不再出，没牌可出就结束回合
        self.assertIn("斗志", self.task._battle["unplayable"])
        self.assertEqual(["1", "enter", "drag", "e"], self.keys)
        self.assertFalse(self.task._battle.get("keys_dead"))

    def test_keys_dead_switches_to_mouse(self):
        # 实跑 17:59~18:45：游戏不理后台按键（数字键、E 都没反应），鼠标照常有效，卡了 45 分钟
        clicks = []
        counts = iter([2, 2, 1, 1])
        skill = lambda task, count: [dict(card("斗志", None, "技能", key="1"), x=0.4, y=None)]
        with mock.patch.object(battle, "_move_and_click", lambda task, x, y: clicks.append((x, y))), \
                mock.patch.object(battle, "read_hand", skill), \
                mock.patch.object(battle, "read_remaining_cost", lambda task, frame: None):
            battle.play_turn(self.task, next(counts), True)   # 斗志按键
            battle.play_turn(self.task, next(counts), True)   # 没打出去：拖动再试
            self.assertEqual(["1", "enter", "drag"], self.keys)
            battle.play_turn(self.task, next(counts), True)   # 拖动打出去了：本场改用鼠标
            self.assertTrue(self.task._battle["keys_dead"])
            self.assertEqual(["1", "enter", "drag", "drag"], self.keys)  # 不用选目标的牌也拖到场地中间
            battle.play_turn(self.task, next(counts), True)   # 没打出去（AP 不够）：没牌可出，按 E 同时点按钮
        self.assertEqual("e", self.keys[-1])
        self.assertEqual([battle._END_TURN_POINT], clicks)

    def test_end_turn_clicks_button_when_e_ignored(self):
        clicks = []
        with mock.patch.object(battle, "_move_and_click", lambda task, x, y: clicks.append((x, y))), \
                mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 0):
            for _ in range(battle._E_KEY_LIMIT):
                battle.play_turn(self.task, 1, True)
            self.assertEqual([], clicks)
            battle.play_turn(self.task, 1, True)  # 按了 _E_KEY_LIMIT 次按钮还在：再用鼠标点
        self.assertEqual([battle._END_TURN_POINT], clicks)

    def test_ap_drop_counts_as_played(self):
        # 抽牌的牌打出后手牌数不变，但 AP 减少了：算打出去了，不能记成出不起
        battle.play_turn(self.task, 1, True)
        with mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 2):
            battle.play_turn(self.task, 1, True)
        self.assertNotIn("斗志", self.task._battle["unplayable"])

    def test_new_turn_after_enemy_phase_even_without_ap(self):
        with mock.patch.object(battle, "read_remaining_cost", lambda task, frame: None):
            battle.play_turn(self.task, 1, True)
            self.task._battle["unplayable"].add("斗志")
            battle.play_turn(self.task, 1, True)      # 没有出得起的牌：结束回合
            battle.play_turn(self.task, 1, False)     # 敌人行动中，「结束回合」按钮不在
            self.keys.clear()
            battle.play_turn(self.task, 1, True)      # 按钮重新出现：新回合，斗志又能出了
        self.assertNotIn("斗志", self.task._battle["unplayable"])
        self.assertEqual(["1", "enter"], self.keys)

    def test_new_turn_when_cost_refills(self):
        battle.play_turn(self.task, 1, True)
        self.task._battle["unplayable"].add("斗志")
        with mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 1):
            battle.play_turn(self.task, 1, True)
        with mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 4):
            battle.play_turn(self.task, 1, True)
        self.assertNotIn("斗志", self.task._battle["unplayable"])


class TestPlayTurnRecovery(TestPlayTurn):
    """实跑中发现的问题：后台拖动没被游戏当成出牌、牌名前后多读出杂字、意图采集读不出时反复点开怪物。"""

    def test_drag_disabled_after_repeated_failures(self):
        self.task.default_config["出牌优先级"] = ["破碎"]
        # 手牌数一直是 2：拖动没打出去。上一张牌打没打出去要到下一次调用才检查，所以第 3 次才发现拖了两次都失败
        for _ in range(battle._DRAG_FAIL_LIMIT + 1):
            battle.play_turn(self.task, 2, True)
        self.assertTrue(self.task._battle_session["drag_disabled"])
        self.assertEqual(["drag", "drag", "2", "enter"], self.keys)  # 改用按键打默认目标
        # 只禁用这一场：实跑中窗口在前台时开头两次拖动失败，之后整次运行都没法选目标
        battle.start_battle(self.task)
        self.assertFalse(self.task._battle_session["drag_disabled"])
        self.keys.clear()
        battle.play_turn(self.task, 2, True)
        self.assertEqual(["drag"], self.keys)

    def test_background_drag_releases_on_target(self):
        # 框架的 PostMessage swipe 在窗口左上角 (0, 0) 松手，牌被放回手里；自己发的拖动要在落点松手
        class PostMessageInteraction:
            def __init__(self):
                self.messages = []

            def update_mouse_pos(self, x, y):
                return (x, y)

            def post(self, message, wparam, lparam):
                self.messages.append((message, wparam, lparam))

        interaction = PostMessageInteraction()
        self.task.executor = SimpleNamespace(interaction=interaction)
        hovered = []
        with mock.patch.object(battle.time, "sleep", lambda seconds: None):
            self.assertTrue(battle._post_drag(self.task, (0.5, 0.86), (0.7, 0.4),
                                              on_hover=lambda: hovered.append(len(interaction.messages))))
        messages = interaction.messages
        self.assertEqual((battle._WM_LBUTTONDOWN, battle._MK_LBUTTON, (1280, 1238)), messages[1])
        self.assertEqual((battle._WM_LBUTTONUP, 0, (1792, 576)), messages[-1])
        self.assertEqual((battle._WM_MOUSEMOVE, battle._MK_LBUTTON, (1792, 576)), messages[-2])
        self.assertEqual([len(messages) - 2], hovered)  # 悬停截图在松手之前

    def test_drag_falls_back_to_swipe_without_post_message(self):
        self.task.executor = SimpleNamespace(interaction=object())
        self.assertFalse(battle._post_drag(self.task, (0.5, 0.86), (0.7, 0.4)))

    def test_blocked_name_ignores_ocr_noise(self):
        self.assertTrue(battle._blocked({"name": "日黑暗斩击", "slot": "1/5"}, {"黑暗斩击"}))
        self.assertTrue(battle._blocked({"name": "未识别3", "slot": "3/5"}, {"3/5"}))
        self.assertFalse(battle._blocked({"name": "未识别3", "slot": "3/4"}, {"3/5", "未识别3"}))

    def test_intent_collected_once_per_enemy_per_turn(self):
        self.task.default_config[battle.COLLECT_KEY] = True
        calls = []
        unknown = [dict(enemy(500, 3, None), drop=(0.6, 0.5))]
        with mock.patch.object(battle, "collect_intent", lambda task, e: calls.append(e["hp"])),                 mock.patch.object(battle, "read_enemies", lambda task, frame: unknown):
            for _ in range(3):
                battle.play_turn(self.task, 1, True)
        self.assertEqual([500], calls)  # 读不出意图也只点开一次，之后照常出牌
        self.assertIn("1", self.keys)

    def test_intent_collection_stops_after_enemy_already_acted(self):
        # 实跑 16:38:14：连点 4 个敌人都是「本回合已行动」，白花 10 秒
        self.task.default_config[battle.COLLECT_KEY] = True
        calls = []
        unknown = [dict(enemy(500 + i, 3, None), x=0.3 + i * 0.1, drop=(0.3 + i * 0.1, 0.5)) for i in range(4)]
        with mock.patch.object(battle, "collect_intent", lambda task, e: calls.append(e["hp"]) or True), \
                mock.patch.object(battle, "read_enemies", lambda task, frame: unknown):
            for _ in range(3):
                battle.play_turn(self.task, 1, True)
            self.assertEqual([500], calls)
            self.task._battle["enemy_phase"] = True  # 下一回合重新采集
            battle.play_turn(self.task, 1, True)
        self.assertEqual([500, 500], calls)

    def test_played_banner_counts_as_played(self):
        # 实跑 21:41:41：AP 为 0 时按出 0 费的「逆转之刃」，它又抽了一张牌，手牌数、AP 都没变，被误判成出不掉
        hand = [dict(card("逆转之刃", 0, "强化", key="1"), x=0.4, y=None)]
        with mock.patch.object(battle, "read_hand", lambda task, count: [dict(c) for c in hand]), \
                mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 0):
            battle.play_turn(self.task, 1, True)
            self.task.all_texts = [Box(int(0.05 * 2560), int(0.46 * 1440), 300, 50, name="逆转之刃")]
            battle.play_turn(self.task, 1, True)
        self.assertNotIn("逆转之刃", self.task._battle["unplayable"])
        self.assertEqual(["1", "enter", "1", "enter"], self.keys)

    def test_zero_cost_remembered_when_ap_used_up(self):
        # 实跑 21:13:36：AP 用完，手里只剩 0 费的「逆转之刃」，费用读不到就结束了回合
        costs = {"逆转之刃": 0}
        hand = [dict(card("逆转之刃", None, None, key="1"), x=0.4, y=None)]
        with mock.patch.object(battle, "read_hand", lambda task, count: [dict(c) for c in hand]), \
                mock.patch.object(battle, "_card_cost", lambda task, frame, c: costs.get(c["name"])):
            battle.play_turn(self.task, 1, True)  # 有 AP 时读到过一次 0 费
            self.keys.clear()
            costs.clear()
            battle.start_battle(self.task)  # 下一场战斗：本场记下的费用清空，本次运行记下的 0 费牌名还在
            with mock.patch.object(battle, "read_remaining_cost", lambda task, frame: 0):
                battle.play_turn(self.task, 1, True)
        self.assertEqual(["1", "enter"], self.keys)  # AP 为 0、费用读不到，仍按 0 费出

    def test_turn_end_trigger_is_an_intent(self):
        lines = [("光秃铁壳虫1", 0.06), ("弱点", 0.1), ("翻滚", 0.17), ("回合结束时触发", 0.21),
                 ("获得50%(35)护盾、士气2", 0.25)]
        self.assertEqual((battle.INTENT_DEFENSE, "翻滚", None, None), battle.classify_intent_panel(lines))


class TestBattleLogCleanup(unittest.TestCase):

    def test_old_files_and_size_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            now = time.time()
            paths = []
            for i, age_days in enumerate((10, 3, 2, 1)):
                path = os.path.join(folder, f"{i}.jsonl")
                with open(path, "wb") as f:
                    f.write(b"x" * 1024 * 1024)
                os.utime(path, (now - age_days * 86400, now - age_days * 86400))
                paths.append(path)
            self.assertEqual(1, battle_log.cleanup(folder, keep_days=7, max_mb=10, now=now))  # 10 天前的
            self.assertEqual(1, battle_log.cleanup(folder, keep_days=7, max_mb=2, now=now))   # 超过 2MB 删最旧的
            self.assertEqual([False, False, True, True], [os.path.exists(p) for p in paths])


class TestBattleLogEvents(unittest.TestCase):
    """两个模式共用的轮次、节点、战斗起止、未识别页面记录。"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, value in (("LOG_DIR", folder.name), ("SHOT_DIR", os.path.join(folder.name, "截图"))):
            patcher = mock.patch.object(battle_log, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = folder.name
        self.now = [1000.0]
        patcher = mock.patch.object(battle_log.time, "time", lambda: self.now[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = SimpleNamespace(name="自动卡厄思模式", config={}, default_config={battle_log.LOG_KEY: True},
                                    node_status={"node_count": 3, "node_type": "小怪", "pass_final_boss_count": 0},
                                    all_texts=[Box(0, 0, 10, 10, name="奇怪的页面")], frame=None,
                                    log_info=lambda message: None)

    def rows(self):
        import json
        rows = []
        for name in os.listdir(self.folder):
            if name.endswith(".jsonl"):
                with open(os.path.join(self.folder, name), encoding="utf-8") as f:
                    rows += [json.loads(line) for line in f]
        return rows

    def test_round_node_battle_and_reroll(self):
        battle_log.reroll(self.task, "零式系统", value=120, unit="pt", required=140)
        battle_log.node_entered(self.task, hp=(1000, 1200))
        battle_log.battle_frame(self.task, True)
        self.now[0] += 30
        battle_log.battle_frame(self.task, True)
        self.now[0] += 3
        battle_log.battle_frame(self.task, False)  # 弹出选择页面：还不算结束
        self.now[0] += battle_log._BATTLE_GONE
        battle_log.battle_frame(self.task, False)
        battle_log.node_entered(self.task, hp=(700, 1200))
        self.now[0] += 60
        battle_log.end_round(self.task, success=False)
        battle_log.node_entered(self.task, hp=(1200, 1200))
        rows = self.rows()
        self.assertEqual(["重开", "进入节点", "战斗开始", "战斗结束", "进入节点", "一轮结束", "进入节点"],
                         [r["event"] for r in rows])
        self.assertEqual(30, rows[3]["seconds"])            # 战斗时长按最后一帧战斗画面算
        self.assertEqual(-300, rows[4]["hp_change"])
        self.assertEqual((1, 1), (rows[5]["round"], rows[5]["rerolls"]))
        self.assertEqual((2, None), (rows[6]["round"], rows[6]["hp_change"]))  # 新一轮不跟上一轮比
        self.assertEqual(1, rows[2]["battle"])

    def test_lost_battle_ends_with_the_round(self):
        # 实跑 10/01 09:15：最终 boss 战输了直接进结算页，「战斗结束」被记到了下一轮
        battle_log.battle_frame(self.task, True)
        self.now[0] += 100
        battle_log.battle_frame(self.task, True)
        self.now[0] += 2
        battle_log.end_round(self.task, success=False, result="失败")
        self.now[0] += battle_log._BATTLE_GONE
        battle_log.battle_frame(self.task, False)
        rows = self.rows()
        self.assertEqual(["战斗开始", "战斗结束", "一轮结束"], [r["event"] for r in rows])
        self.assertEqual((1, 100), (rows[1]["round"], rows[1]["seconds"]))
        self.assertEqual("失败", rows[2]["result"])

    def test_unhandled_page_reported_once_and_not_during_battle(self):
        for _ in range(15):
            battle_log.unhandled_frame(self.task)
            self.now[0] += 1
        rows = self.rows()
        self.assertEqual(["未识别页面"], [r["kind"] for r in rows])
        self.assertIn("奇怪的页面", rows[0]["texts"])
        battle_log.battle_frame(self.task, True)
        for _ in range(15):
            battle_log.unhandled_frame(self.task)
            self.now[0] += 1
        self.assertEqual(1, sum(r["event"] == "异常" for r in self.rows()))

    def test_handled_frames_in_between_restart_the_clock(self):
        for _ in range(8):
            battle_log.unhandled_frame(self.task)
            self.now[0] += 1
        self.now[0] += battle_log._UNHANDLED_GAP + 1  # 中间几帧被别的处理函数认领了
        for _ in range(8):
            battle_log.unhandled_frame(self.task)
            self.now[0] += 1
        self.assertEqual([], self.rows())

    def test_esc_after_long_unhandled_page(self):
        # 实跑：结算页按钮换成「为记忆的尽头」，没有处理函数认领，一直停着
        keys = []
        self.task.send_key, self.task.sleep = keys.append, lambda s: None
        for _ in range(35):
            utils.log_unhandled_page(self.task)
            self.now[0] += 1
        self.assertEqual(["esc", "esc"], keys)              # 第 20 秒按一次，隔 10 秒再按一次
        self.assertEqual(2, sum(r["event"] == "ESC兜底" for r in self.rows()))

    def test_no_esc_during_battle(self):
        keys = []
        self.task.send_key, self.task.sleep = keys.append, lambda s: None
        battle_log.battle_frame(self.task, True)
        for _ in range(30):
            utils.log_unhandled_page(self.task)
            self.now[0] += 1
        self.assertEqual([], keys)

    def test_once_per_round(self):
        self.assertTrue(battle_log.once(self.task, "零式系统进入"))
        self.assertFalse(battle_log.once(self.task, "零式系统进入"))
        battle_log.end_round(self.task)
        self.assertTrue(battle_log.once(self.task, "零式系统进入"))

    def test_discovery_page_records_choice(self):
        import utils_chaos
        texts = [Box(1200, 130, 200, 40, name="获得法典"),
                 Box(1900, 830, 300, 40, name="存档资料储存上限150pt")]
        task = SimpleNamespace(**vars(self.task))
        task.all_texts, task.width, task.height = texts, 2560, 1440
        task.config = {"游戏语言": "繁体中文", utils_chaos.STORAGE_CAPACITY_KEY: 140}
        task.sleep = lambda s: None
        with mock.patch.object(utils_chaos, "_move_and_click", lambda t, x, y: None):
            self.assertTrue(utils_chaos.handle_discovery_select(task))
        row = self.rows()[0]
        self.assertEqual("获得法典", row["event"], row)
        self.assertEqual(("获得法典", [None, None, "150pt"], 3), (row["event"], row["options"], row["chosen"]))


class TestRoundSuccessCount(unittest.TestCase):

    def test_success_counted_once_per_round(self):
        task = mock.MagicMock()
        task.node_status = {"pass_final_boss_count": 1, "success_rounds": 0, "total_rounds": 0}
        task.default_config = {"只打第一层": True}
        task.config = {}
        for _ in range(3):  # 结算页连续识别三帧
            utils._finish_only_first_layer(task)
        self.assertEqual(1, task.node_status["success_rounds"])

    def test_only_first_layer_retreats_at_boss(self):
        clicks, escaped = [], []
        task = mock.MagicMock()
        task.node_status = utils._initial_node_status()
        task.default_config = {"只打第一层": True}
        task.config = {}
        task._boss_retreat_click_time = 0
        menu_open = {"value": False}
        with mock.patch.object(utils, "_move_and_click", lambda t, x, y: clicks.append((x, y))), \
                mock.patch.object(utils, "handle_escape", lambda t: menu_open["value"] and not escaped.append(1)):
            for _ in range(3):  # 菜单还没弹出，路线页连续识别三帧：只点一次右上角
                self.assertTrue(utils._retreat_before_boss(task))
            menu_open["value"] = True
            self.assertTrue(utils._retreat_before_boss(task))
        self.assertEqual([(0.959, 0.053)], clicks)
        self.assertEqual([1], escaped)
        self.assertEqual(1, task.node_status["success_rounds"])

    def test_escape_menu_only_clicked_after_deciding_to_retreat(self):
        # 实跑 22:05：关卡牌弹窗的 ESC 落到战斗里打开了菜单，看到「撤退」就点了，满血放弃一轮
        task = mock.MagicMock()
        task.node_status = utils._initial_node_status()
        task._escape_requested_at = 0
        button = Box(0, 0, 10, 10, name="逃脱")
        with mock.patch.object(utils, "find_box_at_point", lambda t, x, y: button), \
                mock.patch.object(utils, "_get_game_text", lambda t, s: s), \
                mock.patch.object(utils.battle_log, "anomaly", lambda *a, **k: None):
            self.assertTrue(utils.handle_escape(task))
            task.send_key.assert_called_with("esc")
            task.click_box.assert_not_called()
            self.assertFalse(task.node_status["is_escaped"])
            with mock.patch.object(utils, "_move_and_click", lambda *a: None):
                utils._open_escape_menu(task, 0.053)
            self.assertTrue(utils.handle_escape(task))
            task.click_box.assert_called_with(button)
            self.assertTrue(task.node_status["is_escaped"])

class TestStuckReport(unittest.TestCase):
    def test_stuck_screen_reported_once_per_episode(self):
        # 实跑 12:15~13:10：战斗里读不到手牌数、画面一动不动 55 分钟，没留任何截图
        reports = []
        task = SimpleNamespace(_last_change_time=time.time() - 70, all_texts=[Box(0, 0, 10, 10, name="某页面")],
                               log_info=lambda m: None, sleep=lambda s: None, click_box=lambda b: None,
                               find_one=lambda **k: object(), box_of_screen=lambda *a: None)
        with mock.patch.object(utils, "is_frame_stuck", lambda task, **k: True), \
                mock.patch.object(utils.battle_log, "anomaly", lambda task, kind, detail, **f: reports.append(f["texts"])):
            utils.handle_stuck_log(task)
            utils.handle_stuck_log(task)
            self.assertEqual([["某页面"]], reports)
            task._last_change_time = time.time() - 61  # 画面动过又卡住：新的一次
            utils.handle_stuck_log(task)
        self.assertEqual(2, len(reports))


class TestStuckScreens(unittest.TestCase):
    """实跑中卡住很久的两个画面（真实截图 + 真实 OCR）。"""

    def actions(self, task):
        done = []
        task.send_key = lambda key: done.append(key)
        task.click_box = lambda box, **k: done.append(box.name)
        task.sleep = lambda s: None
        return done

    def test_selected_card_hint_cancelled_with_esc(self):
        # 16:24：选中的牌没打出去，左下角「ENTER 使用卡牌 / ESC 取消选择」，点文字点了 50 分钟都没用
        import utils_sortie
        task = screenshot_task("card_selected_stuck")
        done = self.actions(task)
        self.assertFalse(utils.monster_panel_open(task))
        self.assertTrue(utils_sortie.handle_card_info_popup(task))
        self.assertTrue(utils_sortie.handle_card_info_popup(task))
        self.assertEqual(["esc", "取消选择"], done)  # 先按 ESC，还在就改点文字

    def test_monster_panel_alternates_click_and_esc(self):
        # 17:48：大 Boss 的身体伸到关闭位置，点「关闭」又把面板点开，来回卡了两个小时
        task = screenshot_task("monster_panel_stuck")
        done = self.actions(task)
        clicks = []
        with mock.patch.object(utils, "_move_and_click", lambda task, x, y: clicks.append((x, y))):
            self.assertTrue(utils.handle_weakness_info(task))
            self.assertTrue(utils.handle_weakness_info(task))
        self.assertEqual([(0.502, 0.092)], clicks)
        self.assertEqual(["esc"], done)

    def test_zero_hp_when_hp_text_unreadable(self):
        # 实跑 17:35：血量打到 0 后血量文字读不出来（hp 为 null），照样去出崩溃牌「冲动」「衡重」，都出不掉
        full = screenshot_task("boss_full_hp").frame
        empty = np.zeros((1080, 1920, 3), np.uint8)
        self.assertTrue(battle.is_zero_hp((0, 1583), None, full))
        self.assertTrue(battle.is_zero_hp(None, (177, 1583), empty))
        self.assertFalse(battle.is_zero_hp(None, (177, 1583), full))      # 血条上还有绿色：只是文字被挡住
        self.assertFalse(battle.is_zero_hp(None, (1200, 1583), empty))    # 上次还剩很多血：不可能一下到 0
        self.assertFalse(battle.is_zero_hp(None, None, empty))

    def test_zero_hp_carried_into_new_battle(self):
        # 实跑 17:48 战斗 4：带着 0 血进场，本场一次血量都没读到，血条是紫色乱码；没认出 0 血，去按崩溃牌「衝動」
        collapsed = screenshot_task("zero_hp_collapse").frame
        self.assertTrue(battle.is_zero_hp(None, None, collapsed))
        for name in ("boss_full_hp", "hand_lowered", "preview_hit", "intent_panel"):
            self.assertFalse(battle.hp_bar_collapsed(screenshot_task(name).frame), name)

    def test_collect_intent_does_not_click_close_when_panel_never_opened(self):
        # 意图采集点开敌人后没读到面板：不能去点「关闭」位置（大 Boss 身上）
        task = screenshot_task("boss_full_hp")
        task.sleep = lambda s: None
        task.next_frame = lambda: task.frame
        clicks = []
        enemy_info = {"icon_region": (0.6, 0.1, 0.65, 0.15), "drop": (0.7, 0.4)}
        with mock.patch.object(battle, "_move_and_click", lambda task, x, y: clicks.append((x, y))), \
                mock.patch.object(battle.battle_log, "anomaly", lambda *a, **k: None), \
                mock.patch.object(utils, "_move_and_click", lambda task, x, y: clicks.append(("close", x, y))):
            battle.collect_intent(task, enemy_info)
        self.assertEqual([(0.7, 0.4)], clicks)


def flash_task(**config):
    return SimpleNamespace(name="自动出击模式", config=config, default_config={}, node_status={"flash_done_cards": []},
                           log_info=lambda m: None)


def version(name, card_type, description):
    return {"name": name, "type": card_type, "description": description}


class TestFlash(unittest.TestCase):
    """休息区闪光：选哪个版本、列表里的牌是否已经闪完。"""

    def test_per_card_rule_only_applies_to_that_card(self):
        task = flash_task(闪光优先级=["银色之幕", "水之根源:AP3"])
        rules = utils._flash_rules(task)
        self.assertEqual(["水之根源:AP3", "银色之幕"], [r[0] for r in rules])  # 按牌写的排在前面
        water = version("水之根源", "技能", "获得124%治愈「消灭】获得AP3")
        self.assertTrue(utils._flash_rule_matches(rules[0], water))
        self.assertFalse(utils._flash_rule_matches(rules[0], version("破碎", "攻击", "获得AP3")))
        self.assertTrue(utils._flash_rule_matches(rules[1], version("重新集结", "技能", "获得168%护盾银色之幕1")))

    def test_version_keeps_type_then_largest_number(self):
        # 实跑 13:55：秃鹰发射三个版本，原逻辑随机选
        cards = [version("秃鹰发射", "攻击", "对所有敌人造成144%的伤害获得120%的护盾"),
                 version("秃鹰发射", "技能", "获得336%护盾银色之幕1"),
                 version("秃鹰发射", "攻击", "对所有敌人造成216%的伤害获得180%的护盾")]
        chosen, reason = utils.choose_flash_version(cards)
        self.assertIs(cards[2], chosen)  # 336% 的那个把攻击牌变成了技能牌，不选
        self.assertIn("216", reason)

    def test_missing_list_cards_are_done_for_this_run(self):
        task = flash_task(闪光卡牌列表=["破碎", "斗志"])
        seen = {"破碎": {"type": "攻击"}, "水之根源": {"type": "技能"}}
        utils._record_flash_done(task, ["破碎", "斗志"], seen, "测试")
        self.assertEqual(["斗志"], task.node_status["flash_done_cards"])
        self.assertFalse(utils.flash_list_done(task))   # 破碎还在选牌页里：同名的继续闪
        utils._record_flash_done(task, ["破碎", "斗志"], {"水之根源": {"type": "技能"}}, "测试")
        self.assertTrue(utils.flash_list_done(task))    # 都不在了：休息区改为休息
        self.assertTrue(utils.flash_list_done(flash_task(闪光卡牌列表=[])))

    def test_fallback_uses_play_priority_then_attack(self):
        seen = {"水之根源": {"type": "技能"}, "斩击": {"type": "基础攻击"}, "日破碎": {"type": "攻击"}}
        with mock.patch.object(utils, "_get_game_text", lambda task, text: text):
            self.assertEqual("日破碎", utils._flash_fallback_card(flash_task(出牌优先级=["斗志", "破碎"]), seen))
            self.assertEqual("斩击", utils._flash_fallback_card(flash_task(出牌优先级=["斗志"]), seen))


def read_frame(name):
    return cv2.imdecode(np.fromfile(os.path.join(IMAGES, name + ".jpg"), dtype=np.uint8), cv2.IMREAD_COLOR)


class TestEgo(unittest.TestCase):
    def test_ready_slots_by_color(self):
        # 费用框浅青色放得起；灰色 EP 不够（左下角 EP 数字比费用小）
        for name, ready in (("boss_full_hp", [True, True, True]), ("boss_minions_red", [False, False, True]),
                            ("collapse_traps", [True, False, True]), ("intent_panel", [False, False, False])):
            with self.subTest(name):
                self.assertEqual(ready, [s["ready"] for s in battle.ego_slots(read_frame(name))])

    def _use(self, node_type, slots, full=False, costs=None):
        keys = []
        task = SimpleNamespace(frame=np.zeros((1440, 2560, 3), np.uint8), node_status={"node_type": node_type},
                               config={}, default_config={battle_log.LOG_KEY: False}, log_info=lambda m: None,
                               sleep=lambda s: None, send_key=keys.append, _battle={})
        ready = [{"key": k, "y": y, "ready": r} for (k, y), r in zip(battle._EGO_SLOTS, slots)]
        costs = iter(costs or [])
        with mock.patch.object(battle, "ego_slots", lambda frame: [dict(s) for s in ready]), \
                mock.patch.object(battle, "ep_full", lambda frame: full), \
                mock.patch.object(battle, "_read_digit", lambda *a, **k: next(costs, None)):
            used = [battle.use_ego(task, True) for _ in range(3)]
        return used, keys

    def test_boss_uses_highest_cost_ready_ego(self):
        used, keys = self._use("boss", [True, False, True], costs=[2, 5, 2, 5])
        self.assertEqual("F3", keys[0])
        self.assertEqual(["F3", "enter", "F3", "enter", "F1", "enter"], keys)  # 同一个最多按 2 次
        self.assertEqual([True, True, True], used)

    def test_normal_battle_waits_for_full_ep(self):
        self.assertEqual(([False] * 3, []), self._use("小怪", [True, True, True]))
        used, keys = self._use("小怪", [True, True, True], full=True, costs=[3, 6, 2])
        self.assertEqual("F2", keys[0])

    def test_elite_spends_like_boss(self):
        self.assertTrue(self._use("精英", [False, True, False])[0][0])


if __name__ == "__main__":
    unittest.main()
