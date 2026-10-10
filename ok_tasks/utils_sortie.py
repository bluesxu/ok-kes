from ok import TriggerTask

from utils import (
    _move_and_click, _simplify_texts, _edit_distance, _get_config_value, _get_card_list, _get_route_priority, _get_game_text,
    find_box_at_point, find_text, find_text_in_region, recognize_cards,
    _card_has_type_below, select_card, calculate_dominant_hue,
    log_credit, log_node_status, handle_battle_crash, handle_close_page, handle_refine_equipment_credit,
    handle_center_confirm, handle_dont_show_again, handle_settlement, handle_skip,
    handle_destiny_choice, handle_main_member_flash,
    handle_card_reward, handle_equipment,
    handle_select_card, handle_copy_member,
    handle_convert_card,
    handle_negotiation, handle_continue, handle_confirm, handle_enter,
    handle_event_task, handle_route_selection, handle_obtain_reward,
    handle_leave, handle_next_step, handle_select, handle_view_original,
    handle_close_button,
    handle_card_assign, handle_non_battle_page,
    handle_remove, handle_three_choice_card_remove, handle_flash, handle_reflash, handle_grant_flash, handle_copy, handle_convert, handle_equipment_recast, handle_weakness_info, handle_minimizemap,
    handle_held_cards_page,
    handle_stuck_log, is_button_active, _clean_match, has_pile_title,
    handle_shop, handle_expedition_result,
    handle_escape,
    _get_current_credit, _get_current_hp_percent, _get_region_text,
    _find_rest_feature, _wait_for_rest_confirm, _retry_rest_reading,
    # handle_stage_clear,
    _finish_only_first_layer,
    handle_auto_stop,
)
from utils_chaos import handle_archive_target_member

import re
import random
import time

import battle_log
import utils
import utils_battle


# ------------------------- 出击模式独有工具 -------------------------

def _get_member_priority(task: TriggerTask):
    """读取主战员优先级配置，返回列表；解析失败使用默认顺序。"""
    value = _get_config_value(task, '主战员优先级', ["尼娅", "麦格纳", "米卡", "卡修斯"])
    return list(value) if isinstance(value, (list, tuple)) else ["尼娅", "麦格纳", "米卡", "卡修斯"]


def _get_blacklisted_members(task: TriggerTask):
    """读取拉黑主战员列表，返回列表；解析失败使用默认值。"""
    value = _get_config_value(task, '拉黑主战员', ["黛安娜", "阿黛尔海特"])
    return list(value) if isinstance(value, (list, tuple)) else ["黛安娜", "阿黛尔海特"]


# 会合选主战员页面每张卡右下角的职能标签：核心（输出）、支援（抽牌）、保护（护盾）、治疗（奶妈）。
# 优先级里的角色没出现时，按这个顺序选职能；刷新时也至少留一个这类角色不刷
SURVIVAL_ROLES = ("保护", "治疗")
_MEMBER_ROLES = ("核心", "支援") + SURVIVAL_ROLES


def _get_battle_member_priority(task: TriggerTask):
    """读取出战主战员优先级配置，返回列表；解析失败使用默认顺序。"""
    value = _get_config_value(task, "出战主战员优先级", ["海德玛丽", "九", "力", "绯"])
    return list(value) if isinstance(value, (list, tuple)) else ["海德玛丽", "九", "力", "绯"]


def _card_key(text):
    table = str.maketrans("①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿⓵⓶⓷⓸⓹⓺⓻⓼⓽⓾０１２３４５６７８９",
                         "1234567890123456789012345678900123456789")
    text = text.translate(table)
    m = re.search(r"\d", text)
    return m.group(0) if m else None


def _is_card_name(name):
    """判断文本是否为卡牌名：排除类型标签。"""
    exclude_keywords = {"攻击", "强化", "技能", "咒术", "基础", "基本", "状态异常", "诅咒"}
    # 包含类型关键词的文本不是卡牌名
    if any(kw in name for kw in exclude_keywords):
        return False
    # 包含"攻"且长度<=3的文本不是卡牌名（如"X攻"、"基本攻"）
    if "攻" in name and len(name) <= 3:
        return False
    return True


def _hand_card_names(task: TriggerTask):
    """读取手牌区域内的卡牌名，排除按键和类型标签文本。"""
    # 下边界放到 0.90：手牌呈扇形排开，两侧和被压低的牌名会落到 0.83 以下（曾因此整手牌识别成 0 张）
    x1, y1, x2, y2 = 0.159, 0.683, 0.836, 0.900

    boxes = [b for b in task.all_texts
             if x1 <= (b.x + b.width / 2) / task.width <= x2
             and y1 <= (b.y + b.height / 2) / task.height <= y2]
    result = [b for b in boxes
              if not _card_key(b.name)
              and len(b.name.strip()) > 1
              and _is_card_name(b.name)]
    task.log_info(f"_hand_card_names 区域共{len(boxes)}个文本，过滤后剩{len(result)}个: {[b.name for b in result]}")
    return result


def _hand_cards(task: TriggerTask):
    """识别手牌，返回按位置排序的卡牌名与按键列表，按键缺失时根据手牌数和间距推断。"""
    # 按键：包含数字的
    keys = [(b.x / task.width, b.y / task.height, _card_key(b.name)) for b in task.all_texts if _card_key(b.name)]
    keys.sort(key=lambda x: x[0])

    # 卡牌名
    card_names = _hand_card_names(task)
    card_names.sort(key=lambda b: b.x)

    # 读取手牌数
    hand_count = _read_hand_count(task)

    # 配对：每个卡牌匹配其左上方最近的未使用按键
    used_keys = set()
    cards = []
    for name_box in card_names:
        cx = name_box.x / task.width
        left_x = (name_box.x) / task.width  # 使用左边缘坐标，避免文本长度影响间距判断
        cy = name_box.y / task.height

        candidates = []
        for kx, ky, k in keys:
            if k in used_keys:
                continue
            # 垂直：按键在卡牌名上方 0.03~0.06
            if not (cy - 0.06 <= ky <= cy - 0.03):
                continue
            # 水平：按键在卡牌名左方，距离不超过 0.025
            if not (cx - 0.025 <= kx <= cx + 0.01):
                continue
            candidates.append((kx, ky, k))

        if candidates:
            best = min(candidates, key=lambda x: cx - x[0])
            used_keys.add(best[2])
            cards.append({"name": name_box.name, "key": best[2], "x": cx, "left_x": left_x, "y": cy})
        else:
            cards.append({"name": name_box.name, "key": None, "x": cx, "left_x": left_x, "y": cy})

    # 推断缺失的按键：用最小相邻间距作为 expected_spacing 进行插值
    # 以最近的前一张已有按键的卡牌为基准推算，避免累积误差
    if len(cards) >= 2:
        sorted_cards = sorted(enumerate(cards), key=lambda x: x[1]["left_x"])
        # 计算相邻卡牌 left_x 的最小间距
        min_spacing = float('inf')
        for i in range(len(sorted_cards) - 1):
            spacing = sorted_cards[i + 1][1]["left_x"] - sorted_cards[i][1]["left_x"]
            if spacing < min_spacing:
                min_spacing = spacing
        expected_spacing = max(0.055, min_spacing)  # 从0.055和最小间距中取最大值，防止除零
        task.log_info(f"_hand_cards: 最小间距={min_spacing:.4f}")
        # 确保最左边的卡牌有按键，如果没有则分配1
        if sorted_cards[0][1]["key"] is None:
            sorted_cards[0][1]["key"] = "1"
            task.log_info(f"_hand_cards: 卡牌「{sorted_cards[0][1]['name']}」 left_x={sorted_cards[0][1]['left_x']:.4f} → 最左卡牌分配按键1")
        for i in range(1, len(sorted_cards)):
            idx, c = sorted_cards[i]
            if c["key"] is None:
                # 向前查找最近一张已有按键的卡牌
                for j in range(i - 1, -1, -1):
                    prev_idx, prev_c = sorted_cards[j]
                    if prev_c["key"] is not None:
                        offset = (c["left_x"] - prev_c["left_x"]) / expected_spacing
                        approx_key = int(prev_c["key"]) + round(offset)
                        if 1 <= approx_key <= 9:
                            c["key"] = str(approx_key)
                            task.log_info(f"_hand_cards: 卡牌「{c['name']}」 left_x={c['left_x']:.4f} 基于「{prev_c['name']}」(key={prev_c['key']}) offset={offset:.2f} → {c['key']}")
                        else:
                            task.log_info(f"_hand_cards: 卡牌「{c['name']}」 left_x={c['left_x']:.4f} 基于「{prev_c['name']}」(key={prev_c['key']}) offset={offset:.2f} → 推算={approx_key}超出1-9，不分配")
                        break
    elif len(cards) == 1:
        if cards[0]["key"] is None:
            cards[0]["key"] = "1"
            task.log_info(f"_hand_cards: 单张卡牌，分配按键1")

    task.log_info(f"_hand_cards: 识别到 {len(cards)} 张手牌: {[(c['name'], c['key']) for c in cards]}")
    return cards


