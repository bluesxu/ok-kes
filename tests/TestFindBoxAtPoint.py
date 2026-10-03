# 按钮/文字查找：OCR 把按钮切成两个框、或按钮有高/低两套渲染位置时仍能找到
import os
import re
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ok_tasks"))

from ok import Box  # noqa: E402
import utils  # noqa: E402

WIDTH, HEIGHT = 2145, 1207
# 繁中服「請選擇1張欲賦予靈光一閃的卡牌」页面卡住时的实际 OCR 结果：按钮被切成两个框，
# 中间的空隙正好压住按钮检测点 (0.945, 0.918)
SPLIT_BUTTON = (("跳過", 0.750, 0.792, 0.905, 0.947),
                ("賦予靈光-", 0.862, 0.941, 0.902, 0.949),
                ("一閃", 0.949, 0.973, 0.906, 0.944))

# 10/03 命运结算页：底部按钮随条目数在高/低两处渲染（上下浮动约 45px）。坐标为现场包实际 OCR（2554x1437）。
# 高渲染（11:11 卡死 2.5 小时那次，固定检测点 (0.924,0.922) 全部落空）
FATE_PAGE_HIGH = (("命运", 0.880, 0.910, 0.164, 0.191),
                  ("获得卡牌", 0.822, 0.902, 0.281, 0.326),
                  ("跳过", 0.713, 0.757, 0.860, 0.908),
                  ("获得", 0.896, 0.939, 0.860, 0.910))
# 低渲染（11:08 正常点中那次）
FATE_PAGE_LOW = (("命运", 0.880, 0.910, 0.164, 0.191),
                 ("获得卡牌", 0.822, 0.902, 0.281, 0.326),
                 ("跳过", 0.720, 0.762, 0.897, 0.939),
                 ("获得", 0.903, 0.945, 0.897, 0.939))
# 领完后按钮变「离开」（同高渲染）
FATE_PAGE_LEAVE = (("命运", 0.880, 0.910, 0.164, 0.191),
                   ("离开", 0.923, 0.964, 0.864, 0.906))
# 11:43 的选牌子页面：标题/提示比正常渲染高 10~22px，两个探针都落空
CARD_SELECT_PAGE_HIGH = (("获得卡牌", 0.450, 0.541, 0.072, 0.121),
                         ("请选择1张欲获得的卡牌。", 0.792, 0.972, 0.085, 0.114))


def text_box(name, x1, x2, y1, y2):
    # 与运行时一致：OCR 文本先经 _normalize_text 转为简体
    return Box(x1 * WIDTH, y1 * HEIGHT, to_x=x2 * WIDTH, to_y=y2 * HEIGHT, confidence=0.9,
               name=utils._normalize_text(name))


class FlashPageTask:
    """handle_flash 用到的任务接口。"""

    def __init__(self, boxes):
        self.width, self.height = WIDTH, HEIGHT
        self.config = {"游戏语言": "繁体中文"}
        self.all_texts = [text_box(*box) for box in boxes]
        self.clicked, self.logs = [], []

    def click_box(self, box, *args, **kwargs):
        self.clicked.append(box.name)

    def sleep(self, timeout):
        pass

    def log_info(self, message):
        self.logs.append(message)


