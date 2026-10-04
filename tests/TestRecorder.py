# 现场记录（ok_tasks/recorder.py）与现场包工具（scripts/scene.py）测试
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ok_tasks"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from ok import Box  # noqa: E402

import battle_log  # noqa: E402
import recorder  # noqa: E402
import scene  # noqa: E402


class TaskDisabledException(Exception):
    pass


class FakeTask:
    """像加速补丁接管后的任务：run 里全屏识别一次，然后点画面上的按钮。"""

    def __init__(self):
        self.name = "自动出击模式"
        self.config = {}
        self.default_config = {battle_log.LOG_KEY: True, battle_log.SHOT_KEY: True}
        self.config_type = {}
        self.config_description = {}
        self.executor = SimpleNamespace(_frame=None)
        self._speedup = {"active": True, "hit": None, "gated": False}
        self.node_status = {"node_count": 1}
        self.page = "确认"
        self.raise_error = None
        self.speedup_on = True
        self.handled = True
        self.logs = []

    def log_info(self, message):
        self.logs.append(message)

    notes = None

    def notification(self, message, title=None, error=False):
        self.notes = (self.notes or []) + [message]

    @property
    def frame(self):
        return self.executor._frame

    def ocr(self, *args, **kwargs):
        self.executor._frame = np.full((720, 1280, 3), 80, np.uint8)  # 每帧一个新画面
        return [Box(600, 300, 80, 30, name=self.page)]

    def click(self, x=-1, y=-1, **kwargs):
        return True

    def click_box(self, box=None, relative_x=0.5, relative_y=0.5, **kwargs):
        return self.click(box.x + box.width * relative_x, box.y + box.height * relative_y)  # 里层的 click 不重复记

    def send_key(self, key, **kwargs):
        return True

    def run(self):
        self._speedup["hit"] = None
        self._speedup["active"] = self.speedup_on
        texts = self.ocr()
        if self.raise_error:
            raise self.raise_error
        if self.handled:
            self._speedup["hit"] = "handle_confirm"
            self.click_box(texts[0])
        self._speedup["active"] = False  # 和真实的加速补丁一样，run 结束时复位


class RecorderTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.now = [1_000_000.0]
        for patcher in (
                mock.patch("time.time", lambda: self.now[0]),
                mock.patch.object(battle_log, "LOG_DIR", self.folder),
                mock.patch.object(battle_log, "SHOT_DIR", os.path.join(self.folder, "截图")),
                mock.patch.object(recorder, "SCENE_DIR", os.path.join(self.folder, "现场")),
                mock.patch.object(scene, "SCENE_DIR", os.path.join(self.folder, "现场")),
                mock.patch.object(recorder, "_schedule", lambda *args: None),
                mock.patch.object(recorder, "_start_write", recorder._write),
                # 采样线程在用例里手动调 _sample_once，不真起后台线程
                mock.patch.object(recorder, "_start_sampler", lambda task, st: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.task = FakeTask()
        recorder.install(self.task)

    def frames(self, seconds, step=0.5):
        for _ in range(int(seconds / step)):
            self.now[0] += step
            self.task.run()

    def scenes(self):
        folder = os.path.join(self.folder, "现场")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def scene_path(self, index=0):
        return os.path.join(self.folder, "现场", self.scenes()[index])

    def events(self, name):
        found = []
        for file in os.listdir(self.folder):
            if file.endswith(".jsonl"):
                with open(os.path.join(self.folder, file), encoding="utf-8") as f:
                    found += [json.loads(line) for line in f if json.loads(line)["event"] == name]
        return found


class TestRecording(RecorderTestCase):
    def test_writes_thirty_seconds_before_and_after(self):
        self.frames(40)
        battle_log.anomaly(self.task, "疑似循环", "测试")
        self.frames(29)
        self.assertEqual([], self.scenes())  # 后 30 秒还没录完
        self.frames(2)
        self.assertEqual(1, len(self.scenes()))
        path = self.scene_path()
        meta, frames = scene.load(path)
        self.assertEqual("自动出击模式", meta["mode"])
        self.assertEqual(["疑似循环"], [t["kind"] for t in meta["triggers"]])
        self.assertAlmostEqual(30, meta["triggers"][0]["t"], delta=1)
        self.assertAlmostEqual(60, meta["seconds"], delta=1.5)
        first = frames[0]
        self.assertEqual("handle_confirm", first["hit"])
        self.assertEqual([["确认", 600, 300, 80, 30]], first["texts"])
        self.assertEqual("ocr", first["calls"][0]["m"])
        self.assertEqual(1, len(first["actions"]))  # click_box 里调用的 click 不重复记
        self.assertEqual("click_box", json.loads(first["actions"][0]["a"])[0])
        self.assertTrue(os.listdir(os.path.join(path, "frames")))
        self.assertTrue(os.listdir(os.path.join(path, "full")))  # 有动作的帧存原尺寸
        self.assertTrue(os.path.exists(os.path.join(path, "state.pkl")))
        # 异常记录指向现场包，不再单独截图；写完记一条「现场包」
        self.assertEqual(path, self.events("异常")[0]["scene"])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "截图")))
        self.assertEqual(path, self.events("现场包")[0]["path"])

    def test_trigger_during_recording_extends_same_scene(self):
        self.frames(10)
        battle_log.anomaly(self.task, "疑似循环", "第一次")
        self.frames(20)
        battle_log.anomaly(self.task, "出不掉牌", "录制中又出问题")
        self.frames(25)
        self.assertEqual([], self.scenes())  # 从第二次触发起再录 30 秒
        self.frames(10)
        self.assertEqual(1, len(self.scenes()))
        meta, _ = scene.load(self.scene_path())
        self.assertEqual(["疑似循环", "出不掉牌"], [t["kind"] for t in meta["triggers"]])

    def test_scene_length_is_capped(self):
        self.frames(30)
        for _ in range(12):
            battle_log.anomaly(self.task, "疑似循环", "一直在循环")
            self.frames(20)
        meta, _ = scene.load(self.scene_path())
        self.assertLessEqual(meta["seconds"], recorder.MAX_SECONDS + 1)

    def test_same_kind_on_same_page_saved_once_per_round(self):
        self.frames(5)
        battle_log.anomaly(self.task, "疑似循环", "第一次")
        self.frames(35)
        battle_log.anomaly(self.task, "疑似循环", "同一个页面又循环")
        self.frames(35)
        self.assertEqual(1, len(self.scenes()))
        self.assertIn("同上，第 2 次", self.events("现场包跳过")[0]["reason"])
        battle_log.anomaly(self.task, "画面卡住", "另一种异常")
        self.frames(35)
        self.assertEqual(2, len(self.scenes()))
        battle_log.end_round(self.task, success=True)  # 下一轮重新计
        self.frames(1)
        battle_log.anomaly(self.task, "疑似循环", "新一轮")
        self.frames(35)
        self.assertEqual(3, len(self.scenes()))

    def test_unhandled_page_compares_texts(self):
        self.task.handled = False  # 没有接手的处理函数：按画面文字判断是不是同一个页面
        self.frames(5)
        battle_log.anomaly(self.task, "未识别页面", "A")
        self.frames(35)
        self.task.page = "完全不同的页面"
        self.frames(1)
        battle_log.anomaly(self.task, "未识别页面", "B")
        self.frames(35)
        self.assertEqual(2, len(self.scenes()))

    def test_round_limit_and_manual_mark(self):
        kinds = ["疑似循环", "画面卡住", "出不掉牌", "按键无效", "手牌识别失败", "按 E 无效"]
        for kind in kinds:
            battle_log.anomaly(self.task, kind, "测试")
            self.frames(31)
        self.assertEqual(recorder.MAX_PER_ROUND, len(self.scenes()))
        self.assertIn("本轮已存", self.events("现场包跳过")[-1]["reason"])
        self.assertIsNone(recorder.hold(self.task))  # 侧边栏「标记现场」按钮
        recorder.mark(self.task, "卡在确认页")
        self.assertIn("已标记", self.task.notes[-1])
        self.frames(31)
        self.assertIn("已写入", self.task.notes[-1])
        self.assertEqual(recorder.MAX_PER_ROUND + 1, len(self.scenes()))
        self.assertTrue(self.scenes()[-1].endswith(recorder.MANUAL))
        meta, _ = scene.load(self.scene_path(-1))
        self.assertEqual(["卡在确认页"], meta["notes"])
        self.assertIn("卡在确认页", meta["triggers"][0]["detail"])

    def test_mark_counts_from_button_press_while_writing_note(self):
        # 手动标记：按下按钮前 50 秒、后 10 秒（自动触发仍是前后各 30 秒，见上一条用例）
        self.frames(70)
        self.assertIsNone(recorder.hold(self.task))
        self.frames(5)  # 写说明花了 5 秒，任务照常在跑
        recorder.mark(self.task, "说明")
        self.frames(8)
        self.assertEqual(1, len(self.scenes()))  # 录到按下按钮后 10 秒
        meta, _ = scene.load(self.scene_path())
        self.assertAlmostEqual(50, meta["triggers"][0]["t"], delta=1)  # 按下按钮前的 50 秒都在
        self.assertAlmostEqual(60, meta["seconds"], delta=1.5)

    def test_cancel_note_saves_nothing(self):
        self.frames(5)
        self.assertIsNone(recorder.hold(self.task))
        recorder.unhold(self.task)
        self.frames(40)
        self.assertEqual([], self.scenes())
        self.assertLessEqual(len(self.task._recorder["frames"]), recorder.KEEP_SECONDS / 0.5 + 1)

    def test_background_drag_is_recorded(self):
        original = self.task.click_box
        self.task.click_box = lambda box: recorder.note_action(self.task, "drag", (0.4, 0.86), (0.5, 0.45))
        self.frames(1)
        self.task.click_box = original
        frame = self.task._recorder["frames"][-1]
        self.assertEqual("drag", json.loads(frame["actions"][0]["a"])[0])
        self.assertIsNotNone(frame["actions"][0]["img"])

    def test_mark_when_not_running_says_so(self):
        self.assertIn("没在运行", recorder.hold(self.task))
        self.assertIsNone(self.task._recorder["hold"])

    def test_mark_says_log_off_when_running_but_disabled(self):
        # 实跑：关掉「详细战斗日志」后任务照跑，running() 却一直 False，标记按钮误报「任务没在运行」
        self.task.config[battle_log.LOG_KEY] = False
        self.frames(5)
        self.assertEqual(0, len(self.task._recorder["frames"]))  # 照旧不记帧
        self.assertTrue(recorder.running(self.task))
        self.assertIn("没开", recorder.hold(self.task))
        self.assertIsNone(self.task._recorder["hold"])

    def test_handled_frame_resets_unhandled_timer(self):
        # 实跑：事件对话几帧没人认领、一帧点「继续」，交替着累计 10 秒就误报「未识别页面」
        for _ in range(8):
            for _ in range(4):
                self.now[0] += 0.5
                battle_log.unhandled_frame(self.task)
            battle_log.handled_frame(self.task)
        self.assertEqual([], self.events("异常"))
        for _ in range(21):
            self.now[0] += 0.5
            battle_log.unhandled_frame(self.task)
        self.assertEqual(["未识别页面"], [e["kind"] for e in self.events("异常")])

    def test_common_anomaly_only_screenshot(self):
        self.frames(5)
        battle_log.anomaly(self.task, "AP不足", "常见且有兜底")
        self.frames(35)
        self.assertEqual([], self.scenes())
        self.assertTrue(os.listdir(os.path.join(self.folder, "截图")))

    def test_lost_battle_triggers(self):
        battle_log.battle_frame(self.task, True)
        self.frames(20)
        battle_log.end_round(self.task, success=False)
        self.frames(31)
        meta, _ = scene.load(self.scene_path())
        self.assertEqual([recorder.LOST_BATTLE], [t["kind"] for t in meta["triggers"]])

    def test_handler_error_triggers_but_task_stop_does_not(self):
        self.frames(5)
        self.task.raise_error = TaskDisabledException()
        with self.assertRaises(TaskDisabledException):
            self.task.run()
        self.task.raise_error = ValueError("坏了")
        with self.assertRaises(ValueError):
            self.task.run()
        self.task.raise_error = None
        self.frames(31)
        meta, frames = scene.load(self.scene_path())
        self.assertEqual([recorder.HANDLER_ERROR], [t["kind"] for t in meta["triggers"]])
        self.assertIn("ValueError: 坏了", [f["error"] for f in frames])

    def test_disabled_keeps_nothing(self):
        self.task.config[battle_log.SHOT_KEY] = False
        self.frames(5)
        self.assertEqual(0, len(self.task._recorder["frames"]))
        self.assertIsNone(recorder.trigger(self.task, "疑似循环"))


