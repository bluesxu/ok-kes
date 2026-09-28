# 测试用替身：只提供 speedup 依赖的接口，外部行为与 ok_tasks/utils.py 对应函数一致


class _SkipButton:
    name, x, y, width, height = "跳过", 2300, 1350, 120, 48


def _simplify_texts(texts):
    return texts


def find_box_at_point(task, rel_x, rel_y):
    """与真实实现相同：返回包含该点、面积最小的文字框。"""
    px, py = rel_x * task.width, rel_y * task.height
    hits = [b for b in task.all_texts
            if b.x <= px <= b.x + b.width and b.y <= py <= b.y + b.height]
    return min(hits, key=lambda b: b.area()) if hits else None


def handle_shop(task):
    """替身：记录被调用。task.marks['shop_click'] 有框名时点击该框，用来测刷新开关。"""
    task.marks["shop_original"] = task.marks.get("shop_original", 0) + 1
    name = task.marks.get("shop_click")
    if name:
        box = next((item for item in task.all_texts if item.name == name), None)
        if box is not None:
            task.click_box(box, after_sleep=0)
            return True
    return False


def handle_leave(task):
    """与真实实现相同的找按钮方式；测试里不真等 1 秒。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and "离开" in box.name:
        task.click_box(box, after_sleep=0)
        return True
    return False


def is_frame_stuck(task, stuck_threshold_seconds=30, change_threshold=0.08):
    task.stuck_samples = getattr(task, "stuck_samples", 0) + 1
    task._last_change_time = 0
    return False


def handle_stuck_log(task):
    is_frame_stuck(task, stuck_threshold_seconds=10)
    return False


def _get_card_list(task, key):
    value = task.config.get(key, [])
    return list(value) if isinstance(value, (list, tuple)) else []


def _scroll_card_page(task, x, y, amount, page, distance=0.25):
    """与真实实现（PC 分支）相同：移到选牌区域、滚动、等待 0.5 秒。"""
    task.move_relative(x, y)
    task.sleep(0.05)
    task.scroll_relative(x, y, amount)
    task.sleep(0.5)


DECK_FEATURES = {"attack_in_deck": "攻击", "skill_in_deck": "技能", "enhance_in_deck": "强化",
                 "hex_in_deck": "咒术", "hex_in_deck_tw": "诅咒"}


def _recognize_cards_by_features(task, region, page, feature_types, min_feature_distance, name_offsets,
                                 type_offsets, description_offsets, name_only_feature_thresholds=None,
                                 allow_empty_type_threshold=None):
    """最下面一行的描述在区域外：只有非咒术图标也允许“只凭卡名保留”时才识别得到（与实测一致）。"""
    _ = task.frame
    name_only = dict(name_only_feature_thresholds or {})
    world = task.executor.world
    world.recognize_log.append((page, sorted(name_only), __import__("time").time()))
    include_bottom = all(n in name_only for n in ("attack_in_deck", "skill_in_deck", "enhance_in_deck"))
    # 真实实现随后按金色边框标记已选中的卡（_mark_selected_card_by_gold_border）
    return [{"name": name, "index": index, "selected": index in world.selected_cards}
            for index, name in world.visible_deck(include_bottom)]


def recognize_cards_in_deck(task, region=(0.274, 0.108, 0.929, 0.874), page=""):
    """与真实实现相同的调用方式：原本只给咒术图标设了“只凭卡名保留”的阈值。"""
    return _recognize_cards_by_features(
        task=task, region=region, page=page, feature_types=DECK_FEATURES,
        min_feature_distance=0.138, name_offsets=(0, 0, 0, 0), type_offsets=(0, 0, 0, 0),
        description_offsets=(0, 0, 0, 0),
        name_only_feature_thresholds={"hex_in_deck": 0.90, "hex_in_deck_tw": 0.90},
        allow_empty_type_threshold=0.90,
    )


def select_card(task, card_names, count=1, action=""):
    """与真实实现的流程一致：逐页找目标 → 到底后删最底页咒术卡 → 仍不够就点“跳过”。
    删卡时每选中一张累加待移除计数，点“跳过”后清零（与真实实现相同）。"""
    page = f"select_card-{action}"
    selected = 0

    def pick(card):
        nonlocal selected
        task.click(0.5, 0.5)
        task.executor.world.selected_cards.add(card["index"])  # 游戏里选中的卡出现金色边框
        card["selected"] = True
        selected += 1
        if action == "移除":
            task._pending_removed_card_count = getattr(task, "_pending_removed_card_count", 0) + 1

    cards = recognize_cards_in_deck(task, page=page)
    while True:
        for card in cards:
            if (selected < count and not card["selected"]
                    and any(t in card["name"] or card["name"] in t for t in card_names)):
                pick(card)
        if selected >= count:
            return True
        if task.executor.world.deck_at_bottom():
            break
        _scroll_card_page(task, 0.251, 0.735, -3, page)
        cards = recognize_cards_in_deck(task, page=page)
    if action == "移除":
        for card in cards:
            if selected < count and not card["selected"] and card["name"].startswith("咒术"):
                pick(card)
        if selected >= count:
            return True
    task.click_box(_SkipButton())
    if action == "移除":
        task._pending_removed_card_count = 0
    return True