def _try_all_card_keys(task: TriggerTask, count):
    """从当前手牌数向下尝试所有手牌按键，兜底处理按键漏识别或识别错误。
    手牌数为10时先发送0（对应第10张牌），再从9发送到1。"""
    task.log_info(f"_try_all_card_keys: 手牌数={count}")
    if count == 10:
        task.log_info("手牌数为10，先发送按键0")
        task.send_key("0")
        task.sleep(0.5)
        task.send_key("enter")
        task.sleep(1)
        start = 9
    else:
        start = min(count, 9)
    task.log_info(f"_try_all_card_keys: 发送按键 {start} 到 1")
    for index in range(start, 0, -1):
        task.send_key(str(index))
        task.sleep(0.5)
        task.send_key("enter")
        task.sleep(1)


def _read_hand_count(task: TriggerTask):
    """读取当前手牌数；OCR 误识别成三位数时只取后两位纠正。"""
    box = find_box_at_point(task, 0.509, 0.972)
    match = re.search(r"(\d+)/10", box.name) if box else None
    if not match:
        return None
    hand_count_text = match.group(1)
    if len(hand_count_text) >= 3:
        corrected = hand_count_text[-2:]
        task.log_info(f"手牌数 OCR 识别为{hand_count_text}，纠正为{corrected}")
        hand_count_text = corrected
    hand_count = int(hand_count_text)
    if hand_count > 10:
        corrected = hand_count % 100
        task.log_info(f"手牌数 OCR 识别超过10: {hand_count}，纠正为{corrected}")
        hand_count = corrected
    return min(hand_count, 10)


def _read_member_slots(task: TriggerTask):
    """根据等级和重新搜索文本动态读取会合主战员候选槽位。"""
    x1, y1, x2, y2 = 0.077, 0.572, 0.946, 0.871
    refresh_text = _get_game_text(task, "重新搜索")
    region_boxes = [
        box for box in task.all_texts
        if x1 <= (box.x + box.width / 2) / task.width <= x2
        and y1 <= (box.y + box.height / 2) / task.height <= y2
    ]
    level_boxes = sorted(
        (box for box in region_boxes if box.name.strip() == "等级"),
        key=lambda box: box.x,
    )
    refresh_boxes = [
        box for box in region_boxes
        if refresh_text in box.name
    ]
    role_boxes = [box for box in region_boxes if box.name.strip() in _MEMBER_ROLES]

    slots = []
    unused_refresh_boxes = list(refresh_boxes)
    for level_box in level_boxes:
        level_center_x = (level_box.x + level_box.width / 2) / task.width
        level_center_y = (level_box.y + level_box.height / 2) / task.height
        name_x = level_center_x + 0.188
        name_y = level_center_y + 0.042
        name_box = find_box_at_point(task, name_x, name_y)
        name = name_box.name if name_box else ""
        # 职能标签在名字正上方（名字和标签都靠卡片右边对齐）
        role_box = next((
            box for box in role_boxes
            if abs((box.x + box.width / 2) / task.width - name_x) < 0.06
            and 0.02 < name_y - (box.y + box.height / 2) / task.height < 0.08
        ), None)
        role = role_box.name.strip() if role_box else ""

        refresh_box = None
        if unused_refresh_boxes:
            refresh_box = min(
                unused_refresh_boxes,
                key=lambda box: abs(
                    (box.x + box.width / 2) / task.width - level_center_x
                ),
            )
            unused_refresh_boxes.remove(refresh_box)
        refresh_y = (
            (refresh_box.y + refresh_box.height / 2) / task.height
            if refresh_box else None
        )

        if name_box:
            task.log_info(
                f"_read_member_slots: 等级位置({level_center_x:.3f},{level_center_y:.3f}) "
                f"识别到名称=「{name}」，职能=「{role}」，重新搜索y={refresh_y}"
            )
        else:
            task.log_info(
                f"_read_member_slots: 等级位置({level_center_x:.3f},{level_center_y:.3f}) "
                f"未识别到主战员名称，重新搜索y={refresh_y}"
            )
        slots.append({
            "name": name,
            "x": name_x,
            "y": name_y,
            "refresh_y": refresh_y,
            "role": role,
        })
    return slots


def _battle_member_boxes(task: TriggerTask):
    """读取出战主战员列表里的可点击主战员名称文本。"""
    _excluded = {"主战员列表", "甄别主战员", "确认", "返回", "等级", "Q", "6", "支援",
                 "治愈", "守护", "核心", "60", "令", "√", "攻", "弘命", "炫心",
                 "详细信息", "配置", "同步", "全部", "``"}
    matched = []
    for box in task.all_texts:
        cx = (box.x + box.width / 2) / task.width
        cy = (box.y + box.height / 2) / task.height
        in_x = 0.100 <= cx <= 0.984
        in_y = 0.100 <= cy <= 0.892
        excluded = box.name in _excluded
        task.log_debug(f"_battle_member_boxes: name=「{box.name}」 cx={cx:.4f} cy={cy:.4f} "
                       f"in_x={in_x} in_y={in_y} excluded={excluded}")
        if in_x and in_y and not excluded:
            matched.append(box)
    return matched


def _confirm_battle_member_selection(task: TriggerTask):
    """出战主战员选择后，按确认按钮色相决定确认或返回。"""
    dominant_hue = calculate_dominant_hue(task, (0.901, 0.931, 0.911, 0.941))
    if dominant_hue != -1 and 7 <= dominant_hue <= 17:
        task.log_info(f"出战主战员确认按钮色相={dominant_hue}，点击确认")
        _move_and_click(task, 0.906, 0.936)
        task.sleep(2)
    else:
        task.log_info(f"出战主战员确认按钮色相={dominant_hue}，未激活，返回")
        _move_and_click(task, 0.044, 0.050)
    return True


