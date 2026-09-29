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

    def test_logged_turn_no_longer_spends_all_ap_first(self):
        # 实跑 21:03:54：3 AP，出牌优先级有「破碎」，以前先出 3 费破碎把 AP 用光
        cards = [card("饥饿的枷锁", 1, "攻击"), card("扭曲：光荣的抵抗", None, "强化"), card("冻结之拳", None, "攻击"),
                 card("寒霜盾牌", None, "技能"), card("破碎", 3, "攻击")]
        chosen, reason = battle.choose_play(cards, 3, ["破碎"], [], False, set())
        self.assertEqual("扭曲：光荣的抵抗", chosen["name"])
        cards.remove(chosen)
        chosen, _ = battle.choose_play(cards, 2, ["破碎"], [], False, set())
        self.assertEqual("饥饿的枷锁", chosen["name"])  # 破碎出不起（3 费），先出便宜的攻击

    def test_priority_orders_within_same_type_then_cost(self):
        cards = [card("斩击", 1, "攻击"), card("破碎", 2, "攻击"), card("秃鹰发射", 1, "攻击")]
        self.assertEqual("破碎", battle.choose_play(cards, 3, ["破碎"], [], False, set())[0]["name"])
        self.assertEqual("斩击", battle.choose_play(cards, 3, [], [], False, set())[0]["name"])

    def test_lethal_plays_defense_first(self):
        cards = [card("破碎", 2, "攻击"), card("孢子", 0), card("刀背格挡", 1)]
        chosen, reason = battle.choose_play(cards, 3, ["破碎"], ["刀背格挡"], True, set())
        self.assertEqual("刀背格挡", chosen["name"])
        self.assertIn("会被打死", reason)
        self.assertEqual("孢子", battle.choose_play(cards, 3, ["破碎"], ["刀背格挡"], False, set())[0]["name"])

    def test_unknown_cost_is_tried_but_not_when_no_cost_left(self):
        cards = [card("未识别1", None)]
        self.assertIsNotNone(battle.choose_play(cards, 1, [], [], False, set())[0])
        self.assertIsNone(battle.choose_play(cards, 0, [], [], False, set())[0])
        cards[0]["slot"] = "1/1"
        self.assertIsNone(battle.choose_play(cards, 1, [], [], False, {"1/1"})[0])  # 这个位置提示过 AP不足
        self.assertIsNotNone(battle.choose_play([card("孢子", 0)], 0, [], [], False, set())[0])  # 0 费照出

    def test_collapse_card_first_even_without_ap(self):
        cards = [card("破碎", 3, "攻击"), card("冲动", 0, "崩溃", key="2")]
        cards[1]["progress"] = (1, 5)
        chosen, reason = battle.choose_play(cards, 0, ["破碎"], [], True, set())
        self.assertEqual("冲动", chosen["name"])
        self.assertIn("1/5", reason)

    def test_remaining_unknown_plays_anything(self):
        self.assertEqual("破碎", battle.choose_play([card("破碎", 3, "攻击")], None, [], [], False, set())[0]["name"])


def enemy(hp, countdown, intent=None, x=0.5, y=0.3):
    return {"hp": hp, "shield": 0, "countdown": countdown, "intent": intent, "x": x, "y": y}


class TestChooseTarget(unittest.TestCase):

    def test_boss_battle_hits_boss(self):
        enemies = [enemy(1254, 1, "攻击", x=0.43), enemy(6078, None, x=0.70)]
        self.assertEqual(6078, battle.choose_target(enemies, True, None)[0]["hp"])

    def test_attack_intent_then_countdown_then_hp(self):
        enemies = [enemy(300, 2, "增益", x=0.3), enemy(900, 5, "攻击", x=0.5), enemy(500, 5, "攻击", x=0.7)]
        self.assertEqual(500, battle.choose_target(enemies, False, None)[0]["hp"])
        enemies.append(enemy(800, 1, None, x=0.9))  # 意图认不出按攻击算
        self.assertEqual(800, battle.choose_target(enemies, False, None)[0]["hp"])

    def test_sticky_target_until_gone(self):
        enemies = [enemy(300, 1, "攻击", x=0.3), enemy(900, 5, "攻击", x=0.7)]
        self.assertEqual(900, battle.choose_target(enemies, False, (0.71, 0.3))[0]["hp"])
        self.assertEqual(300, battle.choose_target(enemies[:1], False, (0.71, 0.3))[0]["hp"])


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

    def test_attack_card_is_dragged(self):
        self.task.default_config["出牌优先级"] = ["破碎"]
        battle.play_turn(self.task, 2, True)
        self.assertEqual(["drag"], self.keys)

    def test_card_that_never_leaves_hand_is_skipped(self):
        battle.play_turn(self.task, 1, True)
        battle.play_turn(self.task, 1, True)  # 手牌数和 AP 都没减少：按键出的牌失败一次就不再出
        self.assertIn("斗志", self.task._battle["unplayable"])

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


class TestRoundSuccessCount(unittest.TestCase):

    def test_success_counted_once_per_round(self):
        task = mock.MagicMock()
        task.node_status = {"pass_final_boss_count": 1, "success_rounds": 0, "total_rounds": 0}
        task.default_config = {"只打第一层": True}
        task.config = {}
        for _ in range(3):  # 结算页连续识别三帧
            utils._finish_only_first_layer(task)
        self.assertEqual(1, task.node_status["success_rounds"])


if __name__ == "__main__":
    unittest.main()
