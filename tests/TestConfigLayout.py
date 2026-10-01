# 配置界面整理（ok_tasks/config_layout.py）：分组顺序、隐藏项、子选项、说明文字
import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ok_tasks"))

import config_layout  # noqa: E402


def make_task(class_name):
    module = __import__(class_name)
    from src.config import config
    return getattr(module, class_name)(executor=SimpleNamespace(scene=None, config=config), app=None)


class TestConfigLayout(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tasks = {"chaos": make_task("ChaosMode"), "sortie": make_task("SortieMode")}
        cls.orders = {"chaos": config_layout.CHAOS_ORDER, "sortie": config_layout.SORTIE_ORDER}

    def visible_keys(self, task):
        return [key for key in task.default_config
                if not key.startswith("_") and not task.config_type.get(key, {}).get("hidden")]

    def test_visible_keys_follow_groups(self):
        for mode, task in self.tasks.items():
            with self.subTest(mode=mode):
                self.assertEqual(self.orders[mode], self.visible_keys(task))   # 每个显示的项都排进了分组

    def test_hidden_keys_still_have_defaults(self):
        for mode, task in self.tasks.items():
            for key in config_layout.HIDDEN_KEYS:
                if key in task.default_config:
                    with self.subTest(mode=mode, key=key):
                        self.assertTrue(task.config_type[key]["hidden"])
        self.assertIn("意图采集", self.tasks["sortie"].default_config)
        self.assertIn("点击前悬停等待(秒)", self.tasks["chaos"].default_config)

    def test_removed_keys(self):
        for key in ("丢弃卡牌优先级", "从右往左出牌"):
            self.assertNotIn(key, self.tasks["sortie"].default_config)

    def test_sub_configs(self):
        chaos = self.tasks["chaos"]
        self.assertEqual({True: ["刷新商店"]}, chaos.config_type["进入商店"]["sub_configs"])
        self.assertEqual({True: ["优先使用金币治疗"]}, chaos.config_type["治疗崩溃"]["sub_configs"])
        # 出击模式没有「刷新商店」，「进入商店」保持普通开关
        self.assertNotIn("sub_configs", self.tasks["sortie"].config_type.get("进入商店", {}))

    def test_every_visible_key_has_description(self):
        for mode, task in self.tasks.items():
            for key in self.visible_keys(task):
                if key == "配置操作":
                    continue
                with self.subTest(mode=mode, key=key):
                    self.assertTrue(task.config_description.get(key))


if __name__ == '__main__':
    unittest.main()