# 主战员名称 → 模板特征名映射字典（用于OCR识别不到时的模板匹配兜底）
_MEMBER_FEATURE_MAP = {
    "九": "nine",
}


def _select_battle_member(task: TriggerTask, max_scrolls=5):
    """按出战主战员优先级选择列表角色；找不到配置角色则随机选择。"""
    priority = _get_battle_member_priority(task)
    task.log_info(f"出战主战员优先级配置: {priority}")
    for scroll_index in range(max_scrolls + 1):
        boxes = _battle_member_boxes(task)
        recognized_names = [box.name for box in boxes]
        task.log_info(f"第{scroll_index + 1}次扫描, OCR识别到{len(boxes)}个主战员:")
        for b in boxes:
            cx = (b.x + b.width / 2) / task.width
            cy = (b.y + b.height / 2) / task.height
            task.log_info(f"  box: name=「{b.name}」 cx={cx:.4f} cy={cy:.4f} x={b.x} y={b.y} w={b.width} h={b.height}")
        for name in priority:
            member = next(
                (box for box in boxes if name.strip() == box.name.strip()),
                None,
            )
            if member:
                cx = (member.x + member.width / 2) / task.width
                cy = (member.y + member.height / 2) / task.height
                task.log_info(f"出战主战员优先级匹配成功: 配置名「{name}」-> OCR名「{member.name}」 cx={cx:.4f} cy={cy:.4f} x={member.x} y={member.y} w={member.width} h={member.height}")
                task.click_box(member)
                task.sleep(0.5)
                return _confirm_battle_member_selection(task)
            else:
                # OCR 未匹配到，尝试用模板匹配兜底
                feature_name = _MEMBER_FEATURE_MAP.get(name)
                if feature_name and task.feature_exists(feature_name):
                    task.log_info(f"出战主战员OCR未匹配到「{name}」，尝试模板匹配 feature=「{feature_name}」")
                    search_box = task.box_of_screen(0.096, 0.097, 0.988, 0.896)
                    feature_box = task.find_one(feature_name=feature_name, box=search_box, threshold=0.5)
                    if feature_box:
                        cx = (feature_box.x + feature_box.width / 2) / task.width
                        cy = (feature_box.y + feature_box.height / 2) / task.height
                        task.log_info(f"出战主战员模板匹配成功: 配置名「{name}」 feature=「{feature_name}」 cx={cx:.4f} cy={cy:.4f}, 相似度={feature_box.confidence:.4f}")
                        task.click_box(feature_box)
                        task.sleep(0.5)
                        return _confirm_battle_member_selection(task)
                    else:
                        task.log_info(f"出战主战员模板匹配失败: 「{name}」(feature=「{feature_name}」) 未找到")
                else:
                    task.log_info(f"出战主战员优先级匹配失败: 「{name}」未在当前列出的{len(boxes)}个主战员中")
        if scroll_index < max_scrolls:
            task.log_info(f"第{scroll_index + 1}次未匹配到任何优先级角色, 向下滚动重试")
            if task.is_adb():
                task.log_info("ADB从(0.500, 0.700)滑动到(0.500, 0.300)，向下浏览主战员")
                task.swipe_relative(
                    0.5, 0.7, 0.5, 0.3, duration=0.4, settle_time=0.3
                )
            else:
                task.move_relative(0.5, 0.5)
                task.scroll_relative(0.5, 0.7, -3)
            task.sleep(0.5)
            task.all_texts = _simplify_texts(task.ocr())
    boxes = _battle_member_boxes(task)
    if not boxes:
        task.log_info("出战主战员列表为空，无法选择")
        return False
    member = random.choice(boxes)
    cx = (member.x + member.width / 2) / task.width
    cy = (member.y + member.height / 2) / task.height
    task.log_info(f"未找到配置中的出战主战员，随机选择「{member.name}」 cx={cx:.4f} cy={cy:.4f} x={member.x} y={member.y} w={member.width} h={member.height}")
    task.click_box(member)
    task.sleep(0.5)
    return _confirm_battle_member_selection(task)


# ------------------------- 出击模式独有页面处理函数 -------------------------

def handle_boss_selection(task: TriggerTask):
    """首领选择页面: 随机选择一个首领并确认。国服标题为「请选择在核心遇见的首领」，繁中服为「請選擇在核心遭遇的BOSS」。"""
    box = find_box_at_point(task, 0.484, 0.928)
    if not (box and re.search(r"请选择.*(遇见的首领|遭遇的\s*boss)", box.name, re.IGNORECASE)):
        return False
    bosses = []
    for x, y in [(0.358, 0.706), (0.641, 0.706)]:
        name_box = find_box_at_point(task, x, y)
        if name_box:
            bosses.append({"name": name_box.name, "x": x, "y": y})
    if not bosses:
        return False
    boss = random.choice(bosses)
    task.log_info(f"首领选择: 随机选择「{boss['name']}」")
    battle_log.record(task, "BOSS选择", bosses=[b["name"] for b in bosses], chosen=boss["name"], reason="随机")
    _move_and_click(task, boss["x"], boss["y"])
    task.sleep(1)
    # _move_and_click(task, 0.919, 0.930)
    return True


def handle_secret_enemy(task: TriggerTask):
    """战斗页面：发现特殊怪物时，随机拖动一张手牌到怪物中心。"""
    hand_count_box = find_box_at_point(task, 0.513, 0.975)
    if not (hand_count_box and re.search(r"\d+/\d+", hand_count_box.name)):
        return False

    search_box = task.box_of_screen(0.496, 0.154, 0.967, 0.636)
    secret_enemy = task.find_one(
        feature_name="secret_enemy",
        box=search_box,
        threshold=0.7,
    )
    if not secret_enemy:
        return False

    enemy_x = (secret_enemy.x + secret_enemy.width / 2) / task.width
    enemy_y = (secret_enemy.y + secret_enemy.height / 2) / task.height
    card_x = random.uniform(0.202, 0.795)
    card_y = random.uniform(0.726, 0.911)
    task.log_info(
        f"发现特殊怪物，从({card_x:.3f}, {card_y:.3f})拖动手牌到"
        f"怪物中心({enemy_x:.3f}, {enemy_y:.3f})，"
        f"相似度={secret_enemy.confidence:.4f}"
    )
    task.move_relative(card_x, card_y)
    task.swipe_relative(card_x, card_y, enemy_x, enemy_y, duration=0.5)
    return False


def handle_battle_page(task: TriggerTask):
    """战斗页面: 先按条件释放 Ego；否则交给 utils_battle 按费用、出牌优先级、预计扣血出一张牌或结束回合。"""

    hand_count = _read_hand_count(task)
    if hand_count is None:
        return False

    # 如果已到达最终boss节点，标记boss战状态
    if hasattr(task, 'node_status') and task.node_status.get('reach_final_boss', False)             and not task.node_status.get('final_boss_battle', False):
        task.node_status['final_boss_battle'] = True
        task.log_info("检测到最终boss战斗开始，final_boss_battle=True")

    finishturn_box = task.box_of_screen(0.844, 0.782, 0.998, 0.990)
    finish_turn_visible = bool(task.find_feature(feature_name="finishturn", box=finishturn_box))
    # Ego：我方回合、出牌之前，Boss/精英战放得起就放，其余战斗 EP 满格才放；只挑放得起的（见 utils_battle.use_ego）
    if utils_battle.use_ego(task, finish_turn_visible):
        return True
    # 出牌策略（按位置读手牌、读费用、预留防御、拖牌打目标、结束回合）见 utils_battle
    return utils_battle.play_turn(task, hand_count, finish_turn_visible)