class TestSampler(RecorderTestCase):
    """画面采样：任务运行时另开一路抓画面（任务帧之外），画面没变化的不重复留。"""

    def setUp(self):
        super().setUp()
        self.task.executor._frame = np.full((720, 1280, 3), 80, np.uint8)

        def get_frame():
            return self.task.executor._frame

        self.task.executor.method = SimpleNamespace(get_frame=get_frame)
        self.task._recorder["last_frame_t"] = self.now[0]

    def sample(self):
        recorder._sample_once(self.task, self.task._recorder)

    def images(self):
        return self.task._recorder["images"]

    def test_unchanged_frames_are_not_kept_twice(self):
        self.sample()
        self.assertEqual(1, len(self.images()))
        self.sample()  # 画面没变：不重复留
        self.assertEqual(1, len(self.images()))
        self.task.executor._frame = np.full((720, 1280, 3), 200, np.uint8)  # 画面变了
        self.sample()
        self.assertEqual(2, len(self.images()))

    def test_sample_skipped_when_task_not_running(self):
        self.task._recorder["last_frame_t"] = self.now[0] - 10  # 超过 RUNNING_GAP
        self.sample()
        self.assertEqual(0, len(self.images()))

    def test_sample_errors_do_not_raise(self):
        def boom():
            raise RuntimeError("截图失败")

        self.task.executor.method = SimpleNamespace(get_frame=boom)
        self.sample()  # 采样失败静默跳过，不能影响任务
        self.assertEqual(0, len(self.images()))

    def test_sample_frames_go_into_scene_timeline(self):
        self.frames(2)  # 正常跑几帧
        self.task.executor._frame = np.full((720, 1280, 3), 200, np.uint8)
        self.task._recorder["last_frame_t"] = self.now[0]
        self.sample()  # 一张任务帧之外的采样画面
        battle_log.anomaly(self.task, "疑似循环", "测试")
        self.frames(32)
        path = self.scene_path()
        _, frames = scene.load(path)
        samples = [f for f in frames if f.get("sample")]
        self.assertTrue(samples)  # 采样帧作为「只有画面」的条目进了时间线
        for sample in samples:
            for image_id in sample["images"]:
                self.assertTrue(os.path.exists(os.path.join(path, "frames", f"{image_id:05d}.jpg")))


