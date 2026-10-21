import json
import os
import sys

from PySide6.QtCore import QObject
from qfluentwidgets import (BodyLabel, FluentIcon, InfoBar, InfoBarPosition, MessageBoxBase, NavigationItemPosition,
                            PlainTextEdit, SubtitleLabel)

from ok import Logger, og

logger = Logger.get_logger(__name__)

MARK_TEXT = "标记现场"
SCENE_TEXT = "现场记录"
# 与 ok_tasks/recorder.py 的 SWITCH_FILE / SWITCH_KEY 一致；任务没加载时界面也要能读写
SCENE_SWITCH_FILE = os.path.join("configs", "现场记录.json")
SCENE_SWITCH_KEY = "开启"


class Globals(QObject):

    def __init__(self, exit_event):
        super().__init__()

    def on_show_main_window(self, main_window):
        """侧边栏加「现场记录」开关和「标记现场」按钮（现场包见 ok_tasks/recorder.py）。"""
        self._scene_item = main_window.navigationInterface.addItem(
            routeKey="scene_switch", icon=FluentIcon.VIDEO, text=self._scene_text(_read_scene_switch()),
            onClick=lambda: self._toggle_scene(main_window), selectable=False,
            position=NavigationItemPosition.BOTTOM,
            tooltip="开着时内存里一直留最近 50 秒的画面和识别结果，出问题或按「标记现场」时存成现场包；"
                    "后台每秒抓十几张图，比较占 CPU，排查问题时再开")
        main_window.navigationInterface.addItem(
            routeKey="mark_scene", icon=FluentIcon.FLAG, text=og.app.tr(MARK_TEXT),
            onClick=lambda: self._mark_scene(main_window), selectable=False,
            position=NavigationItemPosition.BOTTOM,
            tooltip="把按下按钮前 50 秒、后 10 秒的画面、识别结果和动作存成现场包，供事后排查")

    @staticmethod
    def _scene_text(on):
        return f"{SCENE_TEXT}：{'开' if on else '关'}"

    def _toggle_scene(self, main_window):
        on = not _read_scene_switch()
        recorder = sys.modules.get("recorder")
        if recorder is not None:
            recorder.set_switch(on)
        else:
            _write_scene_switch(on)
        self._scene_item.setText(self._scene_text(on))
        self._tip(main_window, "已开启，出问题时会存现场包" if on else "已关闭", title=SCENE_TEXT)

    def _mark_scene(self, main_window):
        recorder = sys.modules.get("recorder")  # ok_tasks 下的模块，任务加载时已导入；热重载后取最新的
        tasks = [t for t in getattr(og.executor, "trigger_tasks", [])
                 if getattr(t, "_recorder", None) is not None]
        running = [t for t in tasks if recorder and recorder.running(t)]
        if not running:
            self._tip(main_window, "任务没在运行，标记无效", error=True)
            return
        errors = [recorder.hold(t) for t in running]
        held = [t for t, error in zip(running, errors) if error is None]
        if not held:
            self._tip(main_window, errors[0], error=True)
            return
        dialog = _NoteDialog(main_window)
        if dialog.exec():
            note = dialog.note()
            for task in held:
                recorder.mark(task, note)
        else:
            for task in held:
                recorder.unhold(task)

    @staticmethod
    def _tip(main_window, message, error=False, title=MARK_TEXT):
        (InfoBar.error if error else InfoBar.success)(
            title=title, content=message, parent=main_window, position=InfoBarPosition.TOP, duration=4000)


def _read_scene_switch():
    try:
        with open(SCENE_SWITCH_FILE, "r", encoding="utf-8") as f:
            return bool(json.load(f).get(SCENE_SWITCH_KEY, False))
    except (OSError, ValueError, AttributeError):
        return False


def _write_scene_switch(on):
    try:
        os.makedirs(os.path.dirname(SCENE_SWITCH_FILE), exist_ok=True)
        with open(SCENE_SWITCH_FILE, "w", encoding="utf-8") as f:
            json.dump({SCENE_SWITCH_KEY: bool(on)}, f, ensure_ascii=False)
    except OSError:
        logger.error("现场记录开关存盘失败")


class _NoteDialog(MessageBoxBase):
    """写说明：哪里不对、期望怎样。必填。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(MARK_TEXT, self))
        intro = BodyLabel("自动化出了问题（卡住、点错、选错牌等）时用：把按下按钮前 50 秒、后 10 秒的画面、"
                          "识别结果和动作存成现场包，放在 battle_logs/现场，供事后排查。\n"
                          "按下按钮时就已经开始记了，写说明不耽误；点「保存现场」后再录到按下按钮后 10 秒，"
                          "点「取消」不存。", self)
        intro.setWordWrap(True)
        self.viewLayout.addWidget(intro)
        self.edit = PlainTextEdit(self)
        self.edit.setPlaceholderText("说明（必填）：哪里不对、本来应该怎样")
        self.edit.setMinimumSize(420, 120)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("保存现场")
        self.cancelButton.setText("取消")
        self.yesButton.setEnabled(False)
        self.edit.textChanged.connect(lambda: self.yesButton.setEnabled(bool(self.note())))
        self.edit.setFocus()

    def note(self):
        return self.edit.toPlainText().strip()