def handle_get_card(task: TriggerTask):
    """获得卡牌页面: 按优先级选择卡牌。"""
    title = find_box_at_point(task, 0.502, 0.128)
    tip = find_box_at_point(task, 0.883, 0.131)
    if not (title and title.name == "获得卡牌"):
        # 标题和提示有高/低两套渲染位置，探针会落空（10/03 11:43 的选牌子页面 15 秒没人认领）：区域内按文字找
        title = find_text_in_region(
            task, lambda name: _clean_match(name, "获得卡牌"), (0.360, 0.040, 0.680, 0.180))
    if not (tip and re.search(r"请选择.*获得的卡牌", tip.name)):
        tip = find_text_in_region(
            task, lambda name: re.search(r"请选择.*获得的卡牌", name), (0.700, 0.040, 1.000, 0.200))
    if not (title and tip):
        return False
    cards = recognize_cards(task, page="获得卡牌页面")
    if not cards:
        task.log_info("获得卡牌: 未识别到卡牌类型特征，无法选择")
        return False

    priority = _get_config_value(task, "获得卡牌优先级", [])
    task.log_info(f"获得卡牌: 当前优先级配置: {priority}")
    for name in priority:
        task.log_info(f"获得卡牌: 检查优先级「{name}」是否在卡牌列表中")
        chosen = next((card for card in cards if name in card["name"]), None)
        if chosen:
            task.log_info(f"获得卡牌: 优先选择「{chosen['name']}」(匹配优先级「{name}」)")
            battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen=chosen["name"],
                              reason=f"获得卡牌优先级「{name}」")
            _move_and_click(task, chosen["x"], chosen["y"])
            task.sleep(0.5)
            _move_and_click(task, 0.912, 0.931)
            return True
    task.log_info("获得卡牌: 未命中任何优先级卡牌，跳过非优先级卡牌")
    battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen="跳过",
                      reason=f"未命中获得卡牌优先级 {priority}")
    _move_and_click(task, 0.749, 0.931)
    task.sleep(1)
    return True


def handle_draw_card_event(task: TriggerTask):
    """抽牌事件页面: 按获得卡牌优先级选择一张要手持的卡牌。"""
    title = find_box_at_point(task, 0.509, 0.108)
    prompt_pattern = _get_game_text(task, r"请选择.*手持的卡牌")
    if not (title and re.search(prompt_pattern, title.name)):
        return False
    x1, y1, x2, y2 = 0.028, 0.211, 0.938, 0.857
    cards = [
        box for box in task.all_texts
        if x1 <= (box.x + box.width / 2) / task.width <= x2
        and y1 <= (box.y + box.height / 2) / task.height <= y2
        and box.name.strip()
    ]
    if not cards:
        return False
    chosen = None
    for name in _get_config_value(task, "获得卡牌优先级", []):
        chosen = next((card for card in cards if name in card.name), None)
        if chosen:
            task.log_info(f"抽牌事件: 优先选择「{chosen.name}」")
            break
    if chosen is None:
        chosen = random.choice(cards)
        task.log_info(f"抽牌事件: 未命中优先级，随机选择「{chosen.name}」")
    task.click_box(chosen)
    task.sleep(1)
    # _move_and_click(task, 0.952, 0.933)
    return True


def handle_discard_hand_card(task: TriggerTask):
    """手牌中仍有可用卡牌提示: 点击丢弃手牌。"""
    box = find_box_at_point(task, 0.5, 0.356)
    if box and "手牌中仍有可用卡牌" in box.name:
        task.log_info("检测到手牌丢弃页面，点击丢弃")
        _move_and_click(task, 0.424, 0.500) #今日不再提示
        task.sleep(0.5)
        _move_and_click(task, 0.663, 0.607)
        return True
    return False


def handle_sortie_reward_settlement(task: TriggerTask):
    """出击模式奖励结算页面: 按配置领取奖励或关闭页面。"""
    title = find_box_at_point(task, 0.550, 0.068)
    if not (title and title.name == "结算"):
        return False
    reward_box = find_box_at_point(task, 0.848, 0.389)
    if reward_box and reward_box.name == "获得" and _get_config_value(task, "领取奖励", False):
        task.log_info("检测到出击模式奖励结算页面，领取奖励")
        task.click_box(reward_box)
        task.sleep(1)
        return True
    task.log_info("检测到出击模式奖励结算页面，关闭页面")
    if task.node_status.get('is_escaped', False):
        return False
    _mark_sortie_boss_cleared(task)
    return _finish_only_first_layer(task)


def _mark_sortie_boss_cleared(task: TriggerTask):
    """出击模式走到奖励结算页，说明本轮的首领已经打完，把它补记到通关层数上。
    正常路径是 reach_final_boss → 结算按钮让 pass_final_boss_count + 1，但 reach_final_boss 会因为
    工具重启（实跑 10/03 21:20 点完首领节点就重启，21:21 打完首领没退）、或首领节点被识别成普通节点
    而丢掉，只打第一层就不会退出（实跑 10/03 18:53、21:21 两轮都是到了结算页却没退）。
    只在「只打第一层」开启时补记，连打多层时不影响层数统计和初始节点判断。"""
    if not _get_config_value(task, '只打第一层', False):
        return
    if task.node_status.get('pass_final_boss_count', 0) > 0:
        return
    task.node_status['pass_final_boss_count'] = 1
    task.log_info("只打第一层：已到本轮奖励结算页，补记通关层数 1")


def handle_sortie_reward_claim(task: TriggerTask):
    """出击模式奖励领取页面: 按配置领取或放弃卡厄思战利品。"""
    title = find_box_at_point(task, 0.503, 0.335)
    if not (title and re.search(r"卡.*思战利品", title.name)):
        return False
    if _get_config_value(task, "领取奖励", False):
        task.log_info("检测到出击模式奖励领取页面，领取卡厄思战利品")
        _move_and_click(task, 0.567, 0.708)
        task.sleep(1)
        return True
    task.log_info("检测到出击模式奖励领取页面，放弃卡厄思战利品")
    _move_and_click(task, 0.355, 0.714)
    return True


def handle_battle_member_config(task: TriggerTask):
    """主战员配置页面: 区分出战主战员入口和确认进入入口。"""
    title = find_box_at_point(task, 0.130, 0.043)
    if not (title and _get_game_text(task, '主战员配置') in title.name):
        return False
    battle_member_hint = find_box_at_point(task, 0.188, 0.799)
    if not (battle_member_hint and battle_member_hint.name.strip()):
        task.log_info("检测到主战员配置页面: 当前处于出战主战员，点击出战主战员入口")
        _move_and_click(task, 0.315, 0.475)
        task.sleep(2)
        return True
    task.log_info("检测到主战员配置页面: 点击进入")
    _move_and_click(task, 0.719, 0.914)
    return True


def handle_battle_member_selection(task: TriggerTask):
    """出战主战员列表页面: 按配置优先级选择角色。"""
    title = find_box_at_point(task, 0.139, 0.044)
    right_hint = find_box_at_point(task, 0.562, 0.044)
    if not ((title and  _get_game_text(task, '主战员列表') in title.name) and  (right_hint and _get_game_text(task, '甄别主战员') in right_hint.name)):
        return False
    return _select_battle_member(task)