class TestFindBoxAtPoint(unittest.TestCase):

    def setUp(self):
        self._is_button_active = utils.is_button_active

    def tearDown(self):
        utils.is_button_active = self._is_button_active

    def test_split_button_text_is_merged(self):
        task = FlashPageTask(SPLIT_BUTTON)
        box = utils.find_box_at_point(task, 0.945, 0.918)
        self.assertIsNotNone(box)
        self.assertIn("灵光一闪", box.name)  # 切开处的「-」已去掉
        self.assertLessEqual(box.x, int(0.862 * WIDTH) + 1)
        self.assertGreaterEqual(box.x + box.width, int(0.973 * WIDTH) - 1)

    def test_handle_flash_clicks_split_button(self):
        utils.is_button_active = lambda task, box: True  # 已选中卡牌，按钮可点
        task = FlashPageTask(SPLIT_BUTTON)
        self.assertTrue(utils.handle_flash(task))
        self.assertEqual(1, len(task.clicked))
        self.assertIn("灵光一闪", task.clicked[0])

    def test_inactive_split_button_is_not_clicked(self):
        utils.is_button_active = lambda task, box: False  # 还没选卡，按钮是灰色
        task = FlashPageTask(SPLIT_BUTTON)
        self.assertFalse(utils.handle_flash(task))
        self.assertEqual([], task.clicked)

    def test_box_containing_point_is_returned_unchanged(self):
        task = FlashPageTask((("賦予靈光一閃", 0.862, 0.973, 0.902, 0.949),))
        box = utils.find_box_at_point(task, 0.945, 0.918)
        self.assertIs(task.all_texts[0], box)

    def test_distant_texts_are_not_merged(self):
        task = FlashPageTask(SPLIT_BUTTON)
        self.assertIsNone(utils.find_box_at_point(task, 0.830, 0.925))  # 「跳過」与按钮之间
        self.assertIsNone(utils.find_box_at_point(task, 0.945, 0.700))  # 该行没有文字

    def test_no_texts(self):
        task = SimpleNamespace(width=WIDTH, height=HEIGHT, all_texts=[])
        self.assertIsNone(utils.find_box_at_point(task, 0.5, 0.5))


class TestFatePageButtons(unittest.TestCase):
    """命运结算页底部按钮：固定检测点只覆盖低渲染，高渲染要靠区域内按文字找（10/03 卡死 2.5 小时）。"""

    def setUp(self):
        self._is_button_active = utils.is_button_active
        utils.is_button_active = lambda task, box: True

    def tearDown(self):
        utils.is_button_active = self._is_button_active

    def test_obtain_button_high_render_is_found(self):
        task = FlashPageTask(FATE_PAGE_HIGH)  # 探针落空，区域搜索命中
        self.assertTrue(utils.handle_obtain_reward(task))
        self.assertEqual(["获得"], task.clicked)

    def test_obtain_button_low_render_still_works(self):
        task = FlashPageTask(FATE_PAGE_LOW)  # 原探针直接命中，行为不变
        self.assertTrue(utils.handle_obtain_reward(task))
        self.assertEqual(["获得"], task.clicked)

    def test_leave_button_high_render_is_found(self):
        task = FlashPageTask(FATE_PAGE_LEAVE)
        self.assertTrue(utils.handle_leave(task))
        self.assertEqual(["离开"], task.clicked)

    def test_card_title_is_not_mistaken_for_button(self):
        # 「获得卡牌」不在按钮条区域里，也不会被「获得」的精确匹配点到
        task = FlashPageTask((("获得卡牌", 0.822, 0.902, 0.281, 0.326),))
        self.assertIsNone(utils.find_button_by_text(task, ("获得",)))
        self.assertFalse(utils.handle_obtain_reward(task))

    def test_card_select_title_and_tip_found_in_region(self):
        task = FlashPageTask(CARD_SELECT_PAGE_HIGH)
        title = utils.find_text_in_region(
            task, lambda name: utils._clean_match(name, "获得卡牌"), (0.360, 0.040, 0.680, 0.180))
        tip = utils.find_text_in_region(
            task, lambda name: re.search(r"请选择.*获得的卡牌", name), (0.700, 0.040, 1.000, 0.200))
        self.assertIsNotNone(title)
        self.assertIsNotNone(tip)

    def test_falls_back_to_esc_when_no_safe_button(self):
        # 卡死兜底：没有安全按钮时才轮到 ESC
        task = FlashPageTask((("命运", 0.880, 0.910, 0.164, 0.191),))
        task.send_key = lambda key: task.clicked.append(key)
        self.assertTrue(utils._esc_fallback(task, "测试卡住"))
        self.assertEqual(["esc"], task.clicked)

    def test_esc_fallback_clicks_safe_button_first(self):
        # 有安全按钮（这里用「离开」）时不按 ESC
        task = FlashPageTask(FATE_PAGE_LEAVE)
        task.send_key = lambda key: task.clicked.append(key)
        task._esc_fallback_at = 0
        self.assertTrue(utils._esc_fallback(task, "测试卡住"))
        self.assertEqual(["离开"], task.clicked)


if __name__ == '__main__':
    unittest.main()