class TestSceneTool(RecorderTestCase):
    def make_scene(self):
        self.frames(10)
        battle_log.anomaly(self.task, "疑似循环", "测试")
        self.frames(31)
        return self.scene_path()

    def run_cmd(self, fn, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(SimpleNamespace(**kwargs))
        return out.getvalue()

    def test_list_timeline_draw_html(self):
        path = self.make_scene()
        name = os.path.basename(path)
        self.assertIn(name, self.run_cmd(scene.cmd_list))
        timeline = self.run_cmd(scene.cmd_timeline, scene=path, texts=False)
        self.assertIn("触发「疑似循环」", timeline)
        self.assertIn("点「确认」", timeline)
        self.assertIn("同上", timeline)
        _, frames = scene.load(path)
        index = next(f["i"] for f in frames if f["images"])
        drawn = self.run_cmd(scene.cmd_draw, scene=path, frame=index)
        self.assertIn("0: 确认", drawn)
        self.assertTrue(os.listdir(os.path.join(path, "annotated")))
        self.run_cmd(scene.cmd_html, scene=path)
        with open(os.path.join(path, "index.html"), encoding="utf-8") as f:
            self.assertIn("handle_confirm", f.read())
        with open(os.path.join(path, scene.DIAGNOSIS), "w", encoding="utf-8") as f:
            f.write("已看")
        self.assertNotIn(name, self.run_cmd(scene.cmd_list))

    def test_action_point(self):
        size = [2560, 1440]
        box = json.dumps(["click_box", [{"box": [100, 200, 50, 20, "确认"]}], {}])
        self.assertEqual((125, 210), scene.action_point({"a": box}, size))
        relative = json.dumps(["click", [0.5, 0.25], {}])
        self.assertEqual((1280, 360), scene.action_point({"a": relative}, size))
        drag = json.dumps(["drag", [[0.4, 0.86], [0.5, 0.45]], {}])
        self.assertEqual((1280, 648), scene.action_point({"a": drag}, size))
        self.assertIn("拖牌", scene.action_text({"a": drag}))
        key = json.dumps(["send_key", ["e"], {}])
        self.assertIsNone(scene.action_point({"a": key}, size))

    def test_replay_matches_and_detects_changed_logic(self):
        path = self.make_scene()

        def handle_confirm(task):
            box = next((b for b in task.all_texts if b.name == "确认"), None)
            if box is None:
                return False
            task.click_box(box)
            return True

        module = types.ModuleType("fake_handlers")
        module.PAGE_HANDLERS = [handle_confirm]
        with mock.patch.dict(sys.modules, {"fake_handlers": module}), \
                mock.patch.dict(scene.MODE_HANDLERS, {"自动出击模式": "fake_handlers"}), \
                mock.patch("os.chdir"):
            out = self.run_cmd(scene.cmd_replay, scene=path, verbose=False)
            self.assertIn("不一致 0", out)

            def handle_confirm_changed(task):  # 改了逻辑：点到按钮右边去了
                box = next(b for b in task.all_texts if b.name == "确认")
                task.click_box(box, relative_x=0.9)
                return True

            handle_confirm_changed.__name__ = "handle_confirm"
            module.PAGE_HANDLERS = [handle_confirm_changed]
            out = self.run_cmd(scene.cmd_replay, scene=path, verbose=False)
            self.assertNotIn("不一致 0", out)
            self.assertIn("重放动作", out)


class TestRealModes(unittest.TestCase):
    def test_both_modes_record(self):
        import ChaosMode
        import SortieMode
        from src.config import config

        for cls in (ChaosMode.ChaosMode, SortieMode.SortieMode):
            with self.subTest(cls.__name__):
                task = cls(executor=SimpleNamespace(scene=None, config=config), app=None)
                self.assertIsNotNone(getattr(task, "_recorder", None))
                self.assertNotIn(recorder.MARK_KEY, task.config_type)  # 按钮在侧边栏，不在任务设置里


if __name__ == "__main__":
    unittest.main()