def _recruited_roles(task: TriggerTask):
    """本局会合已选到的队友职能（名字 → 保护/治疗），用于保持盾奶平衡。"""
    member_status = getattr(task, "member_status", None)
    roles = member_status.get("recruit_roles") if isinstance(member_status, dict) else None
    return roles if isinstance(roles, dict) else {}


def _survival_counts(task: TriggerTask):
    """本局已选队友里 保护/治疗 各几个。"""
    roles = _recruited_roles(task)
    return tuple(sum(1 for role in roles.values() if role == name) for name in SURVIVAL_ROLES)


def _survival_order(task: TriggerTask):
    """这一轮保护/治疗的选择顺序：本局已选的职能里少的优先，一样多优先保护。"""
    protect, heal = _survival_counts(task)
    return SURVIVAL_ROLES if protect <= heal else tuple(reversed(SURVIVAL_ROLES))


def _record_recruit_role(task: TriggerTask, slot):
    """记下会合选到的队友职能；同一个名字只记一次，页面停留导致的重复识别不会重复计数。"""
    role = (slot.get("role") or "").strip()
    name = (slot.get("name") or "").strip()
    member_status = getattr(task, "member_status", None)
    if role not in SURVIVAL_ROLES or not name or not isinstance(member_status, dict):
        return
    roles = member_status.setdefault("recruit_roles", {})
    if not isinstance(roles, dict):
        roles = member_status["recruit_roles"] = {}
    if name in roles:
        return
    roles[name] = role
    protect, heal = _survival_counts(task)
    task.log_info(f"主战员选择: {role}角色「{name}」加入，本局已选 保护x{protect}/治疗x{heal}")


def handle_member_selection(task: TriggerTask):
    """主战员选择页面: 优先选配置角色（跳过拉黑角色）；没有则点击名字下方按钮刷新一次，
    刷新时保留本轮优先的职能（本局已选队友里保护少就留保护、治疗少就留治疗，一样多留保护），
    刷新后仍没有配置角色就按同一顺序选保护/治疗，都没有才随机选（跳过拉黑角色）。"""
    prompt = find_box_at_point(task, 0.500, 0.931)
    if not (prompt and _get_game_text(task, '主战员') in prompt.name):
        return False
    priority = _get_member_priority(task)
    blacklisted = _get_blacklisted_members(task)
    order = _survival_order(task)
    task.log_info(f"主战员选择: 优先级={priority}, 拉黑列表={blacklisted}, 本轮优先{'/'.join(order)}")

    def not_blacklisted(slot):
        return not any(blk in slot["name"] for blk in blacklisted)

    def survival_slot(slots):
        """按本轮优先顺序找第一个保护/治疗角色。"""
        for role in order:
            slot = next((s for s in slots if s.get("role") == role and s["name"] and not_blacklisted(s)), None)
            if slot:
                return slot
        return None

    slots = _read_member_slots(task)
    chosen = None
    for name in priority:
        chosen = next((slot for slot in slots if name in slot["name"] and not_blacklisted(slot)), None)
        if chosen:
            task.log_info(f"主战员选择: 优先选择「{chosen['name']}」")
            break
    if chosen is None:
        keep = survival_slot(slots)
        if keep:
            task.log_info(f"主战员选择: 未找到优先角色，保留{keep['role']}角色「{keep['name']}」，刷新其余候选")
        else:
            task.log_info("主战员选择: 未找到优先角色或优先角色被拉黑，点击三个名字下方按钮刷新一次")
        for slot in slots:
            if slot is not keep and slot["refresh_y"] is not None:
                _move_and_click(task, slot["x"], slot["refresh_y"])
                task.sleep(1)
        task.sleep(1)
        task.all_texts = _simplify_texts(task.ocr())
        slots = _read_member_slots(task)
        for name in priority:
            chosen = next((slot for slot in slots if name in slot["name"] and not_blacklisted(slot)), None)
            if chosen:
                task.log_info(f"主战员选择: 刷新后选择「{chosen['name']}」")
                break
    if chosen is None:
        chosen = survival_slot(slots)
        if chosen:
            task.log_info(f"主战员选择: 没有优先角色，选择{chosen['role']}角色「{chosen['name']}」")
    if chosen is None:
        valid_slots = [slot for slot in slots if slot["name"] and not_blacklisted(slot)]
        if not valid_slots:
            valid_slots = [slot for slot in slots if slot["name"]]
            if not valid_slots:
                return False
            task.log_info("主战员选择: 所有候选都被拉黑，从全部候选中随机选择")
        chosen = random.choice(valid_slots)
        task.log_info(f"主战员选择: 未找到优先角色，随机选择「{chosen['name']}」")
    _record_recruit_role(task, chosen)
    _move_and_click(task, chosen["x"], chosen["y"])
    task.sleep(1)
    # _move_and_click(task, 0.884, 0.931)
    # task.sleep(0.5)
    # _move_and_click(task, 0.635, 0.639)
    # task.sleep(0.5)
    return True


def handle_rational_supply(task: TriggerTask):
    """补充理性页面: 确认按钮未激活时关闭领取奖励并放弃补充。"""
    # 在区域(0.438,0.101,0.561,0.250)内查找包含"补充理性"的文本
    x1, y1, x2, y2 = 0.438, 0.101, 0.561, 0.250
    title = next((b for b in task.all_texts
                  if x1 <= (b.x + b.width / 2) / task.width <= x2
                  and y1 <= (b.y + b.height / 2) / task.height <= y2
                  and "补充理性" in b.name), None)
    if not title:
        return False
    task.log_info("检测到补充理性页面")
    confirm_box = find_box_at_point(task, 0.664, 0.774)
    if confirm_box and _clean_match(confirm_box.name, "确认") and not is_button_active(task, confirm_box):
        task.log_info("确认按钮未激活，将领取奖励设置为False，点击放弃补充")
        task.config['领取奖励'] = False
        from ok.gui.Communicate import communicate
        communicate.task_list_updated.emit()
        _move_and_click(task, 0.352, 0.774)
        task.sleep(1)
        return True
    return False


def handle_ether_supply(task: TriggerTask):
    """以太补充页面: 提示用户手动补充以太。"""
    box = find_box_at_point(task, 0.502, 0.139)
    if box and box.name == _get_game_text(task, '以太补充'):
        task.log_info("检测到以太补充页面，请手动补充以太后再启动功能")
        _move_and_click(task, 0.347, 0.803)
        task.sleep(0.5)
        return True
    return False


_HAND_SELECT_TAGS = ("攻击", "强化", "技能", "咒术", "基础", "基本", "状态异常", "诅咒")


def _hand_select_candidates(task: TriggerTask):
    """本页可点的牌位：牌名框直接用；带类型标签的框（攻击/基本技能…）点标签上方的牌面
    ——牌名没读到的牌就靠这条定位（实跑 10/03 16:05：攻击牌的牌名框缺失、只读出「攻击」标签，
    候选里只剩技能牌「物质再生」，反复点它卡了 11 分钟）。返回 [(key, 描述, (x, y))]。"""
    out, seen = [], set()
    for b in task.all_texts:
        cx = (b.x + b.width / 2) / task.width
        cy = (b.y + b.height / 2) / task.height
        if not (0.116 <= cx <= 0.859 and 0.697 <= cy <= 0.878):
            continue
        name = b.name.strip()
        if len(name) <= 1 or name in ("确认", "返回", "跳过"):
            continue
        if re.search(r"\d+\s*/\s*\d+", name):  # 手牌数、血量这类「数字/数字」是 OCR 串进来的，不是卡牌
            continue
        if re.fullmatch(r"[A-Za-z]\d{1,2}", name):  # 「C2」「F1」这类是牌的按键提示，不是牌
            continue
        if any(kw in name for kw in _HAND_SELECT_TAGS):
            point, label = (cx, cy - 0.025), f"类型「{name}」"
        else:
            if "攻" in name and len(name) <= 3:
                continue
            point, label = (cx, cy), name
        key = (round(point[0] * 10), round(point[1] * 10))
        if key in seen:
            continue
        seen.add(key)
        out.append((key, label, point))
    return out


