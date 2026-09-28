# 测试用替身：出击模式的页面处理函数列表和出牌时序，与 ok_tasks/utils_sortie.py 中同名函数一致
import time

PAGE_HANDLERS = []


def handle_battle_page(task):
    """与真实实现相同的两个分支：手牌里有牌时出牌（按数字键 → 等 1 秒 → 回车 → 等 2 秒）；
    没识别到手牌、且看到「结束回合」按钮时按 E（→ 等 1 秒）。"""
    names = [b.name for b in task.all_texts]
    if not any("/10" in name for name in names):
        return False
    if "手牌" in names:
        task.marks.setdefault("plays", []).append(time.time())
        task.send_key("4")
        task.sleep(1)
        task.send_key("enter")
        task.sleep(2)
    elif "结束回合" in names:
        task.marks.setdefault("end_turn_checks", []).append(time.time())
        task.send_key("e")
        task.sleep(1)
    return True


def _try_all_card_keys(task, count):
    """与真实实现相同：从手牌数倒着按一遍，每张牌 0.5 + 1 秒。"""
    for index in range(min(count, 9), 0, -1):
        task.send_key(str(index))
        task.sleep(0.5)
        task.send_key("enter")
        task.sleep(1)