def handle_battle_hand_select(task: TriggerTask):
    """战斗中手牌选择页面: 检测到请选择卡牌文本且底部有手牌数，随机选择指定数量的卡牌。
    点过的牌没让页面推进（这页可能只允许选某个类型，比如只让选攻击牌）就换别的牌试：
    实跑 10/03 15:54、16:05 两次反复随机点到技能牌，卡了十几分钟。"""
    # 检测(0.5, 0.111)位置的提示文本
    prompt = find_box_at_point(task, 0.5, 0.111)
    if not prompt:
        task._hand_select_tried, task._hand_select_pending = set(), None  # 页面关了：重试记录清零
        return False
    m = re.search(r'请选择(?=.*卡牌).*?(\d+)张', prompt.name)
    if not m:
        return False

    # 检测(0.505, 0.971)是否有手牌数 x/10
    hand_box = find_box_at_point(task, 0.505, 0.971)
    if not (hand_box and re.search(r'\d+/10', hand_box.name)):
        return False

    need = int(m.group(1))
    tried = getattr(task, "_hand_select_tried", None)
    if tried is None:
        tried = task._hand_select_tried = set()
    pending = getattr(task, "_hand_select_pending", None)
    if pending:
        # 上一帧选了这几张、页面还停在这里：这些都不行，换别的试
        tried.update(pending)
        task._hand_select_pending = None
        task.log_info(f"上次选的牌没让页面推进，本次换别的（已试过 {len(tried)} 个位置）")
    task.log_info(f"检测到战斗中手牌选择页面，需选择{need}张卡牌，随机选择")

    selected, clicked = 0, []
    for _ in range(need):
        task.all_texts = _simplify_texts(task.ocr())
        candidates = _hand_select_candidates(task)
        fresh = [c for c in candidates if c[0] not in tried and c[0] not in clicked]
        if not fresh and candidates:
            tried.clear()  # 全都试过一遍：重来（到时候还不行只能靠卡住兜底收尾）
            fresh = [c for c in candidates if c[0] not in clicked]
        if not fresh:
            task.log_info("手牌区域未找到卡牌，随机在手牌区域内点击一个位置")
            rx = random.uniform(0.216, 0.759)
            ry = random.uniform(0.697, 0.878)
            _move_and_click(task, rx, ry)
            clicked.append((round(rx * 10), round(ry * 10)))
            selected += 1
            task.sleep(1)
            continue
        key, label, (px, py) = random.choice(fresh)
        clicked.append(key)
        task.log_info(f"选择手牌: {label}")
        _move_and_click(task, px, py)
        selected += 1
        task.sleep(1)

    if selected > 0:
        task._hand_select_pending = clicked
        task.log_info(f"已完成选择，点击确认")
        _move_and_click(task, 0.934, 0.883)
        task.sleep(1)
    return True


_PILE_CARD_TYPE = re.compile(r'(攻击|技能|强化|咒术)$')


def handle_battle_pile_select(task: TriggerTask):
    """战斗中从所有牌堆选牌的页面（左上「所有牌堆」，标题下方计数 0/1，右下「选择」）：点一张牌，选满后交给 handle_select。
    实跑 10/08 15:42 打出「暗黑凝聚」后弹出「请选择1张欲赋予的卡牌」，没人选牌，handle_select 在 0/1 时点了「选择」没反应，
    之后卡了 14 分钟。和手牌选择页同一个标题，区别是没有底部手牌数。优先攻击牌，点了计数没变就换下一张。
    「请选择最多3张想要移动至抽牌堆上方的卡牌」（左上「抽牌堆或坟墓」）同一个版式：「最多」可以一张不选，
    handle_select 看到「选择」亮着就点了，一张没选（用户 10/09 17:03 手动标记，现场包 20261009-170307，一局点了三次）。
    所以「最多」页也选，直到选满或所有牌都点过（牌堆里不足 N 张）才交给 handle_select。"""
    prompt = find_box_at_point(task, 0.5, 0.111)
    m = re.search(r'请选择(?=.*卡牌).*?(\d+)张', prompt.name) if prompt else None
    counter = find_box_at_point(task, 0.5, 0.168)
    c = re.search(r'(\d+)\s*/\s*(\d+)', counter.name) if counter else None
    if not (m and c and has_pile_title(task)):
        task._pile_select_tried, task._pile_select_pending = set(), None
        return False
    picked, need = int(c.group(1)), int(c.group(2))
    if picked >= need:
        return False  # 选满了，交给 handle_select 点「选择」

    tried = getattr(task, "_pile_select_tried", None)
    if tried is None:
        tried = task._pile_select_tried = set()
    pending = getattr(task, "_pile_select_pending", None)
    if pending:
        tried.add(pending)  # 上次点的牌没让计数增加：换别的
        task._pile_select_pending = None

    candidates = []
    for b in task.all_texts:
        name = b.name.strip()
        cx, cy = (b.x + b.width / 2) / task.width, (b.y + b.height / 2) / task.height
        if 0.2 <= cy <= 0.8 and _PILE_CARD_TYPE.search(name):
            key = (round(cx * 10), round(cy * 10))
            candidates.append((not name.endswith("攻击"), cy > 0.5, cx, key, name, (cx, cy + 0.06)))
    candidates.sort()
    fresh = [c for c in candidates if c[3] not in tried]
    if not fresh and candidates:
        if "最多" in prompt.name and picked > 0:
            return False  # 牌堆里的牌都点过了、还是没满（牌不足 N 张）：已选的这些就够了，交给 handle_select
        tried.clear()
        fresh = candidates
    task.log_info(f"检测到战斗中所有牌堆选牌页面（{picked}/{need}）: {prompt.name}")
    if not fresh:
        task.log_info("所有牌堆选牌页面没读到卡牌类型，点第一张牌的位置")
        _move_and_click(task, 0.12, 0.35)
        task.sleep(0.5)
        return True
    *_, key, name, (px, py) = fresh[0]
    task.log_info(f"所有牌堆选牌: 点「{name}」那张牌")
    _move_and_click(task, px, py)
    task._pile_select_pending = key
    task.sleep(0.5)
    return True


def handle_card_info_popup(task: TriggerTask):
    """确认卡牌资讯弹窗（带「使用卡牌 / 取消选择」按钮）: 取消选择关掉。
    这个弹窗盖住手牌区时手牌会识别成 0 张，没有它时曾经在同一个画面空转了近 10 小时。
    出牌时选中了牌没打出去，左下角也会出现「ENTER 使用卡牌 / ESC 取消选择」的按键提示，这时点文字没用：
    实跑中每 2 秒点一次「取消选择」、卡了 50 分钟。所以先按 ESC，还关不掉再轮流改点文字。"""
    if not any("使用卡牌" in b.name or "卡牌资讯" in b.name for b in task.all_texts):
        task._card_popup_tries = 0
        return False
    cancel = next((b for b in task.all_texts if "取消选择" in b.name), None)
    if cancel is None:
        task._card_popup_tries = 0
        return False
    tries = getattr(task, "_card_popup_tries", 0)
    task._card_popup_tries = tries + 1
    if tries % 2 == 0:
        task.log_info("检测到确认卡牌资讯弹窗，按 ESC 取消选择")
        task.send_key("esc")
    else:
        task.log_info("检测到确认卡牌资讯弹窗，按 ESC 没关掉，点击取消选择")
        task.click_box(cancel)
    task.sleep(1)
    return True


def handle_curiosity_activate(task: TriggerTask):
    """尼娅的好奇心发动页面: 按优先级选择要手持的卡牌（战斗相关页面，优先级高于战斗页面）。"""
    box = find_box_at_point(task, 0.499, 0.129)
    if box and _get_game_text(task, '请选择1张要手持的卡牌') in box.name:
        task.log_info("检测到尼娅的好奇心发动页面")
        priority = ["剑雨", "展开极光", "一缕光芒", "万众英雄"]
        cards = recognize_cards(task, page="尼娅的好奇心页面")
        chosen_card = None
        for pri_name in priority:
            for card in cards:
                if card["name"] and card["name"] in pri_name:
                    chosen_card = card
                    task.log_info(f"按优先级选择卡牌: {card['name']}")
                    break
            if chosen_card:
                break
        if not chosen_card and cards:
            chosen_card = random.choice(cards)
            task.log_info(f"未命中优先级，随机选择卡牌: {chosen_card['name']}")
        if chosen_card:
            _move_and_click(task, chosen_card["x"], chosen_card["y"])
            task.sleep(2)
            return True
    return False


def handle_extra_card_use(task: TriggerTask):
    """额外使用卡牌页面: 随机选择一张卡牌使用（战斗相关页面，优先级高于战斗页面）。"""
    box = find_box_at_point(task, 0.498, 0.131)
    if box and "请选择张要额外使用的卡牌" in box.name:
        task.log_info("检测到额外使用卡牌页面，随机选择")
        _move_and_click(task, *random.choice([(0.251, 0.546), (0.508, 0.518), (0.764, 0.525)]))
        task.sleep(2)
        return True
    return False


def handle_card_function_select(task: TriggerTask):
    """卡牌功能选择页面: 量子晶种预测选创造，小丑任务随机选任务（战斗相关页面，优先级高于战斗页面）。"""
    title = find_box_at_point(task, 0.499, 0.131)
    if not (title and "请选择功能" in title.name):
        return False
    task_positions = [(0.115, 0.286), (0.349, 0.289), (0.588, 0.290), (0.827, 0.287)]
    task_boxes = [find_box_at_point(task, x, y) for x, y in task_positions]
    if all(b and "任务" in b.name for b in task_boxes):
        chosen = random.choice(task_boxes)
        task.log_info(f"检测到小丑任务选择卡牌发动，随机选择一项任务")
        task.click_box(chosen)
        task.sleep(4)
        return True
    p1 = find_box_at_point(task, 0.214, 0.289)
    p2 = find_box_at_point(task, 0.470, 0.292)
    p3 = find_box_at_point(task, 0.722, 0.286)
    if p1 and p2 and p3 and "创造" in p1.name and "创造" in p2.name and "创造" in p3.name:
        task.log_info("检测到量子晶种预测卡牌页面，点击创造")
        _move_and_click(task, 0.722, 0.286)
        task.sleep(4)
        return True
    cards = recognize_cards(task, page="卡牌功能选择页面")
    if cards:
        chosen = random.choice(cards)
        task.log_info(
            f"卡牌功能选择兜底: 随机点击卡牌「{chosen['name']}」，"
            f"类型「{chosen['type'] or chosen['feature_type']}」"
        )
        _move_and_click(task, chosen["x"], chosen["y"])
        task.sleep(4)
        return True
    return False


def handle_return_to_draw_pile(task: TriggerTask):
    """选择手牌放回抽牌堆页面: 从左往右选择第一张（战斗相关页面，优先级高于战斗页面）。"""
    box = find_box_at_point(task, 0.484, 0.111)
    if not (box and re.search(r"请选择.*要移动至抽牌堆.*", box.name)):
        return False
    task.log_info("检测到选择手牌放回抽牌堆页面，从左往右选择第一张")
    cards = sorted(
        [b for b in task.all_texts
         if 0.116 <= (b.x + b.width / 2) / task.width <= 0.859
         and 0.697 <= (b.y + b.height / 2) / task.height <= 0.908
         and len(b.name.strip()) > 1
         and b.name not in ["确认", "返回", "跳过"]],
        key=lambda b: b.x
    )
    if not cards:
        task.log_info("未找到手牌")
        return False
    chosen = cards[0]
    task.log_info(f"返回抽牌堆页面触发选卡事件，点击「{chosen.name}」")
    task.click_box(chosen)
    task.sleep(1)
    # _move_and_click(task, 0.934, 0.883)
    # task.sleep(1)
    return True





_REST_OPTIONS_WAIT = 1.5  # 刚进休息区时最多等这么久让闪光选项的文字显示出来
_REST_CLICK_GAP = 4       # 点过闪光/休息后这么久内不再重复点


def handle_rest_sortie(task: TriggerTask):
    """出击模式休息页面: 包含休息和闪光两个功能，检测到对应条件分别处理。"""
    # 检测闪光区域 (0.788,0.463)-(0.870,0.594) 是否存在“闪光”和不大于30的数字
    flash_x1, flash_y1, flash_x2, flash_y2 = 0.788, 0.463, 0.870, 0.594
    has_flash_text = False
    flash_cost = None
    flash_box = None
    for b in task.all_texts:
        cx = (b.x + b.width / 2) / task.width
        cy = (b.y + b.height / 2) / task.height
        if flash_x1 <= cx <= flash_x2 and flash_y1 <= cy <= flash_y2:
            if "闪光" in b.name:
                has_flash_text = True
                flash_box = b
            costs = [int(number) for number in re.findall(r"\d+", b.name) if int(number) <= 30]
            if costs and flash_cost is None:
                flash_cost = costs[0]

    flash_feature = task.find_one(
        feature_name="flash_in_sortie_safezoom",
        box=task.box_of_screen(0.702, 0.347, 0.963, 0.713),
    )
    if flash_feature:
        task.log_info(
            f"检测到flash_in_sortie_safezoom特征，匹配置信度: "
            f"{flash_feature.confidence:.2%}"
        )

    rest_feature = _find_rest_feature(task)
    pending = hasattr(task, 'node_status') and task.node_status.get('flash_or_rest', False)
    now = time.time()
    if pending and (flash_feature or rest_feature):
        if now - getattr(task, "_rest_clicked_at", 0.0) < _REST_CLICK_GAP:
            return True  # 刚点过闪光/休息，页面还没切走：不重复点（实跑中等确认按钮超时后又点了一次）
        if now - getattr(task, "_rest_page_last", 0.0) > 5:
            task._rest_page_since = now  # 刚进休息区
        task._rest_page_last = now
        if flash_feature and not (has_flash_text and flash_cost is not None)                 and now - task._rest_page_since < _REST_OPTIONS_WAIT:
            # 页面刚切过来时闪光文字、费用常常还没显示，曾因此直接点了休息：等两个选项都显示出来再决定
            return True
    flash_wanted = not utils.flash_list_done(task)
    if pending and flash_feature and not flash_wanted and not task.node_status.get("flash_done_logged"):
        task.node_status["flash_done_logged"] = True
        task.log_info(f"闪光卡牌列表里的牌本局都已闪过或不在牌库（{task.node_status.get('flash_done_cards')}），不闪光，改为休息")

    if flash_wanted and flash_feature and has_flash_text and flash_cost is not None and flash_box and pending:
        task.log_info("休息区存在可闪光选项")

        # 获取当前信用点
        credit = _get_current_credit(task)
        task.log_info(f"当前信用点: {credit}")

        # 获取当前生命值百分比。刚进入休息区时生命值、信用点常常还没显示，读不到就等下一帧重读；
        # 多帧仍读不到生命值时按 0% 处理（选择休息）：原来按 100% 处理，低血量时也会闪光
        hp_percent = _get_current_hp_percent(task)
        if hp_percent is False or credit == 0:
            if _retry_rest_reading(task, "生命值" if hp_percent is False else "信用点"):
                return True
        task._rest_read_retries = 0
        if hp_percent is False:
            hp_percent = 0

        flash_threshold_str = _get_config_value(task, '生命值大于多少优先闪光(百分比)', "60")
        try:
            flash_threshold = int(flash_threshold_str)
        except (ValueError, TypeError):
            flash_threshold = 60
        task.log_info(f"生命值={hp_percent}%, 阈值={flash_threshold}%, 信用点={credit}")
        if credit > flash_cost and hp_percent >= flash_threshold:
            task.log_info("满足闪光条件，点击闪光")
            battle_log.record(task, "休息区", choice="闪光", hp=utils._get_current_hp(task), credit=credit,
                              hp_percent=hp_percent, flash_threshold=flash_threshold, flash_cost=flash_cost)
            task._rest_clicked_at = time.time()
            task.click_box(flash_box)
            if not _wait_for_rest_confirm(task):
                return True
            task.node_status['flash_or_rest'] = False
            return True
        else:
            task.log_info("不满足闪光条件，继续检测休息")

    free_text = _get_region_text(task, (0.154, 0.602, 0.359, 0.847))
    if (rest_feature and "免费" in free_text and hasattr(task, 'node_status')
            and task.node_status.get('flash_or_rest', False)):
        task.log_info("检测到休息界面，点击休息")
        battle_log.record(task, "休息区", choice="休息", hp=utils._get_current_hp(task),
                          credit=utils._get_current_credit(task))
        task._rest_clicked_at = time.time()
        task.click_box(rest_feature)
        if not _wait_for_rest_confirm(task):
            return True
        task.node_status['flash_or_rest'] = False
        return True

    if rest_feature and "免费" not in free_text:
        task.log_info("检测到rest特征，但休息区域未找到「免费」，跳过点击休息")

    # 检测是否需要进入德朗商店
    shop_box = find_box_at_point(task, 0.360, 0.138)
    if shop_box and "德朗商店" in shop_box.name and hasattr(task, 'node_status') and task.node_status.get('shop', False):
        task.log_info("检测到德朗商店，且 node_status['shop']=True，进入商店")
        task.click_box(shop_box)
        task.sleep(2)
        return True
    return False


# 剧情对话停在同一句多久没变就按空格翻页
_DIALOG_STALL_SECONDS = 2.5


def handle_event_dialog(task: TriggerTask):
    """事件里的剧情对话: 右下角有 SPACE、右上角有 ALT。
    游戏的自动对话被关掉时对话会一直停住，同一句停留超过 2.5 秒就后台按一次空格翻页；
    不去点右上角的自动按钮，它有多个挡位，点了反而可能关掉。"""
    space = [b for b in task.all_texts
             if b.name.strip().upper() == "SPACE"
             and b.x > task.width * 0.85 and b.y > task.height * 0.85]
    alt = [b for b in task.all_texts
           if b.name.strip().upper() == "ALT"
           and b.x > task.width * 0.85 and b.y < task.height * 0.12]
    if not space or not alt:
        task._dialog_state = None
        return False
    lines = tuple(b.name for b in task.all_texts
                  if task.height * 0.6 < b.y < task.height * 0.85)
    now = time.time()
    state = getattr(task, "_dialog_state", None)
    if not state or state[0] != lines:
        task._dialog_state = (lines, now)
        return False
    if now - state[1] < _DIALOG_STALL_SECONDS:
        return False
    task.log_info(f"剧情对话停了 {now - state[1]:.1f} 秒没翻页，按空格继续: {'/'.join(lines)[:40]}")
    task.send_key("space")
    task._dialog_state = (lines, time.time())
    return True


# 出击模式 PAGE_HANDLERS
PAGE_HANDLERS = [
    handle_auto_stop,
    handle_route_selection,
    # handle_stage_clear,
    log_credit,
    log_node_status,
    handle_archive_target_member, #信息统计页面，避免出击模式卡住；排在卡住兜底前面，否则误报卡住时会被先点右上角关闭
    handle_stuck_log,
    handle_event_dialog, #事件剧情对话停住时按空格翻页
    handle_close_page, #点击屏幕关闭页面，优先于其他普通页面处理

    handle_ether_supply,
    handle_refine_equipment_credit, #提炼装备信用点页面，优先于确认按钮
    handle_dont_show_again, #带「今天不再显示」的确认框：先勾上再确认，优先于页面中央确认
    handle_center_confirm,
    handle_equipment, #装备选择
    handle_card_assign,
    handle_confirm, #确认按钮
    handle_convert, #转换按钮
    handle_shop, #德朗商店
    handle_rest_sortie, #休息/商店入口
    handle_close_button, #关闭按钮
    handle_remove, #移除按钮
    handle_three_choice_card_remove, #三选一卡牌移除页面（低优先级兜底）
    handle_flash, #闪光按钮
    handle_reflash, #重新闪光按钮
    handle_grant_flash, #赋予闪光按钮
    handle_copy, #复制按钮
    handle_leave, #离开按钮
    handle_expedition_result, #探险结果页面，优先级高于下一步
    handle_next_step, #下一步按钮
    handle_battle_pile_select, #战斗中所有牌堆选牌，没选满前不让 handle_select 点「选择」
    handle_select, #选择按钮

    handle_equipment_recast, #装备重铸按钮

    handle_non_battle_page,
    handle_battle_crash,
    handle_discard_hand_card,
    handle_card_info_popup, #确认卡牌资讯弹窗，盖住手牌区时先关掉
    handle_battle_hand_select,
    handle_curiosity_activate,
    handle_extra_card_use,
    handle_card_function_select,
    handle_return_to_draw_pile,
    handle_weakness_info,
    handle_battle_page,
    handle_settlement,
    handle_destiny_choice,
    handle_main_member_flash,
    handle_boss_selection,
    handle_get_card,
    handle_card_reward,
    handle_draw_card_event,
    # handle_equipment,
    # handle_mask_card,
    handle_select_card,
    handle_copy_member,
    handle_convert_card,
    handle_negotiation,
    handle_sortie_reward_settlement,
    handle_sortie_reward_claim,
    handle_continue,
    handle_battle_member_selection,
    handle_member_selection,
    handle_battle_member_config,
    handle_enter,
    handle_obtain_reward,
    handle_view_original,
    handle_skip,
    handle_event_task,
    handle_rational_supply,
    handle_weakness_info,
    handle_minimizemap,
    handle_held_cards_page,
    handle_escape,
    utils.log_unhandled_page, #放最后：连续 10 秒没有处理函数认领画面时记「未识别页面」
]
