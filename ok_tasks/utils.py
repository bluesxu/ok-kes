from ok import Box, TriggerTask

import re
import random
import time
import sys
import cv2
import os
import numpy as np
from opencc import OpenCC

import battle_log

_jp2t = OpenCC('jp2t')  # 日文新字体转繁体
_t2s = OpenCC('t2s')  # 繁体转简体

def _normalize_text(text):
    """先将日文汉字字形转繁体，再统一转换为简体。"""
    return _t2s.convert(_jp2t.convert(text))


def _edit_distance(s1, s2, max_dist=1):
    """计算两个字符串的编辑距离是否 <= max_dist。"""
    if abs(len(s1) - len(s2)) > max_dist:
        return False
    if not s1 or not s2:
        return max(len(s1), len(s2)) <= max_dist
    m, n = len(s1), len(s2)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n] <= max_dist


def is_subsequence(first: str, second: str) -> bool:
    """判断第一个字符串是否为第二个字符串的子序列。"""
    second_iter = iter(second)
    return all(char in second_iter for char in first)


def _parse_flash_rules(rules):
    """把「牌名:关键词」或「关键词」写法的规则列表解析成 [(规则原文, 牌名或 None, 关键词)]，带牌名的排在前面。"""
    per_card, common = [], []
    for rule in rules:
        if not isinstance(rule, str) or not re.sub(r"\s+", "", rule):
            continue
        rule = re.sub(r"\s+", "", rule)
        parts = re.split(r"[:：]", rule, maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            per_card.append((rule, parts[0], parts[1]))
        else:
            common.append((rule, None, rule))
    return per_card + common


def _flash_rules(task: TriggerTask):
    """「闪光优先级」规则，写成「牌名:关键词」的只对这张牌生效、排在前面，其余是对所有牌生效的关键词。
    返回 [(规则原文, 牌名或 None, 关键词)]，保持各自在列表里的先后顺序。"""
    return _parse_flash_rules(_get_card_list(task, "闪光优先级"))


def _flash_blacklist_rules(task: TriggerTask):
    """「拉黑卡牌」规则：闪光三选一页里命中这些规则的版本不选，写法和「闪光优先级」一样。"""
    return _parse_flash_rules(_get_card_list(task, "拉黑卡牌"))


def _flash_rule_matches(rule, card):
    """一条闪光规则是否命中这张牌。OCR 会把描述的语序打乱、夹杂杂字，关键词按「字依次出现」比对；
    带牌名的规则先要求牌名对得上（互相包含，OCR 常漏读牌名的一两个字）。"""
    _, name, keyword = rule
    card_name = card["name"].strip()
    if name is not None:
        if not card_name or not (name in card_name or card_name in name):
            return False
        return is_subsequence(keyword, card["description"])
    return is_subsequence(keyword, card["name"] + "：:" + card["description"])


def _move_and_click(task: TriggerTask, x, y):
    """先将鼠标移动到目标位置，等待界面响应后再点击。"""
    page_handler = sys._getframe(1).f_code.co_name
    task.log_info(
        f"页面处理「{page_handler}」触发点击事件，点击目标坐标=({x:.3f}, {y:.3f})"
    )
    task.move_relative(x, y)
    task.sleep(0.5)
    task.click(x, y)


def _simplify_texts(texts):
    """将OCR结果按 jp2t → t2s 批量转换为简体（原地修改）。"""
    for b in texts:
        b.name = _normalize_text(b.name)
    return texts


def _get_config_value(task: TriggerTask, key, default):
    """读取运行时配置，优先从 task.config 读取，其次 default_config，最后使用默认值。返回前将字符串转简体。"""
    if hasattr(task, 'config') and key in task.config:
        value = task.config[key]
    else:
        value = getattr(task, 'default_config', {}).get(key, default)
    if isinstance(value, str):
        value = _normalize_text(value).strip()
    elif isinstance(value, (list, tuple)):
        value = [
            _normalize_text(v).strip() if isinstance(v, str) else v
            for v in value
        ]
    return value


def _get_card_list(task: TriggerTask, key):
    """读取列表配置，解析失败返回空列表。"""
    value = _get_config_value(task, key, [])
    return list(value) if isinstance(value, (list, tuple)) else []


def _get_card_reward_priority(task: TriggerTask):
    """读取卡牌奖励优先级，并将刷初始卡牌配置置于最高优先级。"""
    priority = _get_card_list(task, "卡牌奖励优先级")
    initial_card_name = _get_config_value(task, "刷初始卡牌", "")
    initial_card_name = initial_card_name.strip() if isinstance(initial_card_name, str) else ""
    if initial_card_name:
        priority = [
            initial_card_name,
            *(name for name in priority if name != initial_card_name),
        ]
    return priority


# 游戏语言 → 映射文件路径
_GAME_LANG_FILE_MAP = {
    "繁体中文": os.path.join(os.path.dirname(__file__), 'assets', 'game_text_map', 'zh_tw.py'),
}
# 已加载的映射缓存 {语言: SERVER_TEXT_MAP字典}
_LOADED_MAPS = {}


def _load_game_text_map(game_lang):
    """加载指定语言的映射表（带缓存）。"""
    if game_lang not in _LOADED_MAPS:
        file_path = _GAME_LANG_FILE_MAP.get(game_lang)
        if file_path and os.path.exists(file_path):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(f"_game_map_{game_lang}", file_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _LOADED_MAPS[game_lang] = getattr(mod, 'SERVER_TEXT_MAP', {})
            except Exception:
                _LOADED_MAPS[game_lang] = {}
        else:
            _LOADED_MAPS[game_lang] = {}
    return _LOADED_MAPS[game_lang]


def _get_game_language(task: TriggerTask):
    """获取当前模式配置的游戏语言。"""
    try:
        return str(task.config.get('游戏语言', '简体中文')).strip() or '简体中文'
    except Exception:
        return '简体中文'


def _get_game_text(task: TriggerTask, default_text):
    """根据当前模式配置的游戏语言，返回对应服务器版本的搜索文本。"""
    game_lang = _get_game_language(task)

    if game_lang == '简体中文':
        return default_text

    mapping = _load_game_text_map(game_lang)
    return mapping.get(default_text, default_text)


def _migrate_route_boss_to_elite(task: TriggerTask):
    """迁移用户配置中"路线优先级"的"boss"为"精英"（兼容旧配置）。"""
    try:
        if hasattr(task, 'config') and '路线优先级' in task.config:
            priority = task.config['路线优先级']
            if isinstance(priority, (list, tuple)):
                new_priority = ["精英" if v == "boss" else v for v in priority]
                if new_priority != list(priority):
                    task.config['路线优先级'] = new_priority
                    from ok.gui.Communicate import communicate
                    communicate.task_list_updated.emit()
                    task.log_info(f"迁移路线优先级配置: boss→精英 {new_priority}")
    except Exception:
        pass


def _get_route_priority(task: TriggerTask):
    """读取路线节点优先级配置，返回列表；解析失败使用默认顺序。"""
    value = _get_config_value(task, '路线优先级', ["休息", "事件", "小怪", "精英"])
    return list(value) if isinstance(value, (list, tuple)) else ["休息", "事件", "小怪", "精英"]


# ------------------------- 通用工具 -------------------------

def _get_current_credit(task: TriggerTask):
    """读取当前信用点数，从两个可能位置取最大值。"""
    credit = 0
    for pos_x, pos_y in [(0.794, 0.054), (0.734, 0.053)]:
        box = find_box_at_point(task, pos_x, pos_y)
        if box and box.name.isdigit():
            val = int(box.name)
            if val > credit:
                credit = val
    if credit > 0:
        task._read_credit = credit  # 过程账只用这一帧已经读到的数，不再为记账重读
    return credit


def _acceleration_on(task: TriggerTask):
    """加速模式开着才改等待。配置里没有这一项时保持原时序（测试和未装补丁的任务）。"""
    config = getattr(task, "config", None)
    if not isinstance(config, dict):
        return False
    return bool(config.get("加速模式", False))


def _neutral_card_limit(task: TriggerTask):
    """卡厄思模式每局最多允许在商店购买3张中立牌。"""
    return 3 if task.name == "自动卡厄思模式" else None


def _neutral_card_limit_reached(task: TriggerTask):
    """判断本局获得的中立牌是否已达到固定上限。"""
    limit = _neutral_card_limit(task)
    if limit is None:
        return False
    acquired = getattr(task, "node_status", {}).get("neutral_card_count", 0)
    return acquired >= limit


def _record_removed_cards(task: TriggerTask, count=1):
    """记录本局实际完成移除的卡牌数量。"""
    node_status = getattr(task, "node_status", None)
    if not isinstance(node_status, dict):
        return
    count = max(1, int(count))
    node_status["removed_card_count"] = (
        node_status.get("removed_card_count", 0) + count
    )
    task.log_info(
        f"本局移除卡牌数量增加{count}，"
        f"当前共{node_status['removed_card_count']}张"
    )


def _record_neutral_card(task: TriggerTask):
    """记录本局实际获得一张中立牌。"""
    node_status = getattr(task, "node_status", None)
    if not isinstance(node_status, dict):
        return
    node_status["neutral_card_count"] = (
        node_status.get("neutral_card_count", 0) + 1
    )
    task.log_info(
        f"本局获得中立牌数量增加1，"
        f"当前共{node_status['neutral_card_count']}张"
    )


def _parse_discounted_price(price_text):
    """解析可能将折扣前后价格连在一起的 OCR 数字。"""
    price_text = price_text.strip()
    if not re.fullmatch(r"\d+", price_text):
        return None
    if len(price_text) == 6:
        return int(price_text[-3:])
    if len(price_text) in (4, 5):
        return int(price_text[-2:])
    return int(price_text)


def _get_current_hp(task: TriggerTask):
    """读取当前生命值 (当前, 上限)，读不到返回 None。"""
    hp_box = find_box_at_point(task, 0.209, 0.040)
    hp_match = hp_box and re.search(r'(\d+)/(\d+)', hp_box.name)
    if not hp_match or int(hp_match.group(2)) <= 0:
        return None
    hp = int(hp_match.group(1)), int(hp_match.group(2))
    task._read_hp = hp  # 过程账只用这一帧已经读到的数，不再为记账重读
    return hp


def _get_current_hp_percent(task: TriggerTask):
    """读取当前生命值百分比，无法识别时返回 False。"""
    hp_box = find_box_at_point(task, 0.209, 0.040)
    if not hp_box:
        return False
    hp_match = re.search(r'(\d+)/(\d+)', hp_box.name)
    if not hp_match:
        return False
    current_hp = int(hp_match.group(1))
    max_hp = int(hp_match.group(2))
    if max_hp <= 0:
        return False
    hp_percent = int(current_hp * 100 / max_hp)
    task.log_info(f"当前生命值: {current_hp}/{max_hp} = {hp_percent}%")
    return hp_percent


def find_box_at_point(task: TriggerTask, rel_x, rel_y):
    """查找包含相对坐标点的 box，多个命中时返回面积最小的（最精确）。
    一个都没命中时，检查该点是否落在被 OCR 切开的同一段文字之间，是则返回合并后的框。"""
    px, py = rel_x * task.width, rel_y * task.height
    hits = [b for b in task.all_texts
            if b.x <= px <= b.x + b.width and b.y <= py <= b.y + b.height]
    if hits:
        return min(hits, key=lambda b: b.area())
    return _merge_split_texts_at_point(task, px, py)


# 同一行相邻文字框的最大间隔（相对屏幕宽度），超过这个距离视为两段不同的文字
_SPLIT_TEXT_MAX_GAP = 0.02


def _merge_split_texts_at_point(task: TriggerTask, px, py):
    """OCR 有时把一个按钮的文字切成两个框，例如繁中服的「賦予靈光一閃」被切成「賦予靈光-」和「一閃」，
    按钮检测点 (0.945, 0.918) 正好落在两框之间，按钮找不到，选完卡后一直停在选卡页。
    这里把 (px, py) 所在行里间隔小于 _SPLIT_TEXT_MAX_GAP 的相邻文字框合并成一个框；
    合并后仍不覆盖该点时返回 None。名称去掉切开处多出来的符号（规则与 _clean_match 相同）。"""
    gap = _SPLIT_TEXT_MAX_GAP * task.width
    line = sorted(
        (b for b in task.all_texts if b.name.strip() and b.y <= py <= b.y + b.height),
        key=lambda b: b.x,
    )

    def covers(run):
        return run[0].x <= px <= max(b.x + b.width for b in run)

    run = []
    for box in line:
        if run and box.x - max(b.x + b.width for b in run) > gap:
            if covers(run):
                break
            run = []
        run.append(box)
    if len(run) < 2 or not covers(run):
        return None
    name = re.sub(r'[^一-鿿\w]', '', "".join(b.name.strip() for b in run))
    return Box(
        min(b.x for b in run),
        min(b.y for b in run),
        to_x=max(b.x + b.width for b in run),
        to_y=max(b.y + b.height for b in run),
        confidence=min(b.confidence for b in run),
        name=name,
    )


# 底部按钮条：结算/奖励类页面的按钮文字有高/低两套渲染位置（条目数不同时上下浮动约 45px，
# 见 10/03 命运结算页卡死），固定检测点会落空。按钮检测一律先固定点、落空再在这里按文字找。
_BOTTOM_BUTTON_REGION = (0.600, 0.820, 1.000, 0.990)


def find_text_in_region(task: TriggerTask, matcher, region):
    """在区域内找第一个 matcher(name) 命中的文字框，多个命中返回面积最小的（最精确）。"""
    x1, y1, x2, y2 = region
    hits = [
        box for box in task.all_texts
        if x1 <= (box.x + box.width / 2) / task.width <= x2
        and y1 <= (box.y + box.height / 2) / task.height <= y2
        and matcher(box.name)
    ]
    if not hits:
        return None
    return min(hits, key=lambda box: box.area())


def find_button_by_text(task: TriggerTask, words, region=_BOTTOM_BUTTON_REGION):
    """在区域内按按钮文字精确查找（_clean_match，去符号后完全相等）；不做包含匹配——
    「获得」不能匹配到「获得卡牌」。"""
    return find_text_in_region(
        task, lambda name: any(_clean_match(name, word) for word in words), region
    )


def find_target_card(task: TriggerTask):
    """查找target卡牌特征，返回特征框列表及其对应的相对点击位置。"""
    search_box = task.box_of_screen(0.090, 0.179, 0.927, 0.342)
    target_boxes = task.find_feature(feature_name="target", box=search_box) or []
    click_positions = []
    for target_box in target_boxes:
        center_x = (target_box.x + target_box.width / 2) / task.width
        center_y = (target_box.y + target_box.height / 2) / task.height
        click_positions.append((
            min(1.0, max(0.0, center_x - 0.0975)),
            min(1.0, max(0.0, center_y + 0.2460)),
        ))
    return target_boxes, click_positions


# 牌名框里如果读到的只是类型字，说明没框到牌名
_CARD_TYPE_WORDS = {"攻击", "技能", "强化", "异能", "诅咒", "状态"}


def _recognize_cards_by_features(
    task: TriggerTask,
    region,
    page,
    feature_types,
    min_feature_distance,
    name_offsets,
    type_offsets,
    description_offsets,
    name_only_feature_thresholds=None,
    allow_empty_type_threshold=None,
):
    """按指定特征和相对位置识别卡牌。"""
    search_box = task.box_of_screen(*region)
    feature_candidates = []
    for feature_name, feature_type in feature_types.items():
        feature_boxes = task.find_feature(
            feature_name=feature_name,
            box=search_box,
            threshold=0.65,
        ) or []
        for feature_box in feature_boxes:
            feature_candidates.append((feature_name, feature_type, feature_box))

    def feature_distance(first, second):
        first_x = (first.x + first.width / 2) / task.width
        first_y = (first.y + first.height / 2) / task.height
        second_x = (second.x + second.width / 2) / task.width
        second_y = (second.y + second.height / 2) / task.height
        return (
            (first_x - second_x) ** 2 + (first_y - second_y) ** 2
        ) ** 0.5

    filtered_features = []
    for candidate in sorted(
        feature_candidates,
        key=lambda item: item[2].confidence,
        reverse=True,
    ):
        if any(
            feature_distance(candidate[2], kept[2]) < min_feature_distance
            for kept in filtered_features
        ):
            continue
        filtered_features.append(candidate)

    cards = []
    log_prefix = f"{page}: " if page else ""
    for feature_name, feature_type, feature_box in filtered_features:
        center_x = (feature_box.x + feature_box.width / 2) / task.width
        center_y = (feature_box.y + feature_box.height / 2) / task.height
        name_region = (
            max(0.0, center_x + name_offsets[0]),
            max(0.0, center_y + name_offsets[1]),
            min(1.0, center_x + name_offsets[2]),
            min(1.0, center_y + name_offsets[3]),
        )
        type_region = (
            max(0.0, center_x + type_offsets[0]),
            max(0.0, center_y + type_offsets[1]),
            min(1.0, center_x + type_offsets[2]),
            min(1.0, center_y + type_offsets[3]),
        )
        desc_region = (
            max(0.0, center_x + description_offsets[0]),
            max(0.0, center_y + description_offsets[1]),
            min(1.0, center_x + description_offsets[2]),
            min(1.0, center_y + description_offsets[3]),
        )
        card_name = _get_region_text(task, name_region).strip()
        if not card_name:
            # 强化图标模板有时匹配在牌名那一行（获得卡牌页实测置信度约 0.80，真正的类型图标在下面一行），
            # 牌名框就落到了卡面图上、牌名掉进了类型框。按“特征在牌名行”下移再框一次
            shift = -(name_offsets[1] + name_offsets[3]) / 2
            shifted = [
                (max(0.0, center_x + o[0]), max(0.0, center_y + shift + o[1]),
                 min(1.0, center_x + o[2]), min(1.0, center_y + shift + o[3]))
                for o in (name_offsets, type_offsets, description_offsets)
            ]
            shifted_name = _get_region_text(task, shifted[0]).strip()
            if shifted_name and re.sub(r'[^一-鿿]', '', shifted_name) not in _CARD_TYPE_WORDS:
                task.log_info(f"{log_prefix}卡牌识别调试: 特征={feature_name} 的牌名框为空，"
                              f"下移 {shift:.4f} 后读到牌名「{shifted_name}」")
                card_name = shifted_name
                name_region, type_region, desc_region = shifted
        task.log_info(
            f"{log_prefix}卡牌识别调试: 特征={feature_name}，"
            f"特征中心=({center_x:.4f},{center_y:.4f})，"
            f"特征置信度={feature_box.confidence:.4f}，"
            f"名称区域={tuple(round(value, 4) for value in name_region)}，"
            f"名称OCR={_region_text_debug_info(task, name_region)}，"
            f"类型区域={tuple(round(value, 4) for value in type_region)}，"
            f"类型OCR={_region_text_debug_info(task, type_region)}，"
            f"描述区域={tuple(round(value, 4) for value in desc_region)}"
        )
        if not card_name:
            task.log_info(f"{log_prefix}卡牌识别调试: 因名称为空排除该特征")
            continue
        card_type = _get_region_text(task, type_region).strip()
        description = _get_region_text(task, desc_region)
        name_only_threshold = (name_only_feature_thresholds or {}).get(
            feature_name
        )
        allow_name_only = (
            name_only_threshold is not None
            and feature_box.confidence > name_only_threshold
        )
        allow_empty_type = (
            allow_empty_type_threshold is not None
            and feature_box.confidence > allow_empty_type_threshold
        )
        if not allow_name_only and (
            not description or (not card_type and not allow_empty_type)
        ):
            task.log_info(
                f"{log_prefix}卡牌识别调试: 因类型或描述缺失排除该特征，"
                f"类型=「{card_type}」，描述=「{description}」"
            )
            continue
        cards.append({
            "name": card_name,
            "type": card_type,
            "description": description,
            "feature_name": feature_name,
            "feature_type": feature_type,
            "confidence": feature_box.confidence,
            "feature_box": feature_box,
            "x": (name_region[0] + name_region[2]) / 2,
            "y": (name_region[1] + name_region[3]) / 2,
            "name_region": name_region,
            "type_region": type_region,
            "description_region": desc_region,
        })
    cards.sort(key=lambda card: card["feature_box"].x)
    if cards:
        task.log_info(f"{log_prefix}卡牌识别到{len(cards)}张卡牌")
        for index, card in enumerate(cards, 1):
            task.log_info(
                f"{log_prefix}卡牌{index}: 名称=「{card['name']}」，"
                f"类型=「{card['type'] or card['feature_type']}」，"
                f"描述=「{card['description']}」，"
                f"特征={card['feature_name']}，置信度={card['confidence']:.4f}"
            )
    return cards


def recognize_cards(
    task: TriggerTask,
    region=(0.021, 0.172, 0.988, 0.432),
    page="",
):
    """识别卡牌选择页面中的卡牌。"""
    return _recognize_cards_by_features(
        task=task,
        region=region,
        page=page,
        feature_types={
            "attack": "攻击/基础攻击",
            "skill": "技能/基础技能",
            "enhance": "强化",
            "hex": "咒术",
            "abnormal": "状态异常",
        },
        min_feature_distance=(
            (0.618 - 0.454) ** 2 + (0.304 - 0.306) ** 2
        ) ** 0.5,
        name_offsets=(-0.0150, -0.0635, 0.1450, -0.0175),
        type_offsets=(0.0120, -0.0205, 0.1090, 0.0245),
        description_offsets=(-0.0565, 0.1190, 0.1495, 0.4900),
    )


def recognize_cards_in_deck(
    task: TriggerTask,
    region=(0.274, 0.108, 0.929, 0.874),
    page="",
):
    """识别卡组区域中的卡牌，并标记金色边框选中的卡牌。"""
    cards = _recognize_cards_by_features(
        task=task,
        region=region,
        page=page,
        feature_types={
            "attack_in_deck": "攻击/基础攻击",
            "skill_in_deck": "技能/基础技能",
            "enhance_in_deck": "强化",
            "hex_in_deck": "咒术",
            "hex_in_deck_tw": "诅咒",
        },
        min_feature_distance=(
            (0.464 - 0.326) ** 2 + (0.175 - 0.175) ** 2
        ) ** 0.5,
        name_offsets=(-0.0090, -0.0435, 0.0900, -0.0105),
        type_offsets=(0.0070, -0.0175, 0.0880, 0.0175),
        description_offsets=(-0.0370, 0.0515, 0.1000, 0.3295),
        name_only_feature_thresholds={
            "hex_in_deck": 0.90,
            "hex_in_deck_tw": 0.90,
        },
        allow_empty_type_threshold=0.90,
    )
    _mark_selected_card_by_gold_border(task, cards, page=page)
    return cards


def recognize_event_options(
    task: TriggerTask,
    region=(0.198, 0.840, 0.803, 1.000),
    page="",
):
    """按事件选项特征识别最多三个事件描述。"""
    event_region = task.box_of_screen(*region)
    event_features = []
    for feature_name in ("event1", "event2", "event3", "event4", "event5", "event6", "event7", "event8"):
        for feature_box in task.find_feature(
            feature_name=feature_name,
            box=event_region,
            threshold=0.70,
        ) or []:
            center_x = (feature_box.x + feature_box.width / 2) / task.width
            center_y = (feature_box.y + feature_box.height / 2) / task.height
            event_features.append(
                (feature_name, feature_box, center_x, center_y)
            )

    filtered_features = []
    for candidate in sorted(
        event_features,
        key=lambda item: item[1].confidence,
        reverse=True,
    ):
        if any(
            (
                (candidate[2] - kept[2]) ** 2
                + (candidate[3] - kept[3]) ** 2
            ) ** 0.5 < 0.207
            for kept in filtered_features
        ):
            continue
        filtered_features.append(candidate)
        if len(filtered_features) >= 3:
            break

    filtered_features.sort(key=lambda item: item[2])
    event_options = []
    for feature_name, feature_box, center_x, center_y in filtered_features:
        description_region = (
            max(0.0, center_x - 0.119),
            max(0.0, center_y - 0.208),
            min(1.0, center_x + 0.122),
            min(1.0, center_y - 0.021),
        )
        description = _get_region_text(task, description_region).strip()
        if not description:
            continue
        event_options.append({
            "x": center_x,
            "y": center_y,
            "description": description,
            "description_region": description_region,
            "feature_name": feature_name,
            "confidence": feature_box.confidence,
        })

    if event_options:
        prefix = f"{page}: " if page else ""
        task.log_info(f"{prefix}识别到{len(event_options)}个事件选项")
        for index, event_option in enumerate(event_options, 1):
            task.log_info(
                f"{prefix}事件选项{index}: 描述=「{event_option['description']}」，"
                f"特征={event_option['feature_name']}，"
                f"置信度={event_option['confidence']:.4f}"
            )
    return event_options


def recognize_map_connections(
    task: TriggerTask,
    region=(0.019, 0.633, 0.380, 0.972),
    feature_threshold=0.85,
    line_threshold=0.30,
    special_feature_threshold=0.65,
):
    """识别小地图节点，并根据节点之间亮线的连续覆盖率生成连通关系。"""
    if task.frame is None:
        task.log_info("小地图连通关系识别失败：当前画面为空")
        return {"nodes": [], "connections": [], "adjacency": {}}

    node_types = {
        "position_in_map": "当前位置",
        "settlement_in_map": "结算",
        "enemy_in_map": "小怪",
        "safezoom_in_map": "休息",
        "elite_in_map": "精英",
        "event_in_map": "事件",
    }
    # 值大于0表示优先进入，小于0表示降低进入优先级；后续新增标志只需
    # 在这里登记，不需要改动识别和绑定逻辑。
    special_feature_priorities = {
        "kalei_in_map": 1,
        "shop_in_map": 1,
        "seal_in_map": 1,
        "hard_in_map": -1,
    }
    search_box = task.box_of_screen(*region)
    candidates = []
    for feature_name, node_type in node_types.items():
        for feature_box in task.find_feature(
            feature_name=feature_name,
            box=search_box,
            threshold=feature_threshold,
        ) or []:
            center_x = (feature_box.x + feature_box.width / 2) / task.width
            center_y = (feature_box.y + feature_box.height / 2) / task.height
            # 当前位置是水滴形图标，线路实际连接点在图标下方尖端而非中心。
            if feature_name == "position_in_map":
                center_y += (feature_box.height / task.height) * 0.36
            candidates.append({
                "feature_name": feature_name,
                "type": node_type,
                "x": center_x,
                "y": center_y,
                "confidence": float(feature_box.confidence),
                "feature_box": feature_box,
            })

    position_candidates = [
        candidate for candidate in candidates
        if candidate["feature_name"] == "position_in_map"
    ]
    position_x = None
    passed_feature_x_limit = None
    if position_candidates:
        position_x = max(
            position_candidates,
            key=lambda item: item["confidence"],
        )["x"]
        passed_feature_x_limit = position_x + 0.027
        original_candidate_count = len(candidates)
        candidates = [
            candidate for candidate in candidates
            if candidate["feature_name"] == "position_in_map"
            or candidate["x"] >= passed_feature_x_limit
        ]
        task.log_info(
            f"小地图当前位置X={position_x:.4f}，过滤X小于"
            f"{passed_feature_x_limit:.4f}的已走过节点，"
            f"排除{original_candidate_count - len(candidates)}个普通节点特征"
        )

    # 同一节点可能被多个普通节点模板命中。按给定的两个参考点
    # (0.229, 0.674)、(0.257, 0.674)之间的距离去重，只保留最高置信度。
    feature_dedup_distance = (
        (0.257 - 0.229) ** 2 + (0.674 - 0.674) ** 2
    ) ** 0.5
    nodes = []
    for candidate in sorted(
        candidates,
        key=lambda item: item["confidence"],
        reverse=True,
    ):
        if any(
            (
                (candidate["x"] - kept["x"]) ** 2
                + (candidate["y"] - kept["y"]) ** 2
            ) ** 0.5 < feature_dedup_distance
            for kept in nodes
        ):
            continue
        nodes.append(candidate)
    # 小地图的推进方向是从左到右。先按横坐标聚类成列，再在每列内
    # 从上到下排序，保证节点编号与实际可选顺序一致。
    columns = []
    for node in sorted(nodes, key=lambda item: item["x"]):
        column = next(
            (
                existing
                for existing in columns
                if abs(node["x"] - existing["center_x"]) < 0.025
            ),
            None,
        )
        if column is None:
            columns.append({"center_x": node["x"], "nodes": [node]})
            continue
        column["nodes"].append(node)
        column["center_x"] = sum(
            item["x"] for item in column["nodes"]
        ) / len(column["nodes"])

    nodes = []
    for column_index, column in enumerate(columns):
        column_nodes = sorted(column["nodes"], key=lambda item: item["y"])
        for row_index, node in enumerate(column_nodes, start=1):
            node["column"] = column_index
            node["row"] = row_index
            node["id"] = len(nodes)
            node["special_features"] = []
            node["special_priority"] = 0
            nodes.append(node)

    # 每个特殊标志只绑定到距离最近的一个节点。节点可以同时具有多个标志。
    special_features = []
    for feature_name, priority in special_feature_priorities.items():
        for feature_box in task.find_feature(
            feature_name=feature_name,
            box=search_box,
            threshold=special_feature_threshold,
        ) or []:
            special_feature = {
                "feature_name": feature_name,
                "priority": priority,
                "x": (feature_box.x + feature_box.width / 2) / task.width,
                "y": (feature_box.y + feature_box.height / 2) / task.height,
                "confidence": float(feature_box.confidence),
                "feature_box": feature_box,
            }
            if (
                passed_feature_x_limit is not None
                and special_feature["x"] < passed_feature_x_limit
            ):
                task.log_info(
                    f"小地图特殊标志{feature_name}位于已走过区域，"
                    f"X={special_feature['x']:.4f}，排除"
                )
                continue
            special_features.append(special_feature)
    filtered_special_features = []
    for special_feature in sorted(
        special_features,
        key=lambda item: item["confidence"],
        reverse=True,
    ):
        if any(
            (
                (special_feature["x"] - kept["x"]) ** 2
                + (special_feature["y"] - kept["y"]) ** 2
            ) ** 0.5 < feature_dedup_distance
            for kept in filtered_special_features
        ):
            continue
        filtered_special_features.append(special_feature)
    special_features = filtered_special_features
    for special_feature in special_features:
        nearest_node = min(
            nodes,
            key=lambda node: (
                (node["x"] - special_feature["x"]) ** 2
                + (node["y"] - special_feature["y"]) ** 2
            ),
            default=None,
        )
        if nearest_node is None:
            continue
        distance = (
            (nearest_node["x"] - special_feature["x"]) ** 2
            + (nearest_node["y"] - special_feature["y"]) ** 2
        ) ** 0.5
        if distance > 0.035:
            continue
        special_feature["node_id"] = nearest_node["id"]
        nearest_node["special_features"].append(special_feature)
        nearest_node["special_priority"] += special_feature["priority"]

    gray = cv2.cvtColor(task.frame[:, :, :3], cv2.COLOR_BGR2GRAY)
    frame_height, frame_width = gray.shape[:2]
    perpendicular_radius = max(2, round(frame_height * 0.004))

    def line_brightness_ratio(first, second):
        start_x = first["x"] * frame_width
        start_y = first["y"] * frame_height
        end_x = second["x"] * frame_width
        end_y = second["y"] * frame_height
        delta_x = end_x - start_x
        delta_y = end_y - start_y
        pixel_distance = (delta_x ** 2 + delta_y ** 2) ** 0.5
        if pixel_distance <= 0:
            return 0.0
        perpendicular_x = -delta_y / pixel_distance
        perpendicular_y = delta_x / pixel_distance
        sample_count = max(8, round(pixel_distance * 0.40))
        bright_samples = 0
        for progress in np.linspace(0.30, 0.70, sample_count):
            sample_x = start_x + delta_x * progress
            sample_y = start_y + delta_y * progress
            band_values = []
            for offset in range(-perpendicular_radius, perpendicular_radius + 1):
                pixel_x = int(round(sample_x + perpendicular_x * offset))
                pixel_y = int(round(sample_y + perpendicular_y * offset))
                if 0 <= pixel_x < frame_width and 0 <= pixel_y < frame_height:
                    band_values.append(gray[pixel_y, pixel_x])
            if band_values and max(band_values) >= 110:
                bright_samples += 1
        return bright_samples / sample_count

    connections = []
    adjacency = {node["id"]: [] for node in nodes}

    def has_intermediate_node(first, second):
        vector_x = second["x"] - first["x"]
        vector_y = second["y"] - first["y"]
        vector_length_squared = vector_x ** 2 + vector_y ** 2
        if vector_length_squared <= 0:
            return False
        for other in nodes:
            if other is first or other is second:
                continue
            progress = (
                (other["x"] - first["x"]) * vector_x
                + (other["y"] - first["y"]) * vector_y
            ) / vector_length_squared
            if not 0.12 < progress < 0.88:
                continue
            projected_x = first["x"] + vector_x * progress
            projected_y = first["y"] + vector_y * progress
            if (
                (other["x"] - projected_x) ** 2
                + (other["y"] - projected_y) ** 2
            ) ** 0.5 < 0.025:
                return True
        return False

    def has_intermediate_special_feature(first, second):
        """判断候选连线是否穿过属于第三个节点的特殊标志。"""
        vector_x = second["x"] - first["x"]
        vector_y = second["y"] - first["y"]
        vector_length_squared = vector_x ** 2 + vector_y ** 2
        if vector_length_squared <= 0:
            return False
        endpoint_ids = {first["id"], second["id"]}
        for special_feature in special_features:
            if special_feature.get("node_id") in endpoint_ids:
                continue
            progress = (
                (special_feature["x"] - first["x"]) * vector_x
                + (special_feature["y"] - first["y"]) * vector_y
            ) / vector_length_squared
            if not 0.12 < progress < 0.88:
                continue
            projected_x = first["x"] + vector_x * progress
            projected_y = first["y"] + vector_y * progress
            if (
                (special_feature["x"] - projected_x) ** 2
                + (special_feature["y"] - projected_y) ** 2
            ) ** 0.5 < 0.015:
                return True
        return False

    for first_index, first in enumerate(nodes):
        for second in nodes[first_index + 1:]:
            # 只保留当前列指向右侧相邻列的边，不生成反向邻接关系。
            if second["column"] != first["column"] + 1:
                continue
            delta_x = abs(first["x"] - second["x"])
            delta_y = abs(first["y"] - second["y"])
            distance = (delta_x ** 2 + delta_y ** 2) ** 0.5
            if not 0.035 <= distance <= 0.210 or delta_x > 0.125:
                continue
            # 地图连线为横线或斜线；不同排的同列节点不直接相连。
            if delta_y > 0.025 and delta_x < 0.025:
                continue
            if has_intermediate_node(first, second):
                continue
            if has_intermediate_special_feature(first, second):
                continue
            brightness_ratio = line_brightness_ratio(first, second)
            if brightness_ratio < line_threshold:
                continue
            connection = {
                "from": first["id"],
                "to": second["id"],
                "brightness_ratio": brightness_ratio,
            }
            connections.append(connection)
            adjacency[first["id"]].append(second["id"])

    task.log_info(f"小地图识别到{len(nodes)}个节点、{len(connections)}条亮线连接")
    for node in nodes:
        task.log_info(
            f"小地图节点{node['id']}: 类型={node['type']}，"
            f"第{node['column'] + 1}列第{node['row']}个，"
            f"位置=({node['x']:.4f}, {node['y']:.4f})，"
            f"特征={node['feature_name']}，置信度={node['confidence']:.4f}，"
            f"特殊标志={[item['feature_name'] for item in node['special_features']]}，"
            f"特殊优先级={node['special_priority']}"
        )
    for connection in connections:
        task.log_info(
            f"小地图连接: 节点{connection['from']} -> 节点{connection['to']}，"
            f"亮线覆盖率={connection['brightness_ratio']:.2%}"
        )
    task.log_info(f"小地图有向邻接关系: {adjacency}")
    return {
        "nodes": nodes,
        "connections": connections,
        "adjacency": adjacency,
    }


def find_best_map_route(map_info, target_node_type):
    """寻找目标类型节点最多的有向路线，并返回下一列应选择的节点。"""
    nodes = map_info.get("nodes", [])
    adjacency = map_info.get("adjacency", {})
    node_by_id = {node["id"]: node for node in nodes}
    current_node = next(
        (node for node in nodes if node["type"] == "当前位置"),
        None,
    )
    if current_node is None:
        return {
            "target_type": target_node_type,
            "target_count": 0,
            "special_priority_score": 0,
            "route": [],
            "next_node_id": None,
            "next_row": None,
        }

    route_cache = {}

    def best_route_from(node_id):
        if node_id in route_cache:
            return route_cache[node_id]
        node = node_by_id[node_id]
        is_target = node["type"] == target_node_type
        own_score = int(is_target)
        own_special_score = node.get("special_priority", 0) if is_target else 0
        next_node_ids = adjacency.get(node_id, [])
        if not next_node_ids:
            result = (own_score, own_special_score, [node_id])
            route_cache[node_id] = result
            return result

        candidates = []
        for next_node_id in next_node_ids:
            child_score, child_special_score, child_route = best_route_from(
                next_node_id
            )
            candidates.append((
                own_score + child_score,
                own_special_score + child_special_score,
                node_by_id[next_node_id]["row"],
                [node_id, *child_route],
            ))
        # 先比较目标节点数量，再比较目标节点携带的特殊优先级；仍相同时
        # 选择下一节点更靠上的路线。
        best_score, best_special_score, _, best_route = min(
            candidates,
            key=lambda item: (-item[0], -item[1], item[2]),
        )
        result = (best_score, best_special_score, best_route)
        route_cache[node_id] = result
        return result

    target_count, special_priority_score, route = best_route_from(
        current_node["id"]
    )
    next_node = node_by_id[route[1]] if len(route) > 1 else None
    return {
        "target_type": target_node_type,
        "target_count": target_count,
        "special_priority_score": special_priority_score,
        "route": route,
        "next_node_id": next_node["id"] if next_node else None,
        "next_row": next_node["row"] if next_node else None,
    }


def find_best_map_route_by_priority(map_info, node_type_priority):
    """按节点类型加权计算最优路线，并返回下一列应选择的节点。"""
    nodes = map_info.get("nodes", [])
    adjacency = map_info.get("adjacency", {})
    node_by_id = {node["id"]: node for node in nodes}
    current_node = next(
        (node for node in nodes if node["type"] == "当前位置"),
        None,
    )
    if current_node is None:
        return None

    route_cache = {}
    priority_weights = {
        node_type: len(node_type_priority) - index
        for index, node_type in enumerate(node_type_priority)
    }
    highest_priority_weight = max(priority_weights.values(), default=1)
    shop_bonus = highest_priority_weight * 2

    def best_route_from(node_id):
        if node_id in route_cache:
            return route_cache[node_id]
        node = node_by_id[node_id]
        own_counts = tuple(
            int(node["type"] == node_type)
            for node_type in node_type_priority
        )
        own_shop_count = int(any(
            item["feature_name"] == "shop_in_map"
            for item in node.get("special_features", [])
        ))
        own_special_score = sum(
            item["priority"]
            for item in node.get("special_features", [])
            if item["feature_name"] != "shop_in_map"
        )
        own_weighted_score = (
            priority_weights.get(node["type"], 0)
            + own_shop_count * shop_bonus
            + own_special_score
        )
        next_node_ids = adjacency.get(node_id, [])
        if not next_node_ids:
            result = (
                own_weighted_score,
                own_shop_count,
                own_counts,
                own_special_score,
                [node_id],
            )
            route_cache[node_id] = result
            return result

        candidates = []
        for next_node_id in next_node_ids:
            (
                child_weighted_score,
                child_shop_count,
                child_counts,
                child_special_score,
                child_route,
            ) = best_route_from(next_node_id)
            total_counts = tuple(
                own + child
                for own, child in zip(own_counts, child_counts)
            )
            candidates.append((
                own_weighted_score + child_weighted_score,
                own_shop_count + child_shop_count,
                total_counts,
                own_special_score + child_special_score,
                node_by_id[next_node_id]["row"],
                [node_id, *child_route],
            ))
        (
            best_weighted_score,
            best_shop_count,
            best_counts,
            best_special_score,
            _,
            best_route,
        ) = min(
            candidates,
            key=lambda item: (
                -item[0],
                -item[1],
                tuple(-count for count in item[2]),
                -item[3],
                item[4],
            ),
        )
        result = (
            best_weighted_score,
            best_shop_count,
            best_counts,
            best_special_score,
            best_route,
        )
        route_cache[node_id] = result
        return result

    (
        weighted_score,
        shop_count,
        type_counts,
        special_priority_score,
        route,
    ) = best_route_from(current_node["id"])
    next_node = node_by_id[route[1]] if len(route) > 1 else None
    return {
        "priority": list(node_type_priority),
        "priority_weights": priority_weights,
        "shop_bonus": shop_bonus,
        "weighted_score": weighted_score,
        "shop_count": shop_count,
        "type_counts": dict(zip(node_type_priority, type_counts)),
        "special_priority_score": special_priority_score,
        "route": route,
        "next_node_id": next_node["id"] if next_node else None,
        "next_row": next_node["row"] if next_node else None,
        "next_node_type": next_node["type"] if next_node else None,
        "next_special_features": [
            item["feature_name"]
            for item in next_node.get("special_features", [])
        ] if next_node else [],
    }


def _mark_selected_card_by_gold_border(
    task: TriggerTask,
    cards,
    page="",
    threshold=0.25,
):
    """计算卡牌的金黄色边框得分，并在卡牌信息中写入选中状态。"""
    for card in cards:
        card["selected"] = False
        card["gold_border_score"] = 0.0
        card["gold_border_edges"] = {}
    if not cards or task.frame is None:
        return

    frame = task.frame[:, :, :3]
    frame_height, frame_width = frame.shape[:2]
    band_x = max(2, round(frame_width * 0.004))
    band_y = max(2, round(frame_height * 0.006))

    def gold_ratio(left, top, right, bottom):
        left = max(0, min(frame_width, round(left)))
        right = max(0, min(frame_width, round(right)))
        top = max(0, min(frame_height, round(top)))
        bottom = max(0, min(frame_height, round(bottom)))
        if right <= left or bottom <= top:
            return 0.0
        hsv = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2HSV)
        gold_mask = cv2.inRange(
            hsv,
            np.array((8, 100, 160), dtype=np.uint8),
            np.array((38, 255, 255), dtype=np.uint8),
        )
        return float(cv2.countNonZero(gold_mask)) / gold_mask.size

    scored_cards = []
    for card in cards:
        feature_box = card["feature_box"]
        center_x = feature_box.x + feature_box.width / 2
        center_y = feature_box.y + feature_box.height / 2
        card_left = center_x - frame_width * 0.047
        card_right = center_x + frame_width * 0.103
        card_top = center_y - frame_height * 0.061
        card_bottom = center_y + frame_height * 0.330

        edge_scores = {
            "上": gold_ratio(
                card_left, card_top - band_y, card_right, card_top + band_y
            ),
            "下": gold_ratio(
                card_left, card_bottom - band_y, card_right, card_bottom + band_y
            ),
            "左": gold_ratio(
                card_left - band_x, card_top, card_left + band_x, card_bottom
            ),
            "右": gold_ratio(
                card_right - band_x, card_top, card_right + band_x, card_bottom
            ),
        }
        visible_edge_scores = [
            edge_scores["左"],
            edge_scores["右"],
        ]
        score = sum(visible_edge_scores) / len(visible_edge_scores)
        strong_edge_count = sum(value >= 0.08 for value in visible_edge_scores)
        card["gold_border_score"] = score
        card["gold_border_edges"] = edge_scores
        scored_cards.append((score, strong_edge_count, card))

    prefix = f"{page}: " if page else ""
    for score, strong_edge_count, card in scored_cards:
        if score >= threshold and strong_edge_count == 2:
            card["selected"] = True

    for card in cards:
        edge_scores = card["gold_border_edges"]
        task.log_info(
            f"{prefix}卡牌「{card['name']}」是否选中={card['selected']}，"
            f"金色边框得分={card['gold_border_score']:.4f}，"
            f"上={edge_scores['上']:.4f}，下={edge_scores['下']:.4f}，"
            f"左={edge_scores['左']:.4f}，右={edge_scores['右']:.4f}"
        )

    if not any(card["selected"] for card in cards):
        task.log_info(f"{prefix}未检测到选中卡牌的金色边框")


def find_text(task: TriggerTask, pattern):
    """按正则在所有识别文本中查找第一个匹配的 box。"""
    return next((b for b in task.all_texts if re.search(pattern, b.name)), None)


def _clean_match(name, target):
    """去除OCR文本中的非中文/字母/数字符号后比较是否等于 target。"""
    cleaned = re.sub(r'[^\u4e00-\u9fff\w]', '', name)
    return cleaned == target


def _get_region_text(task: TriggerTask, region):
    """获取指定区域内所有OCR文本，去除空白后用"".join拼接返回。"""
    x1, y1, x2, y2 = region
    texts = [
        b.name.strip() for b in task.all_texts
        if x1 <= (b.x + b.width / 2) / task.width <= x2
        and y1 <= (b.y + b.height / 2) / task.height <= y2
        and b.name.strip()
    ]
    return "".join(texts)


def _region_text_debug_info(task: TriggerTask, region):
    """返回参与区域文本拼接的OCR框信息，用于排查相对区域偏移。"""
    x1, y1, x2, y2 = region
    matched = []
    for box in task.all_texts:
        center_x = (box.x + box.width / 2) / task.width
        center_y = (box.y + box.height / 2) / task.height
        if x1 <= center_x <= x2 and y1 <= center_y <= y2 and box.name.strip():
            matched.append(
                f"「{box.name.strip()}」"
                f"(中心={center_x:.4f},{center_y:.4f},"
                f"置信度={box.confidence:.4f})"
            )
    return "，".join(matched) if matched else "无"


_CARD_TYPE_KEYWORDS = {
    "攻击", "强化", "技能", "技", "咒术", "诅咒",
    "攻", "击", "基础", "基本", "状态", "异常",
}


def _card_has_type_below(task: TriggerTask, box):
    """判断文本框下方是否有卡牌类型标签（卡牌名特征）。"""
    box_bottom_y = (box.y + box.height) / task.height
    box_cx = (box.x + box.width / 2) / task.width
    for b in task.all_texts:
        cx = (b.x + b.width / 2) / task.width
        cy = (b.y + b.height / 2) / task.height
        dy = cy - box_bottom_y
        dx = abs(cx - box_cx)
        if -0.005 <= dy <= 0.040 and dx <= 0.045 and len(b.name) <= 4:
            for kw in _CARD_TYPE_KEYWORDS:
                if kw in b.name:
                    return True
    return False


def region_white_ratio(task: TriggerTask, region):
    """计算指定区域内白色像素占比。"""
    if task.frame is None:
        return 1.0
    region_box = task.box_of_screen(*region)
    pixels = task.frame[
        region_box.y:region_box.y + region_box.height,
        region_box.x:region_box.x + region_box.width,
        :3,
    ]
    if pixels.size == 0:
        return 1.0
    channel_min = pixels.min(axis=2)
    channel_max = pixels.max(axis=2)
    white_mask = (channel_min >= 240) & ((channel_max - channel_min) <= 15)
    return float(np.count_nonzero(white_mask)) / white_mask.size


def _point_is_white(task: TriggerTask, x, y, page):
    """判断指定点是否为选牌页面滚动条使用的白色。"""
    pixel_x = min(task.width - 1, max(0, round(x * task.width)))
    pixel_y = min(task.height - 1, max(0, round(y * task.height)))
    blue, green, red = (
        int(value) for value in task.frame[pixel_y, pixel_x, :3]
    )
    is_white = min(blue, green, red) >= 240 and (
        max(blue, green, red) - min(blue, green, red)
    ) <= 15
    task.log_info(
        f"{page}: 点({x:.3f}, {y:.3f})颜色="
        f"B{blue}/G{green}/R{red}，是否白色={is_white}"
    )
    return is_white


def _scroll_card_page(task: TriggerTask, x, y, amount, page, distance=0.25):
    """将鼠标移到选牌区域后滚动。"""
    direction = "向下" if amount < 0 else "向上"
    if task.is_adb():
        to_y = max(0.05, y - distance) if amount < 0 else min(0.95, y + distance)
        task.log_info(
            f"{page}: ADB从({x:.3f}, {y:.3f})滑动到({x:.3f}, {to_y:.3f})，"
            f"{direction}浏览卡牌"
        )
        task.swipe_relative(x, y, x, to_y, duration=1, settle_time=1)
        task.sleep(1)
    else:
        task.log_info(f"{page}: 在({x:.3f}, {y:.3f}){direction}滚动")
        task.move_relative(x, y)
        task.sleep(0.05)
        task.scroll_relative(x, y, amount)
        task.sleep(0.5)


_SELECT_CARD_SESSION_GAP = 10.0   # 中间没离开过，隔这么久没见本页才算换了新会话（一般靠离开标记重置）
_SELECT_CARD_RETRY_SECONDS = 4.0  # 选满后按钮这么久还没把页面点走，解锁重新决策
_SELECT_CARD_PROMPT = re.compile(r'请选择(\d*)张*.*?(移除|复制|闪光|灵光).*?卡牌')


def _select_card_memory(task: TriggerTask, action, count):
    """取本页面的选卡记忆；页面离开过（handle_select_card 不匹配时会清 seen）或换页了就重新开始记。"""
    memory = getattr(task, "_select_card_memory", None)
    now = time.time()
    if (memory is None or memory["action"] != action or memory["count"] != count
            or not memory["seen"] or now - memory["seen"] > _SELECT_CARD_SESSION_GAP):
        memory = {"action": action, "count": count, "picked": [], "t": 0.0, "seen": now,
                  "scrolls": 0, "at_top": False, "deck_bottom": False, "ready": False}
        task._select_card_memory = memory
    memory["seen"] = now
    return memory


def _note_select_card_pick(task: TriggerTask, x, y, name):
    """记一次选卡点击（位置、牌名和时间）：选满后本页不再重新决策，续选时跳过已点过的位置。"""
    memory = getattr(task, "_select_card_memory", None)
    if memory is not None:
        memory["picked"].append((x, y, name))
        memory["t"] = time.time()
        if len(memory["picked"]) >= memory["count"]:
            memory["ready"] = True


def _card_picked_before(task: TriggerTask, card):
    """这张牌是不是本页已经点过：位置相近且名字对得上（OCR 互相包含也算）。
    光看位置会把下一个选卡页面同位置的另一张牌误当成点过的。"""
    memory = getattr(task, "_select_card_memory", None)
    if not memory:
        return False
    for x, y, name in memory["picked"]:
        if abs(card["x"] - x) <= 0.06 and abs(card["y"] - y) <= 0.10 and _names_match(card["name"], name):
            return True
    return False


def _deck_frame_gray(task: TriggerTask):
    """牌库区域画面的粗灰度图，滚动前后不变说明滚不动了（到底/到顶）。"""
    frame = getattr(task, "frame", None)
    if frame is None:
        return None
    return frame[int(0.108 * task.height):int(0.874 * task.height):16,
                 int(0.274 * task.width):int(0.929 * task.width):16, :3]


def _deck_scrolled(task: TriggerTask, before):
    """滚动后牌库画面变化没有；一路滚不动（和上一张一样）算已经到边界。"""
    after = _deck_frame_gray(task)
    if before is None or after is None or before.shape != after.shape:
        return True
    return float(np.mean(np.abs(after.astype(np.int16) - before.astype(np.int16)))) > 2.0


def select_card(task: TriggerTask, card_names, count=1, action=""):
    """使用卡组特征识别选择卡牌，支持滚动查找、基础牌移除和兜底选择。"""
    selected = 0
    max_scrolls = 20
    page = f"select_card-{action}" if action else "select_card"
    # 选满要求张数后本页不再重新决策：点过的卡变暗/被点击光效盖住后名字读不出，
    # 重新决策会把刚选的顶掉（2026-10-04 19:56 实况：选了「定位雷射」，下一帧认不出又点「钴蓝之光」）。
    # 锁定期间交给按钮 handler 把页面点走；4 秒后按钮还没亮，认为那次点击没生效，解锁重新决策。
    memory = _select_card_memory(task, action, count)
    if len(memory["picked"]) >= count:
        memory["ready"] = True
        if time.time() - memory["t"] < _SELECT_CARD_RETRY_SECONDS:
            task.log_info(f"{page}: 本页已点过 {len(memory['picked'])} 张卡，等按钮响应，不重复选择")
            return True
        task.log_info(f"{page}: 选满后 {_SELECT_CARD_RETRY_SECONDS} 秒按钮还没亮，解锁重新决策")
        memory["picked"] = []
        memory["ready"] = False
    prefer_remove_base = (
        action == "移除"
        and _get_config_value(task, "优先移除基础牌", True)
    )
    prefer_target_member_row = (
        action == "移除"
        and _get_config_value(task, "刷空档", False) is True
    )
    base_card_type = _get_game_text(task, "基础")
    target_member_box = task.box_of_screen(0.079, 0.092, 0.209, 0.675)
    flash_rules = _flash_rules(task) if action in ("闪光", "灵光") else []
    # 出击模式闪光：选牌页里只列出还能闪光的牌，记下看到过的牌名，用来判断列表里的牌是否已经闪完
    sortie_flash = action in ("闪光", "灵光") and task.name == "自动出击模式"
    seen_cards = {}

    def log_pick(card, reason):
        battle_log.record(task, "选牌", action=action or "选择", card=card["name"], reason=reason,
                          description=card.get("description"), wanted=list(card_names or [])[:20])

    def record_pending_removal():
        if action == "移除":
            task._pending_removed_card_count = (
                getattr(task, "_pending_removed_card_count", 0) + 1
            )

    def filter_flash_priority_cards(cards):
        """闪光时排除已命中闪光优先级的卡牌，避免重复选择。"""
        if not flash_rules:
            return cards

        filtered_cards = []
        for card in cards:
            matched_keyword = next(
                (rule[0] for rule in flash_rules if _flash_rule_matches(rule, card)),
                None,
            )
            if matched_keyword:
                task.log_info(
                    f"{page}: 卡牌「{card['name']}」的名称和描述命中"
                    f"闪光优先级「{matched_keyword}」，排除该卡牌"
                )
                continue
            filtered_cards.append(card)
        return filtered_cards

    def refresh_cards():
        task.all_texts = _simplify_texts(task.ocr())
        cards = recognize_cards_in_deck(task, page=page)
        cards = filter_flash_priority_cards(cards)
        for card in cards:
            if card["name"].strip():
                seen_cards.setdefault(card["name"].strip(), card)
        return cards

    def click_cards(cards, predicate, reason):
        nonlocal selected
        clicked = False
        for card in cards:
            if selected >= count:
                break
            if card["selected"] or not predicate(card):
                continue
            task.log_info(f"{page}: {reason}「{card['name']}」")
            log_pick(card, reason)
            battle_log.process_shot(task, "选牌")
            _move_and_click(task, card["x"], card["y"])
            task.sleep(0.3)
            card["selected"] = True
            selected += 1
            record_pending_removal()
            _note_select_card_pick(task, card["x"], card["y"], card["name"])
            clicked = True
            if _acceleration_on(task) and any(
                not later["selected"] and predicate(later) for later in cards[cards.index(card) + 1:]
            ):
                # 下一张也在这一页：这一帧先停，0.3 秒交给文字闸门。后面没有要连点的牌就继续翻页。
                return "pause"
        return clicked

    def click_priority_cards(cards):
        nonlocal selected
        clicked = False
        for target in card_names:
            if selected >= count:
                break
            target = target.strip() if isinstance(target, str) else ""
            if not target:
                continue
            for card in cards:
                if selected >= count:
                    break
                if card["selected"]:
                    continue
                card_name = card["name"].strip()
                if target not in card_name and card_name not in target:
                    continue
                task.log_info(
                    f"{page}: 命中优先级「{target}」，点击目标卡牌「{card['name']}」"
                )
                log_pick(card, f"命中优先级「{target}」")
                battle_log.process_shot(task, "选牌")
                _move_and_click(task, card["x"], card["y"])
                task.sleep(0.3)
                card["selected"] = True
                selected += 1
                record_pending_removal()
                _note_select_card_pick(task, card["x"], card["y"], card["name"])
                clicked = True
                if _acceleration_on(task) and _another_priority_card(target, card):
                    return "pause"
        return clicked

    def _another_priority_card(current_target, clicked_card):
        """这一页上还有没有下一张马上要点的优先级牌。有的话这一帧先停，没有就继续翻页。"""
        targets = [name.strip() for name in card_names if isinstance(name, str) and name.strip()]
        try:
            start = targets.index(current_target.strip())
        except ValueError:
            start = 0
        for name in targets[start:]:
            for later in cards:
                if later is clicked_card or later["selected"]:
                    continue
                later_name = later["name"].strip()
                if name in later_name or later_name in name:
                    return True
        return False

    def click_target_member_row_cards(cards):
        """刷空档时优先移除目标主战员同一排的卡牌。"""
        if not prefer_target_member_row or selected >= count:
            return False
        if not task.feature_exists("target_member_in_select_card"):
            return False
        target_member = task.find_one(
            feature_name="target_member_in_select_card",
            box=target_member_box,
            threshold=0.6,
        )
        if not target_member:
            return False
        target_y = (
            target_member.y + target_member.height / 2
        ) / task.height
        task.log_info(
            f"{page}: 刷空档找到目标主战员，相似度="
            f"{target_member.confidence:.4f}，中心Y={target_y:.4f}"
        )
        return click_cards(
            cards,
            lambda card: abs(card["y"] - target_y) <= 0.25,
            "刷空档优先移除目标主战员同排卡牌，点击",
        )

    def sync_visible_selected(cards):
        nonlocal selected
        if selected == 0:
            selected = min(count, sum(card["selected"] for card in cards))

    def find_action_button():
        if not action:
            return None
        action_text = _get_game_text(task, action)
        return next(
            (
                box for box in task.all_texts
                if 0.495 <= (box.x + box.width / 2) / task.width <= 0.997
                and 0.878 <= (box.y + box.height / 2) / task.height <= 1.001
                and action_text in box.name
            ),
            None,
        )

    cards = refresh_cards()
    if not cards:
        if find_action_button():
            task.log_info(
                f"{page}: 首次识别卡牌漏识别，但仍存在「{action}」按钮，"
                "继续执行选卡流程"
            )
        else:
            task.log_info(f"{page}: 未识别到任何卡牌或操作按钮，终止选卡")
            return False
    if memory["picked"]:
        # 本页点过的位置当作已选中：续选和重新决策时都不再点它们（单选项重点=取消选中）
        for card in cards:
            if not card["selected"] and _card_picked_before(task, card):
                card["selected"] = True
    sync_visible_selected(cards)
    scrollbar_white_ratio = region_white_ratio(
        task, (0.976, 0.119, 0.988, 0.858)
    )
    single_page = scrollbar_white_ratio < 0.01
    task.log_info(
        f"{page}: 滚动条区域白色像素占比={scrollbar_white_ratio:.2%}，"
        f"是否仅一页卡牌={single_page}"
    )

    def _paused(result):
        """加速时一帧只点一张，把连点之间的等待交给文字闸门。"""
        if result == "pause":
            task.log_info(f"{page}: 已点一张，等页面响应后再点下一张")
            return True
        return False

    def finish_if_full():
        """选满了：记下 ready，下一帧才允许 handle_flash 点右下角按钮。"""
        if selected < count:
            return False
        memory["ready"] = True
        task.log_info(f"{page}: 已选中{selected}/{count}张卡牌")
        return True

    while True:
        if _paused(click_target_member_row_cards(cards)):
            return True
        if finish_if_full():
            return True
        if _paused(click_priority_cards(cards)):
            return True
        if finish_if_full():
            return True

        if single_page:
            task.log_info(f"{page}: 当前仅一页卡牌，不执行向下滚动")
            break

        if memory["deck_bottom"]:
            task.log_info(f"{page}: 本页之前已确认翻到底，不再向下滚动")
            break

        if _point_is_white(task, 0.982, 0.846, page):
            memory["deck_bottom"] = True
            task.log_info(f"{page}: 检测到已到达卡牌底部")
            break

        if memory["scrolls"] >= max_scrolls:
            task.log_info(f"{page}: 向下滚动已达到{max_scrolls}次限制")
            break

        before_gray = _deck_frame_gray(task)
        _scroll_card_page(task, 0.251, 0.735, -3, page)
        memory["scrolls"] += 1
        cards = refresh_cards()
        if not cards:
            if find_action_button():
                task.log_info(
                    f"{page}: 向下滚动后卡牌漏识别，但仍存在「{action}」按钮，"
                    "继续向下滚动"
                )
                continue
            task.log_info(f"{page}: 向下滚动后未识别到卡牌或操作按钮，终止选卡")
            return False
        if not _deck_scrolled(task, before_gray):
            memory["deck_bottom"] = True
            task.log_info(f"{page}: 向下滚动后画面没有变化，视为已到达底部")
            break

    if action == "移除" and selected < count:
        bottom_to_top_cards = sorted(
            cards,
            key=lambda card: (card["y"], card["x"]),
            reverse=True,
        )
        if _paused(click_cards(
            bottom_to_top_cards,
            lambda card: card["feature_name"] in {
                "hex_in_deck",
                "hex_in_deck_tw",
            },
            "底部页面优先移除咒术卡牌，点击",
        )):
            return True
        if finish_if_full():
            return True

    if prefer_remove_base and selected < count:
        bottom_to_top_cards = sorted(
            cards,
            key=lambda card: (card["y"], card["x"]),
            reverse=True,
        )
        if _paused(click_cards(
            bottom_to_top_cards,
            lambda card: base_card_type in card["type"],
            "底部页面优先移除基础牌，点击",
        )):
            return True
        if finish_if_full():
            return True

        up_scrolls = 0
        while not single_page:
            if memory["at_top"]:
                task.log_info(f"{page}: 本页之前已确认翻到顶，不再向上滚动")
                break

            if _point_is_white(task, 0.982, 0.128, page):
                memory["at_top"] = True
                task.log_info(f"{page}: 检测到已到达卡牌顶部")
                break

            if up_scrolls >= max_scrolls:
                task.log_info(f"{page}: 向上滚动已达到{max_scrolls}次限制")
                break

            before_gray = _deck_frame_gray(task)
            _scroll_card_page(task, 0.252, 0.179, 3, page)
            up_scrolls += 1
            if not _deck_scrolled(task, before_gray):
                memory["at_top"] = True
                task.log_info(f"{page}: 向上滚动后画面没有变化，视为已到达顶部")
                break
            cards = refresh_cards()
            if not cards:
                if find_action_button():
                    task.log_info(
                        f"{page}: 向上滚动后卡牌漏识别，但仍存在「{action}」按钮，"
                        "继续向上滚动"
                    )
                    continue
                task.log_info(f"{page}: 向上滚动后未识别到卡牌或操作按钮，终止选卡")
                return False
            if _paused(click_target_member_row_cards(cards)):
                return True
            if finish_if_full():
                return True
            bottom_to_top_cards = sorted(
                cards,
                key=lambda card: (card["y"], card["x"]),
                reverse=True,
            )
            if _paused(click_cards(
                bottom_to_top_cards,
                lambda card: base_card_type in card["type"],
                "向上翻页找到基础牌，点击",
            )):
                return True
            if finish_if_full():
                return True

    if sortie_flash and selected < count:
        _record_flash_done(task, card_names, seen_cards, page)
        # 点进闪光时已经扣了信用点，跳过也不退：按出牌优先级、再按攻击牌兜底选一张（2026-09-29 实跑中白跳过十几次）
        fallback = _flash_fallback_card(task, seen_cards)
        if fallback and _click_card_from_top(task, fallback, refresh_cards, page):
            memory["ready"] = True
            return True

    task.all_texts = _simplify_texts(task.ocr())
    action_box = task.box_of_screen(0.424, 0.882, 1.000, 0.999)
    for button_name in ("跳过", "取消"):
        button = next(
            (
                box for box in task.all_texts
                if action_box.x <= box.x + box.width / 2 <= action_box.x + action_box.width
                and action_box.y <= box.y + box.height / 2 <= action_box.y + action_box.height
                and button_name in box.name
            ),
            None,
        )
        if button:
            task.log_info(f"{page}: 未找到足够卡牌，点击「{button_name}」")
            task.click_box(button)
            if action == "移除":
                task._pending_removed_card_count = 0
            return True

    cards = recognize_cards_in_deck(task, page=f"{page}-兜底")
    cards = filter_flash_priority_cards(cards)
    fallback_cards = sorted(
        cards,
        key=lambda card: (card["y"], card["x"]),
        reverse=True,
    )
    if _paused(click_cards(fallback_cards, lambda card: True, "兜底补选卡牌，点击")):
        return True
    task.log_info(f"{page}: 兜底处理完成，已选中{selected}/{count}张卡牌")
    if selected >= count:
        memory["ready"] = True
    return True


def _names_match(first, second):
    """牌名互相包含就算同一张（OCR 常多读或漏读一两个字）。"""
    first, second = first.strip(), second.strip()
    return bool(first and second) and (first in second or second in first)


def _record_flash_done(task: TriggerTask, card_names, seen_cards, page):
    """选牌页翻完了：「闪光卡牌列表」里没出现的牌记为本局不用再闪（已经闪过，或本局没拿到）。"""
    status = getattr(task, "node_status", None)
    if status is None:
        return
    done = status.setdefault("flash_done_cards", [])
    for name in card_names:
        if not isinstance(name, str) or not name.strip() or name.strip() in done:
            continue
        if not any(_names_match(name, seen) for seen in seen_cards):
            done.append(name.strip())
            task.log_info(f"{page}: 选牌页里没有「{name.strip()}」，本局不再为它进闪光")


def flash_list_done(task: TriggerTask):
    """「闪光卡牌列表」里的牌是否都已记为本局不用再闪；列表为空也算（没有想闪的牌）。"""
    done = (getattr(task, "node_status", None) or {}).get("flash_done_cards", [])
    names = [name.strip() for name in _get_card_list(task, "闪光卡牌列表") if isinstance(name, str) and name.strip()]
    return all(name in done for name in names)


def _flash_fallback_card(task: TriggerTask, seen_cards):
    """列表里的牌都不在：按出牌优先级挑，再挑第一张攻击牌，再挑第一张牌；本页点过的不再挑
    （点过再点=取消选中，会把选择顶掉）。返回牌名。"""
    memory = getattr(task, "_select_card_memory", None)
    picked_names = [picked for _, _, picked in memory["picked"]] if memory else []
    candidates = [seen for seen in seen_cards
                  if not any(_names_match(seen, picked) for picked in picked_names)]
    for name in _get_card_list(task, "出牌优先级"):
        if not isinstance(name, str):
            continue
        found = next((seen for seen in candidates if _names_match(name, seen)), None)
        if found:
            return found
    attack = _get_game_text(task, "攻击")
    found = next((seen for seen in candidates if attack in seen_cards[seen].get("type", "")), None)
    return found or next(iter(candidates), None)


def _click_card_from_top(task: TriggerTask, name, refresh_cards, page, max_scrolls=20):
    """滚回选牌页顶部，再往下翻找到这张牌并点击；本页滚到过顶/底就不再重滚（跨帧）。"""
    memory = getattr(task, "_select_card_memory", None)
    at_top = bool(memory and memory["at_top"])
    for _ in range(max_scrolls):
        if at_top or _point_is_white(task, 0.982, 0.128, page):
            at_top = True
            break
        before_gray = _deck_frame_gray(task)
        _scroll_card_page(task, 0.252, 0.179, 3, page)
        task.next_frame()
        if not _deck_scrolled(task, before_gray):
            at_top = True
            break
    if memory is not None and at_top:
        memory["at_top"] = True
    for _ in range(max_scrolls):
        cards = refresh_cards()
        card = next((c for c in cards if _names_match(name, c["name"]) and not c["selected"]
                     and not _card_picked_before(task, c)), None)
        if card:
            task.log_info(f"{page}: 闪光卡牌列表里的牌都不在，兜底选择「{card['name']}」")
            _move_and_click(task, card["x"], card["y"])
            task.sleep(0.3)
            _note_select_card_pick(task, card["x"], card["y"], card["name"])
            return True
        if memory is not None and memory["deck_bottom"]:
            break
        if _point_is_white(task, 0.982, 0.846, page):
            if memory is not None:
                memory["deck_bottom"] = True
            break
        before_gray = _deck_frame_gray(task)
        _scroll_card_page(task, 0.251, 0.735, -3, page)
        if not _deck_scrolled(task, before_gray):
            if memory is not None:
                memory["deck_bottom"] = True
            task.log_info(f"{page}: 兜底查找向下滚动时画面没有变化，视为已到达底部")
            break
    task.log_info(f"{page}: 兜底没找回「{name}」")
    return False


def calculate_dominant_hue(task: TriggerTask, region):
    """计算区域的主导色相，返回色相值(0-179)，无有效色相返回-1。"""
    box = task.box_of_screen(*region)
    frame = task.frame[box.y:box.y + box.height, box.x:box.x + box.width, :3]
    hue, sat, val = cv2.split(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV))

    valid_hue = hue[(sat > 30) & (val > 30)]
    if len(valid_hue) == 0:
        return -1

    hist = cv2.calcHist([valid_hue.astype(np.float32)], [0], None, [180], [0, 180])
    return int(np.argmax(hist))


def is_button_active(task: TriggerTask, button_box):
    """判断按钮是否处于可点击状态（激活状态）。

    参数:
        task: TriggerTask实例
        button_box: 按钮文本的Box对象（像素坐标）

    返回:
        bool: True表示按钮可点击（激活），False表示不可点击（未激活/灰色）
    """
    # 计算左侧检测区域（按钮图标/背景区域）
    # 根据用户提供的例子推算比例：
    # 按钮box: (0.898, 0.908, 0.941, 0.950) w=0.043, h=0.042
    # 左侧区域: (0.866, 0.912, 0.895, 0.947) w=0.029, h=0.035
    # 左侧区域宽度 = 按钮宽度 * 0.67，x = 按钮x - 左侧区域宽度 * 1.1
    # 左侧区域高度 = 按钮高度 * 0.83，y = 按钮y + 按钮高度 * 0.1

    left_width = int(button_box.width * 0.67)
    left_height = int(button_box.height * 0.83)
    left_x = button_box.x - int(left_width * 1.1)
    left_y = button_box.y + int(button_box.height * 0.1)

    # 确保区域在屏幕内
    if left_x < 0:
        left_x = 0
    if left_y < 0:
        left_y = 0
    if left_x + left_width > task.width:
        left_width = task.width - left_x
    if left_y + left_height > task.height:
        left_height = task.height - left_y

    if left_width <= 0 or left_height <= 0:
        task.log_info(f"按钮左侧区域无效: ({left_x}, {left_y}, {left_width}, {left_height})")
        return False

    # 提取区域图像
    region_img = task.frame[left_y:left_y + left_height, left_x:left_x + left_width, :3]
    if region_img.size == 0:
        task.log_info("按钮左侧区域图像为空")
        return False

    # 计算平均BGR颜色
    avg_color = cv2.mean(region_img)[:3]  # B, G, R 平均值
    avg_b, avg_g, avg_r = avg_color

    # 判断是否接近禁用灰色 (195,195,195)
    # 容错范围：每个通道在190-200之间，且三个通道值接近
    # target_gray = 195
    tolerance = 5  # 允许±5的误差

    # 计算范围边界
    lower_bound = 120 #target_gray - tolerance  # 190
    upper_bound = 200 #target_gray + tolerance  # 200

    # 检查每个通道是否在目标范围内
    in_range = (
        lower_bound <= avg_b <= upper_bound and
        lower_bound <= avg_g <= upper_bound and
        lower_bound <= avg_r <= upper_bound
    )

    # 检查三个通道是否接近（最大差异小）
    max_diff = max(abs(avg_b - avg_g), abs(avg_g - avg_r), abs(avg_r - avg_b))
    is_close = max_diff < tolerance

    # 如果是接近(195,195,195)的灰色，按钮不可点击
    is_disabled_gray = in_range and is_close

    task.log_info(f"按钮左侧区域颜色: B={avg_b:.1f}, G={avg_g:.1f}, R={avg_r:.1f}, "
                  f"是否禁用灰色={is_disabled_gray} (范围{lower_bound}-{upper_bound}, 最大差异={max_diff:.1f})")

    # 如果是禁用灰色，按钮不可点击；否则可点击
    return not is_disabled_gray


# def group_dialog_columns(task: TriggerTask, region, max_width_ratio=0.25, align_tolerance=0.04):
#     """把区域内文本框按左边缘聚成对话框列。"""
#     x1, y1, x2, y2 = region
#     boxes = [
#         box for box in task.all_texts
#         if x1 <= (box.x + box.width / 2) / task.width <= x2
#         and y1 <= (box.y + box.height / 2) / task.height <= y2
#         and box.width / task.width <= max_width_ratio
#         and len(box.name) > 2
#     ]
#     columns = []
#     for box in sorted(boxes, key=lambda item: item.x):
#         left = box.x / task.width
#         center_x = (box.x + box.width / 2) / task.width
#         if columns and left - columns[-1]["left"] <= align_tolerance:
#             columns[-1]["centers"].append(center_x)
#             columns[-1]["texts"].append(box.name)
#         else:
#             columns.append({"left": left, "centers": [center_x], "texts": [box.name]})
#     return [
#         {"x": sum(column["centers"]) / len(column["centers"]), "texts": column["texts"]}
#         for column in columns
#     ]


# ------------------------- 帧卡住检测 -------------------------

# 两次卡住检查间隔超过这么多秒，说明任务被禁用/暂停过，或长时间没有帧走到这里：
# 中间的画面有没有变过无从得知，接着旧计时算「卡住」会误报
_STUCK_CHECK_GAP = 5


def is_frame_stuck(task: TriggerTask, stuck_threshold_seconds=30, change_threshold=0.08):
    """
    基于像素变化检测画面是否卡住。
    在 task 上缓存 _prev_frame_gray 和 _last_change_time。
    连续 stuck_threshold_seconds 秒变化比例低于 change_threshold 返回 True。
    stuck_threshold_seconds: 判定卡住的连续秒数阈值，默认30秒
    change_threshold: 两帧之间变化像素比例阈值，默认0.08（8%）
    """
    if not hasattr(task, '_last_change_time'):
        task._last_change_time = time.time()
        task._prev_frame_gray = None

    now = time.time()
    last_check = getattr(task, '_stuck_check_at', 0.0)
    task._stuck_check_at = now
    if now - last_check > _STUCK_CHECK_GAP:
        # 实跑 10/05 10:02：任务被禁用 1.5 分钟再启用，重启后第一次检查的画面（刚点开的信息统计页面）
        # 和禁用前最后一帧（同一个信息统计页面）几乎一样，中间 95 秒的空白被当成「画面95秒没变」，
        # 卡住兜底每帧抢先把信息页关掉，和「获取主战员头像」来回循环
        task._last_change_time = now
        task._prev_frame_gray = None

    frame = task.frame
    if frame is None:
        return False

    # 缩放灰度图以减少计算量
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (w // 4, h // 4))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    if task._prev_frame_gray is not None and gray.shape == task._prev_frame_gray.shape:
        diff = cv2.absdiff(gray, task._prev_frame_gray)
        _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
        change_ratio = cv2.countNonZero(thresh) / (gray.shape[0] * gray.shape[1])

        if change_ratio >= change_threshold:
            task._last_change_time = time.time()

    task._prev_frame_gray = gray

    return time.time() - task._last_change_time >= stuck_threshold_seconds


def handle_stuck_log(task: TriggerTask):
    """画面卡住超过10秒时依次处理关闭页、特殊怪物、卡牌或未知页面。"""
    if not is_frame_stuck(task, stuck_threshold_seconds=10):
        return False

    stuck_seconds = int(time.time() - task._last_change_time)
    if stuck_seconds >= 60 and getattr(task, "_stuck_reported", None) != task._last_change_time:
        # 实跑中出现过战斗里读不到手牌数、画面一动不动 55 分钟，事后不知道是什么页面：每次卡住留一张截图和画面文字
        task._stuck_reported = task._last_change_time
        battle_log.anomaly(task, "画面卡住", f"画面已 {stuck_seconds} 秒没有变化",
                           texts=[box.name for box in (getattr(task, "all_texts", None) or [])][:120])

    # 全员死亡后的软锁（见 _battle_wipe_locked）：点场地中间（_esc_fallback）对拿在手里的牌有用，
    # 对软锁没用——实跑点了几百次画面一动不动，只有右上角菜单的「撤退」能出来。距上次请求不到 30 秒
    # 就不重复点，给 handle_escape 留出点「撤退」的时间（那个窗口也是 30 秒）。
    if (stuck_seconds >= _BATTLE_WIPE_RETREAT_AFTER and _battle_wipe_locked(task)
            and time.time() - getattr(task, "_escape_requested_at", 0) > _ESCAPE_INTENT_SECONDS):
        task.log_info(f"画面卡住已持续{stuck_seconds}秒，我方血条紫色空掉（全员死亡后软锁），点右上角菜单撤退")
        battle_log.record(task, "全灭撤退", reason=f"全员死亡后画面卡死{stuck_seconds}秒")
        _open_escape_menu(task, 0.053)
        return True

    close_page = task.find_one(
        feature_name="close_page",
        box=task.box_of_screen(0.921, 0.003, 0.998, 0.100),
    )
    if close_page:
        task.log_info(
            f"画面卡住已持续{stuck_seconds}秒，检测到close_page特征，点击关闭页面"
        )
        task.click_box(close_page)
        task.sleep(1)
        return True

    from utils_sortie import handle_secret_enemy
    handle_secret_enemy(task)
    cards = recognize_cards(task, page="画面卡住兜底")
    if cards:
        chosen_card = random.choice(cards)
        task.log_info(
            f"画面卡住兜底: 随机点击卡牌「{chosen_card['name']}」"
        )
        _move_and_click(task, chosen_card["x"], chosen_card["y"])
    elif not handle_unknown_page(task) and stuck_seconds >= _ESC_FALLBACK_AFTER:
        _esc_fallback(task, f"画面卡住已持续{stuck_seconds}秒")
    # 普通随机点屏幕兜底暂时停用。
    # click_x = random.uniform(0.059, 0.985)
    # click_y = random.uniform(0.129, 0.981)
    # _move_and_click(task, click_x, click_y)
    task.log_info(f"画面卡住，已持续{stuck_seconds}秒")
    return False


# ------------------------- 页面处理函数（通用） -------------------------
# 约定: 每个函数处理一种页面, 处理成功返回 True, 未命中返回 False。

def handle_auto_stop(task: TriggerTask):
    """自动停止功能: 如果配置"几轮后停止(0为不停止)"不为0，
    且 node_status 中的 total_rounds 达到配置轮数，则自动 disable 当前任务。"""
    stop_rounds = _get_config_value(task, '几轮后停止(0为不停止)', 0)
    if stop_rounds and stop_rounds != 0:
        ns = getattr(task, 'node_status', None)
        if ns and ns.get('total_rounds', 0) >= stop_rounds:
            task.log_info(f"已达到配置的停止轮数 {stop_rounds}，当前 total_rounds={ns['total_rounds']}，自动停止任务")
            task.disable()
            return True
    return False


def log_credit(task: TriggerTask):
    """记录当前信用点数量（仅记录, 不拦截后续处理）。"""
    credit = _get_current_credit(task)
    if credit > 0:
        task.info_set("当前信用点", f"{credit}")
    return False


# def handle_stage_clear(task: TriggerTask):
#     """成功通关页面: 检测(0.142,0.806)处文本是否包含'战斗结束'，成功次数+1。"""
#     box = find_box_at_point(task, 0.142, 0.806)
#     if box and "战斗结束" in box.name:
#         task.log_info("检测到成功通关页面，success_rounds + 1")
#         if hasattr(task, 'node_status'):
#             task.node_status['success_rounds'] += 1
#     return False


def log_node_status(task: TriggerTask):
    """记录当前胜率（仅记录, 不拦截后续处理）；顺带告诉详细日志这一帧是不是战斗画面（记战斗开始/结束）。"""
    hand = find_box_at_point(task, 0.512, 0.969)
    in_battle = bool(hand and re.search(r'\d+/10', hand.name))
    if not in_battle and any("所有牌堆" in b.name for b in task.all_texts):
        # 战斗中从所有牌堆选牌的页面没有手牌栏，卡久了会误记「战斗结束」（10/08 15:42 实况）
        counter = find_box_at_point(task, 0.5, 0.168)
        in_battle = bool(counter and re.search(r'\d+\s*/\s*\d+', counter.name))
    battle_log.battle_frame(task, in_battle)
    ns = getattr(task, 'node_status', None)
    if ns:
        try:
            from src.config import version
            app_version = str(version).strip() or "dev"
        except Exception:
            app_version = "dev"
        task.info_set("版本号", app_version)
        task.info_set("游戏语言", _get_game_language(task))
        total = ns.get('total_rounds', 0)
        node_count = ns.get('node_count', 0)
        node_type = ns.get('node_type', "")
        task.info_set("所处层数，节点，类型", f"第{ns['pass_final_boss_count']+1}层，第{node_count}节点，{node_type}")
        task.info_set("是否到达关底boss", f"{ns['reach_final_boss']}")
        task.info_set("是否进入关底boss战斗", f"{ns['final_boss_battle']}")
        task.info_set("是否已逃脱", f"{ns['is_escaped']}")
        task.info_set(
            "是否已获得特定闪光",
            ns.get("get_specific_flash", False),
        )
        task.info_set(
            "获取刷存档主战员头像",
            ns.get("save_target_member", False),
        )
        task.info_set("本局已移除卡牌", ns.get("removed_card_count", 0))
        task.info_set("本局已获得中立牌", ns.get("neutral_card_count", 0))
        equipment = _equipment_state(task)
        equipment_names = [
            _current_equipment_for_slot(task, equipment, slot)[0]
            for slot in range(3)
        ]
        task.info_set(
            "装备信息",
            "，".join(
                f"{slot + 1}号位：{name or '空'}"
                for slot, name in enumerate(equipment_names)
            ),
        )
        meditation_state = _member_deck_state(task).get("冥想", {})
        if isinstance(meditation_state, dict):
            for card_name, pending in meditation_state.items():
                task.info_set(f"冥想：{card_name}", pending)
        if total > 0:
            task.info_set("当前胜率", f"{ns['success_rounds']}/{total} ({ns['success_rounds']*100//total}%)")
        else:
            task.info_set("当前胜率",f"{ns['success_rounds']}/{total} NaN")
        task.log_info("")
    return False


_ESC_FALLBACK_AFTER = 20  # 没有处理函数认领画面 / 画面卡住且别的兜底都没动作，持续这么多秒就兜底一次
_ESC_FALLBACK_GAP = 10    # 两次兜底至少间隔这么久，给页面留出响应时间
_BATTLE_WIPE_RETREAT_AFTER = 60  # 战斗页面 + 我方血条紫色空掉又卡住这么久：全员死亡后软锁，点右上角菜单撤退
_CARD_HELD_POINT = (0.5, 0.45)           # 战斗里「把拿在手里的牌放下」的点击位置（和拖牌打空地的落点一致）
_CARD_HELD_TEXT = re.compile(r"\d+/10")  # 手牌数「N/10」就压在战斗页下方中央，读到它说明是战斗页面
# 卡死兜底时按文字找的安全按钮：点一下只会推进/关闭页面，不会消费或做不可逆操作（命运结算页的「跳过」不在内）
_ESC_SAFE_BUTTONS = ("离开", "关闭", "继续", "下一步", "返回")


def _on_battle_page(task: TriggerTask) -> bool:
    """画面是战斗页面（手牌数「N/10」压在下方中央）时返回 True。
    牌的拖动松手消息丢了、或数字键选中后回车没跟上时，牌会一直拿在手里、结束回合按钮变灰，见 _esc_fallback。"""
    box = find_box_at_point(task, 0.509, 0.972)
    return bool(box and _CARD_HELD_TEXT.search(box.name))


def _battle_wipe_locked(task: TriggerTask) -> bool:
    """全员死亡后游戏不再推进的软锁：画面还是战斗页面（手牌数压着），但我方血条已经变紫、绿色全空。
    实跑 10/04 10:11 战斗 14（精英尼希隆）：打完最后一手后敌方回合打死全队，游戏就停在战斗画面
    （结束回合按钮变灰、点场地中间几百次都没用），用户 13 分钟后从右上角菜单手动撤退才出来。
    血条用 utils_battle 那套判断（弹窗盖住血条只算「空」不算「紫」）：只有真 0 血才是两行都紫。"""
    frame = getattr(task, "frame", None)
    if frame is None:
        return False
    if not _on_battle_page(task):
        return False
    from utils_battle import hp_bar_collapsed, hp_bar_empty
    return hp_bar_empty(frame) and hp_bar_collapsed(frame)


def _esc_fallback(task: TriggerTask, reason: str) -> bool:
    """没见过的页面、卡住的页面最后按 ESC 兜底（多数弹窗/子页面 ESC 就能关掉或返回）。
    战斗页面除外：那里按 ESC 只会打开撤退菜单，关掉后还是原来那样，来回循环（实跑 10/02 15:15 这样卡了 103 分钟）。
    战斗里卡住多半是有一张牌还拿在手里，点一下场地中间就是把牌放下。
    ESC 前先在底部按文字找安全按钮：命运结算页的按钮有高/低两套渲染、探针全落空时 ESC 完全无效，
    实跑白按了 2.5 小时（10/03 11:11）。"""
    now = time.time()
    if now - getattr(task, "_esc_fallback_at", 0) < _ESC_FALLBACK_GAP:
        return False
    task._esc_fallback_at = now
    if _on_battle_page(task):
        task.log_info(f"{reason}，战斗页面：点场地中间把拿在手里的牌放下")
        battle_log.record(task, "放下卡牌", reason=reason)
        _move_and_click(task, *_CARD_HELD_POINT)
        return True
    button = find_button_by_text(task, _ESC_SAFE_BUTTONS)
    if button:
        task.log_info(f"{reason}，点击「{button.name}」")
        battle_log.record(task, "兜底按钮", reason=reason, button=button.name)
        task.click_box(button)
        task.sleep(1)
        return True
    task.log_info(f"{reason}，按 ESC 兜底")
    battle_log.record(task, "ESC兜底", reason=reason)
    task.send_key("esc")
    task.sleep(1)
    return True


def check_loop(task: TriggerTask, handler: str):
    """每帧有处理函数动作后调用（加速补丁的 _gated_run 里）。几个处理函数来回动作、长时间没有推进时分级处理：
    第 1 次判定先记异常（截图 + 画面文字），卡厄思模式顺带重新获取刷存档主战员头像（循环常因开局头像没取准）；
    之后每次按 ESC 兜底。实跑中商店 ↔ 购买页、灰色事件选项、「今天不再显示」弹窗都这样循环过。"""
    hit = battle_log.loop_frame(task, handler)
    if hit is None:
        return
    level, kinds = hit
    reason = f"疑似循环：已 {battle_log.loop_seconds(task):.0f} 秒没有推进，动作只来自 {'、'.join(kinds)}"
    if level == 1:
        battle_log.anomaly(task, "疑似循环", reason, handlers=kinds,
                           texts=[box.name for box in (getattr(task, "all_texts", None) or [])][:120])
        if "刷存档主战员" in getattr(task, "default_config", {}) and hasattr(task, "node_status"):
            task.log_info(f"{reason}，重新获取刷存档主战员头像")
            task.node_status["save_target_member"] = False
            return
    _esc_fallback(task, reason)


def log_unhandled_page(task: TriggerTask):
    """放在 PAGE_HANDLERS 最后：走到这里说明这一帧没有处理函数认领，连续 10 秒就记一次「未识别页面」（截图 + 全部文字），
    连续 20 秒按 ESC 兜底。"""
    battle_log.unhandled_frame(task)
    seconds = battle_log.unhandled_seconds(task)
    if seconds >= _ESC_FALLBACK_AFTER:
        return _esc_fallback(task, f"已 {seconds:.0f} 秒没有页面处理函数认领画面")
    return False


def handle_battle_crash(task: TriggerTask):
    """战斗信息错乱 / 点击重试 / 发生未知错误: 点击屏幕中央恢复。
    「发生未知错误。err:card_available_failed:...  请点击画面」：出击模式出牌时游戏偶尔弹出，实跑中没人认领卡了 30 秒。"""
    if (find_text(task, r'出现错乱')
            or find_text(task, r'点击重试')
            or find_text(task, r'通讯不稳定.*重新尝试')
            or find_text(task, r'发生未知错误')):
        task.log_info("战斗信息出现错乱，点击恢复")
        _move_and_click(task, 0.5, 0.5)
        return True
    return False


def handle_close_page(task: TriggerTask):
    """提示"点击屏幕事件": 点击屏幕。"""
    box = find_text(task, _get_game_text(task, '点击屏幕'))
    if box:
        task.log_info("点击屏幕事件，点击屏幕")
        task.click_box(box)
        return True
    return False


def handle_refine_equipment_credit(task: TriggerTask):
    """提炼装备信用点页面：点击“以信用点接收”。"""
    box = find_box_at_point(task, 0.598, 0.635)
    receive_text = _get_game_text(task, "以信用点接收")
    if box and receive_text in box.name:
        task.log_info(f"检测到提炼装备信用点页面，点击{receive_text}")
        task.click_box(box)
        task.sleep(0.5)
        return True
    return False


def _is_confirm_text(name):
    """文字框是不是「确认」按钮。事件选项描述「确认【盗猎者的乐趣】资讯」常被 OCR 切出一个「确认【」框，
    _clean_match 去掉括号后也等于「确认」（实跑 10/01 10:05 一直点这个灰色选项）：带括号的不算。"""
    return _clean_match(name, "确认") and not re.search(r"[【】「」『』\[\]]", name)


_DONT_SHOW_AGAIN = ("不再显示", "不再顯示", "不再显", "不再顯")
_CHECKBOX_LEFT = 0.029    # 勾选框中心在「今天不再显示」文字左缘的左侧


def _checkbox_checked(task: TriggerTask, x, y):
    """勾选框勾上后填成橙色，中间对勾是白的。周围橙色够多才算已勾上；读不到画面返回 None。"""
    frame = getattr(task, "frame", None)
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    px, py = int(x * width), int(y * height)
    half = max(4, int(0.012 * width))
    region = frame[max(0, py - half):min(height, py + half), max(0, px - half):min(width, px + half), :3]
    if region.size == 0:
        return None
    blue = region[:, :, 0]
    red = region[:, :, 2]
    orange = (red > 170) & (blue < 100) & (red.astype(int) > blue.astype(int) + 60)
    return float(orange.mean()) >= 0.2


def handle_dont_show_again(task: TriggerTask):
    """带「今天不再显示 / 下次登入前不再显示」勾选框的确认框（分解存档资料、获得神之灵光一闪的卡牌、确认准备战斗说明等）：
    没勾上就先勾上，再点下方的「确认」或「进入」。已勾上再点会取消，所以先看框是不是橙色。"""
    label = next((box for box in task.all_texts
                  if any(needle in box.name for needle in _DONT_SHOW_AGAIN)), None)
    if label is None:
        return False
    x = label.x / task.width - _CHECKBOX_LEFT
    y = (label.y + label.height / 2) / task.height
    checked = _checkbox_checked(task, x, y)
    if checked is None:
        task.log_info(f"「{label.name}」读不到勾选框颜色，不点击，避免把已勾选取消")
    elif not checked:
        task.log_info(f"勾选「{label.name}」")
        _move_and_click(task, x, y)
        task.sleep(0.4)
    # 按钮有的写「确认」，有的写「进入」（确认准备战斗说明）。找不到时不占这一帧，交给后面的处理函数
    # （实跑 10/01 10:53：只认「确认」，每帧都返回 True，画面卡住 20 秒后被 ESC 关掉再重开，一直循环）
    confirm = next((box for box in task.all_texts if box.y > label.y
                    and (_is_confirm_text(box.name) or _clean_match(box.name, "进入"))), None)
    if confirm is None:
        return checked is False
    task.log_info(f"「{label.name}」确认框：点击「{confirm.name}」")
    task.click_box(confirm)
    task.sleep(1)
    return True


def handle_center_confirm(task: TriggerTask):
    """页面中央的"确认"按钮。"""
    confirm_region = (0.009, 0.168, 0.977, 0.875)
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _is_confirm_text(text_box.name)
            and confirm_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= confirm_region[2]
            and confirm_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= confirm_region[3]
        ),
        None,
    )
    if box:
        task.log_info("检测到页面中央确认按钮，点击确认")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_settlement(task: TriggerTask):
    """"结算"按钮。"""
    box = find_box_at_point(task, 0.941, 0.917)
    if box and _clean_match(box.name, "结算"):
        _move_and_click(task, 0.941, 0.917)
        if hasattr(task, 'node_status') and task.node_status.get('reach_final_boss', False):
            task.node_status['pass_final_boss_count'] += 1
            passed = task.node_status['pass_final_boss_count']
            task.log_info(f"检测到boss结算页面且 reach_final_boss=True，通关层数+1 (当前: {passed})")
            reset_layer_status(task)
        task.sleep(1)
        return True
    return False


def handle_skip(task: TriggerTask):
    """"跳过"按钮。"""
    box = find_box_at_point(task, 0.941, 0.917)
    if box and _clean_match(box.name, "跳过"):
        task.log_info("跳过页面触发跳过事件，点击「跳过」按钮")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_destiny_choice(task: TriggerTask):
    """命运选择奖励页面: 随机选择一个命运标题。"""
    box = find_box_at_point(task, 0.499, 0.932)
    if box and _get_game_text(task, '请选择你的命运') in box.name:
        task.log_info("检测到命运选择奖励，进行相应操作")
        task.sleep(2)  # 给按钮一些加载时间

        # # 检查确认按钮是否已处于激活状态
        # # 在确认按钮点击位置附近查找"确认"文本
        # confirm_box = find_box_at_point(task, 0.884, 0.931)
        # if confirm_box and confirm_box.name == "确认":
        #     if is_button_active(task, confirm_box):
        #         task.log_info("确认按钮已激活，跳过选择（由其他逻辑处理确认）")
        #         return False  # 按钮已激活，不处理，让其他逻辑点击确认
        # 在命运标题区域随机选择一个
        titles = [
            b for b in task.all_texts
            if 0.202 <= (b.x + b.width / 2) / task.width <= 0.800
            and 0.474 <= (b.y + b.height / 2) / task.height <= 0.600
            and len(b.name.strip()) > 1
            and b.name not in ["确认", "返回", "跳过"]
        ]
        if titles:
            chosen = random.choice(titles)
            task.log_info(f"随机选择命运: {chosen.name}")
            task.click_box(chosen)
            task.sleep(1)
            # 选择命运后不点击确认按钮，返回False让其他逻辑处理
            return True
    return False


def _prioritize_target_member_click(task: TriggerTask, click_positions, search_region):
    """匹配目标成员头像，并将距离最近的候选点击位置移到最前。"""
    if not click_positions:
        task.log_info("刷存档主战员匹配失败：没有可绑定的候选点击位置")
        return click_positions
    if not task.feature_exists("target_member_large"):
        task.log_info(
            "刷存档主战员匹配跳过：尚未保存target_member_large头像特征，"
            "保持原主战员点击顺序"
        )
        return click_positions
    target_member = task.find_one(
        feature_name="target_member_large",
        box=task.box_of_screen(*search_region),
        threshold=0.35,
    )
    if not target_member:
        task.log_info(
            "刷存档主战员头像匹配失败："
            f"在区域{search_region}内未找到target_member_large（阈值=0.35），"
            "保持原主战员点击顺序"
        )
        return click_positions

    center_x = (target_member.x + target_member.width / 2) / task.width
    center_y = (target_member.y + target_member.height / 2) / task.height
    target_position = min(
        click_positions,
        key=lambda position: (
            (position[0] - center_x) ** 2 + (position[1] - center_y) ** 2
        ),
    )
    task.log_info(
        f"刷存档主战员头像匹配成功，相似度={target_member.confidence:.4f}，"
        f"绑定点击位置{target_position}并设为最高优先级"
    )
    return [target_position] + [
        position for position in click_positions if position != target_position
    ]


def handle_main_member_flash(task: TriggerTask):
    """主战员闪光选择页面: 依次尝试主战员，直到出现确认特征。"""
    box = find_box_at_point(task, 0.495, 0.936)
    if not (box and _get_game_text(task, "请选择获得") in box.name):
        return False

    task.log_info("检测主战员闪光选择，进行相应操作")
    confirm_box = task.box_of_screen(0.145, 0.044, 0.856, 0.214)
    click_positions = [(0.228, 0.510), (0.504, 0.504), (0.755, 0.508)]
    click_positions = _prioritize_target_member_click(
        task, click_positions, (0.173, 0.232, 0.858, 0.508)
    )

    for cx, cy in click_positions:
        task.log_info(f"点击位置({cx}, {cy})")
        _move_and_click(task, cx, cy)
        task.sleep(0.5)

        feature = task.wait_feature(
            "flashmemberconfirm",
            box=confirm_box,
            threshold=0.7,
            time_out=2,
        )
        if feature:
            task.log_info(
                f"点击位置({cx}, {cy})后成功找到flashmemberconfirm"
            )
            return True
        task.log_info(
            f"点击位置({cx}, {cy})后未找到flashmemberconfirm，继续尝试"
        )

    task.log_info("所有尝试均未找到flashmemberconfirm")
    return True


def handle_card_reward(task: TriggerTask):
    """卡牌奖励页面: 按类型特征识别卡牌，并按优先级选择。"""
    page_title = _get_region_text(task, (0.345, 0.012, 0.642, 0.141))
    if "卡牌奖励" not in page_title:
        return False

    task.log_info("检测到卡牌奖励页面")
    cards = recognize_cards(task, page="卡牌奖励页面")
    if not cards:
        task.log_info("卡牌奖励页面未识别到卡牌，等待下一轮处理")
        return True

    target_boxes, target_click_positions = find_target_card(task)
    if target_boxes:
        click_position = target_click_positions[0]
        task.log_info(
            f"卡牌奖励页面: 检测到target卡牌，点击位置{click_position}"
        )
        battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen="target", reason="target 卡牌特征")
        _move_and_click(task, *click_position)
        return True

    priority = _get_card_reward_priority(task)

    initial_card_name = _get_config_value(task, "刷初始卡牌", "")
    initial_card_name = initial_card_name.strip() if isinstance(initial_card_name, str) else ""
    node_status = getattr(task, "node_status", {})
    is_initial_node = (
        node_status.get("pass_final_boss_count", 0) == 0
        and node_status.get("node_count", 0) == 0
    )
    if initial_card_name and is_initial_node:
        initial_card = next(
            (card for card in cards if initial_card_name in card["name"]),
            None,
        )
        if initial_card:
            task.log_info(
                f"刷初始卡牌命中「{initial_card_name}」，点击该卡牌"
            )
            battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen=initial_card["name"],
                              reason=f"刷初始卡牌「{initial_card_name}」")
            _move_and_click(task, initial_card["x"], initial_card["y"])
            task.sleep(1)
            return True
        task.log_info(
            f"刷初始卡牌未找到「{initial_card_name}」，点击ESC重新开始"
        )
        battle_log.reroll(task, f"刷初始卡牌「{initial_card_name}」", cards=[c["name"] for c in cards])
        _move_and_click(task, 0.960, 0.053)
        task.sleep(1)
        return True

    chosen_card = None
    for pri_name in priority:
        chosen_card = next(
            (
                card for card in cards
                if pri_name
                and pri_name in card["name"]
                and _edit_distance(pri_name, card["name"], max_dist=1)
            ),
            None,
        )
        if chosen_card:
            task.log_info(
                f"按优先级选择卡牌: {chosen_card['name']}（配置: {pri_name}）"
            )
            break

    if chosen_card is None:
        refresh_boxes = []
        for box in task.all_texts:
            center_x = (box.x + box.width / 2) / task.width
            center_y = (box.y + box.height / 2) / task.height
            if not (
                0.105 <= center_x <= 0.903
                and 0.764 <= center_y <= 0.851
            ):
                continue
            match = re.fullmatch(r"\s*(\d)\s*/\s*3\s*", box.name)
            if match and int(match.group(1)) != 0:
                refresh_boxes.append((box, int(match.group(1)), 3))
        if refresh_boxes:
            for refresh_box, remaining, maximum in refresh_boxes:
                task.log_info(
                    f"卡牌奖励页面未命中优先级卡牌，"
                    f"点击刷新次数「{remaining}/{maximum}」刷新卡牌"
                )
                battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen="刷新",
                                  reason=f"未命中优先级 {priority}，剩余刷新 {remaining}/{maximum}")
                task.click_box(refresh_box)
            return True

    if chosen_card is None and cards:
        task.log_info("未命中优先级卡牌，跳过非优先级卡牌")
        battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen="跳过",
                          reason=f"未命中优先级 {priority}，没有刷新次数")
        # 在区域(0.620,0.883,0.990,0.983)内查找包含"跳过"的box并点击
        skip_box = next((b for b in task.all_texts
                         if 0.620 <= (b.x + b.width / 2) / task.width <= 0.990
                         and 0.883 <= (b.y + b.height / 2) / task.height <= 0.983
                         and "跳过" in b.name), None)
        if skip_box:
            task.log_info("卡牌奖励页面触发跳过事件，点击「跳过」按钮")
            task.click_box(skip_box)
        else:
            task.log_info("未找到跳过按钮，点击固定位置")
            _move_and_click(task, 0.745, 0.933)
        task.sleep(0.5)
        return True

    if chosen_card:
        task.log_info(f"卡牌奖励页面触发选卡事件，点击「{chosen_card['name']}」")
        battle_log.record(task, "卡牌奖励", cards=[c["name"] for c in cards], chosen=chosen_card["name"],
                          reason="卡牌奖励优先级")
        _move_and_click(task, chosen_card["x"], chosen_card["y"])
        task.sleep(1)
        return True
    return False


_EQUIPMENT_TYPE_SLOTS = {"攻击力": 0, "防御力": 1, "生命值": 2}
# 游戏里的品质：蓝底稀有、橙底传说、紫底独特（最高品质，每个主战员只能装一件）
_EQUIPMENT_QUALITY_RANKS = {"": 0, "稀有": 1, "传说": 2, "独特": 3}
_EQUIPMENT_QUALITY_RETRIES = 3   # 待安装装备读不出品质（卡片还在淡入，读成暗灰）时最多重读几帧，之后按稀有处理
_EQUIPMENT_DECISION_KEEP = 10    # 同一件装备这么多秒内再看到，沿用上次选的主战员（页面会停好几帧）


def _equipment_slot(task: TriggerTask, type_text):
    """根据装备类型文本返回 equipment 下标，无法识别时返回 None。"""
    return next((slot for equipment_type, slot in _EQUIPMENT_TYPE_SLOTS.items()
                 if _get_game_text(task, equipment_type) in type_text), None)


def _equipment_priority(task: TriggerTask, slot):
    """读取指定装备位的优先级配置。"""
    priority = _get_config_value(task, f"装备{slot + 1}号位优先级", [])
    return list(priority) if isinstance(priority, (list, tuple)) else []


def _match_equipment_name(ocr_name, priority):
    """用双向包含匹配装备名，返回配置中的标准名称及优先级下标。"""
    if not ocr_name:
        return None, None
    for index, config_name in enumerate(priority):
        if not config_name:
            continue
        if ocr_name in config_name or config_name in ocr_name:
            return config_name, index
    return None, None


def _equipment_rank(name, priority):
    """返回已记录装备的优先级下标，未命中配置时排在配置装备之后。"""
    _, rank = _match_equipment_name(name, priority)
    return rank if rank is not None else len(priority)


def _equipment_state(task: TriggerTask):
    """获取并修正目标主战员的装备状态字典。"""
    member_status = getattr(task, "member_status", None)
    if not isinstance(member_status, dict):
        member_status = _initial_member_status()
        task.member_status = member_status
    equipment = member_status.setdefault("equipment", {})
    if not isinstance(equipment, dict):
        equipment = {
            "names": ["", "", ""],
            "descriptions": ["", "", ""],
            "qualities": ["", "", ""],
        }
        member_status["equipment"] = equipment
    elif "names" not in equipment or "descriptions" not in equipment:
        old_equipment = equipment
        equipment = {
            "names": ["", "", ""],
            "descriptions": ["", "", ""],
            "qualities": ["", "", ""],
        }
        for equipment_name, description in old_equipment.items():
            for slot in range(3):
                if _match_equipment_name(
                    equipment_name,
                    _equipment_priority(task, slot),
                )[0]:
                    equipment["names"][slot] = equipment_name
                    equipment["descriptions"][slot] = description
                    break
        member_status["equipment"] = equipment
    for key in ("names", "descriptions", "qualities"):
        values = equipment.get(key)
        if not isinstance(values, list):
            values = []
        equipment[key] = (values + ["", "", ""])[:3]
    if not isinstance(member_status.get("deck"), dict):
        member_status["deck"] = {}
    return equipment


def _member_deck_state(task: TriggerTask):
    """获取并修正目标主战员的卡组状态字典。"""
    member_status = getattr(task, "member_status", None)
    if not isinstance(member_status, dict):
        member_status = _initial_member_status()
        task.member_status = member_status
    deck = member_status.setdefault("deck", {})
    if not isinstance(deck, dict):
        deck = {}
        member_status["deck"] = deck
    return deck


def _reset_meditation_state(task: TriggerTask):
    """按当前配置重建目标主战员的冥想状态。"""
    configured_cards = _get_config_value(task, "需要冥想的卡牌", [])
    if not isinstance(configured_cards, (list, tuple)):
        configured_cards = []

    meditation_state = {}
    for card_name in configured_cards:
        normalized_name = str(card_name).strip()
        if normalized_name and normalized_name not in meditation_state:
            meditation_state[normalized_name] = False

    _member_deck_state(task)["冥想"] = meditation_state


def _matching_meditation_card_names(task: TriggerTask, cards):
    """返回与识别卡牌名称匹配的冥想配置名称。"""
    meditation_state = _member_deck_state(task).get("冥想", {})
    if not isinstance(meditation_state, dict):
        return []

    matched_names = []
    for configured_name in meditation_state:
        for card in cards:
            recognized_name = str(card.get("name", "")).strip()
            if (
                recognized_name
                and (configured_name in recognized_name or recognized_name in configured_name)
                and _edit_distance(configured_name, recognized_name, max_dist=1)
            ):
                matched_names.append(configured_name)
                break
    return matched_names


def _current_equipment_for_slot(task: TriggerTask, equipment, slot):
    """按槽位读取当前装备名称及其优先级。"""
    priority = _equipment_priority(task, slot)
    names = equipment.get("names", [])
    equipment_name = names[slot] if slot < len(names) else ""
    if not equipment_name:
        return "", len(priority)
    return equipment_name, _equipment_rank(equipment_name, priority)


def _pixel_rgb(task: TriggerTask, point):
    """读取归一化坐标的像素，并将 OpenCV BGR 转为 RGB。"""
    if task.frame is None:
        return None
    x = min(task.width - 1, max(0, round(point[0] * task.width)))
    y = min(task.height - 1, max(0, round(point[1] * task.height)))
    blue, green, red = (int(value) for value in task.frame[y, x, :3])
    return red, green, blue


def _quality_from_rgb(rgb):
    """按底色判断品质：灰暗没有颜色返回 ""，蓝稀有、紫独特、橙传说，其余颜色认不出返回 None。
    实跑读数：稀有 (72, 89, 160)、传说 (165, 110, 86)、独特 (136, 96, 184)；成员装备格上的紫更暗，
    实测 (135, 82, 163)——蓝只比红多 28，单靠「蓝>红+30」读不出来（10/03 之前「独特」一次都没读出过），
    所以紫单独判：红蓝都明显高于绿、且蓝多于红。"""
    red, green, blue = rgb
    if max(rgb) - min(rgb) < 30:
        return ""
    if red > green + 15 and blue > green + 30 and blue > red:
        return "独特"
    if blue > red + 30:
        return "稀有"
    if red > blue + 40 and red >= green:
        return "传说"
    return None


def _equipment_quality_at(task: TriggerTask, point):
    """按指定点颜色识别待安装装备的品质；读不出（卡片还在淡入时是暗灰）返回 None。"""
    rgb = _pixel_rgb(task, point)
    if rgb is None:
        return None, None
    return _quality_from_rgb(rgb) or None, rgb


def _slot_quality(task: TriggerTask, center):
    """主战员装备格的品质：取格子右上角一小块的平均色（左上角是类型徽章、中间是装备图、底部是星星，
    右上角只有底色）。蓝底稀有、橙底传说、紫底独特，灰暗没有颜色是空槽（空格半透明，底下可能透出立绘），
    认不出的颜色返回 None。"""
    if task.frame is None:
        return None, None
    h, w = task.frame.shape[:2]
    x1, x2 = int((center[0] + 0.012) * w), int((center[0] + 0.026) * w)
    y1, y2 = int((center[1] - 0.050) * h), int((center[1] - 0.038) * h)
    patch = task.frame[max(0, y1):max(1, y2), max(0, x1):max(1, x2), :3]
    if patch.size == 0:
        return None, None
    blue, green, red = (int(v) for v in patch.reshape(-1, 3).mean(axis=0))
    rgb = (red, green, blue)
    return _quality_from_rgb(rgb), rgb


def _member_equipment_qualities(task: TriggerTask, level_box):
    """根据等级文本的相对位置读取该主战员三个装备槽的品质。"""
    level_center_x = (level_box.x + level_box.width / 2) / task.width
    level_center_y = (level_box.y + level_box.height / 2) / task.height
    # 按 2145×1207 实跑截图量的装备格中心：与「等级」标签同高（原来往上偏了 0.0655，取到了格子上方的卡片背景）
    relative_offsets = (
        (0.130, 0.0),
        (0.200, 0.0),
        (0.270, 0.0),
    )
    qualities, readings = [], []
    for slot, (offset_x, offset_y) in enumerate(relative_offsets):
        point = (level_center_x + offset_x, level_center_y + offset_y)
        quality, rgb = _slot_quality(task, point)
        qualities.append(quality)
        readings.append(f"{'未知' if quality is None else quality or '空'}{rgb}")
        task.log_info(
            f"第{slot + 1}号装备位颜色RGB={rgb}，"
            f"识别品质={'未知' if quality is None else quality or '未安装'}"
        )
    task._slot_readings = readings  # 详细日志：装备分配时一起记下，方便核对取色
    # 实跑中 138 次读数全是「传说」（空槽也是），取色点多半没落在装备格上：每次运行存一张安装装备页截图用来校准
    if not getattr(task, "_equipment_page_shot", False):
        task._equipment_page_shot = True
        shot = battle_log.save_shot(task, "安装装备页", task.frame, force=True)
        if shot:
            task.log_info(f"安装装备页截图：{shot}")
    return qualities


def _should_install_equipment(task, current_name, current_quality, new_equipment):
    """先比较配置装备优先级，优先级相同时再比较品质。"""
    priority = new_equipment["priority"]
    _, current_rank = _match_equipment_name(current_name, priority)
    new_rank = new_equipment["rank"]
    if new_rank is not None or current_rank is not None:
        if new_rank is not None and (
            current_rank is None or new_rank < current_rank
        ):
            return True, "配置优先级更高"
        if current_rank is not None and (
            new_rank is None or current_rank < new_rank
        ):
            return False, "当前装备配置优先级更高"

    current_quality_rank = _EQUIPMENT_QUALITY_RANKS.get(current_quality or "", 0)
    new_quality_rank = _EQUIPMENT_QUALITY_RANKS.get(
        new_equipment.get("quality") or "", 0
    )
    return (
        new_quality_rank > current_quality_rank,
        f"品质{new_equipment.get('quality') or '未知'}"
        f"{'高于' if new_quality_rank > current_quality_rank else '不高于'}"
        f"{current_quality or '未安装'}",
    )


def _equipment_info(task: TriggerTask, name_region, type_region, description_region):
    """从指定区域读取装备名称、类型和描述，并解析槽位、品质与配置优先级。"""
    ocr_name = _get_region_text(task, name_region).strip()
    type_text = _get_region_text(task, type_region).strip()
    description = _get_region_text(task, description_region).strip()
    if not ocr_name or not type_text:
        return None
    slot = _equipment_slot(task, type_text)
    if slot is None:
        return None
    priority = _equipment_priority(task, slot)
    canonical_name, rank = _match_equipment_name(ocr_name, priority)
    quality, quality_rgb = _equipment_quality_at(task, (0.117, 0.409))
    task.log_info(f"待选装备颜色RGB={quality_rgb}，识别品质={quality or '未知'}")
    return {
        "ocr_name": ocr_name,
        "name": canonical_name or ocr_name,
        "type": type_text,
        "description": description,
        "slot": slot,
        "priority": priority,
        "rank": rank,
        "quality": quality,
    }


def _find_member_level_tags(task: TriggerTask, region, page="主战员选择页面"):
    """在指定区域识别leveltag，按位置去重并从上到下返回最多三个。"""
    level_tags = task.find_feature(
        feature_name="leveltag",
        box=task.box_of_screen(*region),
        threshold=0.7,
    ) or []
    deduplicated_level_tags = []
    for level_tag in sorted(
        level_tags,
        key=lambda feature: feature.confidence,
        reverse=True,
    ):
        level_center_x = level_tag.x + level_tag.width / 2
        level_center_y = level_tag.y + level_tag.height / 2
        if any(
            (
                (level_center_x - (kept.x + kept.width / 2)) ** 2
                + (level_center_y - (kept.y + kept.height / 2)) ** 2
            ) ** 0.5
            < max(level_tag.width, level_tag.height, kept.width, kept.height)
            for kept in deduplicated_level_tags
        ):
            continue
        deduplicated_level_tags.append(level_tag)

    kept_level_tags = sorted(
        deduplicated_level_tags,
        key=lambda feature: feature.y,
    )[:3]
    task.log_info(
        f"{page}识别到{len(level_tags)}个leveltag特征，"
        f"去重并限制后保留{len(kept_level_tags)}个"
    )
    for index, level_tag in enumerate(kept_level_tags, 1):
        task.log_info(
            f"第{index}号主战员leveltag: "
            f"中心=({(level_tag.x + level_tag.width / 2) / task.width:.4f}, "
            f"{(level_tag.y + level_tag.height / 2) / task.height:.4f})，"
            f"置信度={level_tag.confidence:.4f}"
        )
    return kept_level_tags


def _find_target_member_index(
    task: TriggerTask,
    lv_texts,
    region,
    feature_name="target_member_small",
):
    """在指定区域匹配目标成员头像，并返回距离最近的等级文本索引。"""
    if not lv_texts or not task.feature_exists(feature_name):
        return None
    target_member_box = task.find_one(
        feature_name=feature_name,
        box=task.box_of_screen(*region),
        threshold=0.4,
    )
    if not target_member_box:
        return None

    target_center_x = target_member_box.x + target_member_box.width / 2
    target_center_y = target_member_box.y + target_member_box.height / 2
    target_member_index = min(
        range(len(lv_texts)),
        key=lambda index: (
            (lv_texts[index].x + lv_texts[index].width / 2 - target_center_x) ** 2
            + (lv_texts[index].y + lv_texts[index].height / 2 - target_center_y) ** 2
        ),
    )
    task.log_info(
        f"刷存档主战员头像匹配成功，相似度={target_member_box.confidence:.4f}，"
        f"绑定第{target_member_index + 1}号主战员"
    )
    return target_member_index


def _log_equipment(task, new_equipment, member, member_index, reason, current_name, current_quality,
                   purchase, price, **fields):
    """详细日志：装备分配。target_slots 是刷存档主战员三个装备格的取色读数（品质 + RGB）。"""
    battle_log.record(
        task, "装备分配", equipment=new_equipment["name"], slot=new_equipment["slot"] + 1,
        quality=new_equipment.get("quality"), rank=new_equipment.get("rank"),
        member=member, member_index=None if member_index is None else member_index + 1, reason=reason,
        current=current_name, current_quality=current_quality,
        target_slots=getattr(task, "_slot_readings", None), purchase=purchase, price=price, **fields,
    )
    task._slot_readings = None


_TARGET_MISS_REFETCH = 2   # 购买页连续这么多次认不出刷存档主战员：重新获取主战员头像
_TARGET_MISS_GIVE_UP = 4   # 重新获取后还认不出：按 ESC 退出，这件装备本轮商店不再点


def _target_member_missing(task: TriggerTask, new_equipment):
    """购买页认不出刷存档主战员时的兜底（实跑 10/01 11:02：开局头像没取准，商店和购买页来回点了 1 分多钟）。
    返回 True 表示放弃这件装备，调用方按 ESC 退出。"""
    misses = task.node_status.get("target_member_misses", 0) + 1
    task.node_status["target_member_misses"] = misses
    if misses == _TARGET_MISS_REFETCH:
        task.log_info("购买页连续认不出刷存档主战员，重新获取主战员头像")
        battle_log.record(task, "重新获取主战员头像", equipment=new_equipment["name"], misses=misses)
        task.node_status["save_target_member"] = False
    if misses < _TARGET_MISS_GIVE_UP:
        return False
    task.log_info(f"重新获取头像后仍认不出刷存档主战员，按 ESC 退出，本轮商店不再点「{new_equipment['name']}」")
    battle_log.record(task, "放弃购买", equipment=new_equipment["name"], reason="认不出刷存档主战员")
    task.node_status.setdefault("shop_cancelled", []).append(new_equipment["ocr_name"])
    task.node_status["target_member_misses"] = 0
    return True


def _reserves_unique(task: TriggerTask):
    """写了装备优先级配置时，刷存档主战员唯一的独特名额只留给配置里的装备。"""
    return any(_equipment_priority(task, slot) for slot in range(3))


def _has_unique_elsewhere(qualities, slot):
    return any(quality == "独特" and other != slot for other, quality in enumerate(qualities))


def _recommended_member(task: TriggerTask, lv_texts):
    """游戏在某个主战员卡片右上角标的「推荐」：归给它下方最近的那个主战员。"""
    tag = next((box for box in task.all_texts
                if "推荐" in box.name and (box.x + box.width / 2) / task.width > 0.85), None)
    if not tag:
        return None
    tag_y = tag.y + tag.height / 2
    below = [index for index, box in enumerate(lv_texts) if box.y + box.height / 2 > tag_y]
    return min(below, key=lambda index: lv_texts[index].y) if below else None


def _choose_other_member(task: TriggerTask, lv_texts, others, new_equipment):
    """刷存档主战员不要的装备给谁，返回 (主战员下标或 None, 原因, 各人装备格读数)。
    先给这一格空着的人，再给这一格品质比它低的人里最低的，同样的给「推荐」的人、否则给站位靠前的；
    谁都比不过就返回 None（提炼）。每人只能装一件独特。
    出击模式以前是随机选人、不看装备（10/03 实跑出「稀有顶掉独特」），现在两个模式同一套规则。"""
    slot = new_equipment["slot"]
    new_rank = _EQUIPMENT_QUALITY_RANKS.get(new_equipment["quality"] or "", 0)
    eligible, readings = [], {}
    for index in others:
        qualities = _member_equipment_qualities(task, lv_texts[index])
        readings[index + 1] = getattr(task, "_slot_readings", None)
        if new_equipment["quality"] == "独特" and _has_unique_elsewhere(qualities, slot):
            continue
        eligible.append((index, qualities[slot]))
    if not eligible:
        return None, "其他主战员都已有独特装备", readings

    recommended = _recommended_member(task, lv_texts)

    def pick(indexes):
        return recommended if recommended in indexes else indexes[0]

    empty = [index for index, quality in eligible if quality == ""]
    if empty:
        return pick(empty), "这一格是空的", readings
    lower = [(_EQUIPMENT_QUALITY_RANKS[quality], index) for index, quality in eligible
             if quality is not None and _EQUIPMENT_QUALITY_RANKS[quality] < new_rank]
    if lower:
        lowest = min(rank for rank, _ in lower)
        return pick([index for rank, index in lower if rank == lowest]), "这一格品质最低", readings
    return None, "其他主战员这一格都不比它差", readings


def handle_equipment(task: TriggerTask):
    """装备选择/安装界面: 按装备位优先级选择，并维护目标主战员的装备状态。"""
    title = find_box_at_point(task, 0.499, 0.126)
    if not (title and title.name == "装备"):
        task._equipment_purchase_pending = None
        return False

    task.log_info("检测到装备页面")
    equipment = _equipment_state(task)
    equip_hint = find_box_at_point(task, 0.921, 0.135)

    if equip_hint and _get_game_text(task, '请选择主战员') in equip_hint.name:
        task.log_info("检测到安装装备界面")
        purchase_bottom_boxes = [
            box for box in task.all_texts
            if 0.013 <= (box.x + box.width / 2) / task.width <= 0.992
            and 0.881 <= (box.y + box.height / 2) / task.height <= 0.994
            and box.name.strip()
        ]
        cancel_box = next(
            (box for box in purchase_bottom_boxes if "取消" in box.name), None
        )
        purchase_box = next(
            (box for box in purchase_bottom_boxes if "购买" in box.name), None
        )
        price_box = next(
            (box for box in purchase_bottom_boxes
             if re.fullmatch(r"\d+", box.name.strip())),
            None,
        )
        is_purchase_page = bool(cancel_box and purchase_box)
        pending_purchase = getattr(task, "_equipment_purchase_pending", None)
        if (
            is_purchase_page
            and pending_purchase
            and _acceleration_on(task)
            and time.time() - pending_purchase["time"] < _EQUIPMENT_DECISION_KEEP
        ):
            # 上一帧已经选好主战员。这一帧只点购买，不重新选人。
            task.log_info("购买装备已选定主战员，点击「购买」")
            task._equipment_purchase_pending = None
            battle_log.process_shot(task, "装备分配")
            task.click_box(purchase_box)
            task.sleep(1)
            return True
        if not is_purchase_page:
            task._equipment_purchase_pending = None
        equipment_price = None
        current_credit = None
        if is_purchase_page:
            task.log_info("检测到购买装备页面")
            equipment_price = (
                _parse_discounted_price(price_box.name) if price_box else None
            )
            current_credit = _get_current_credit(task)
            task.log_info(
                f"购买装备页面: 当前信用点={current_credit}，"
                f"OCR价格=「{price_box.name if price_box else ''}」，"
                f"实际价格={equipment_price}"
            )
            if equipment_price is None:
                task.log_info("购买装备页面未识别到价格，按价格低于当前信用点继续购买")
            elif equipment_price > current_credit:
                task.log_info(
                    f"装备价格{equipment_price}大于当前信用点{current_credit}，点击「取消」"
                )
                battle_log.process_shot(task, "装备分配")
                task.click_box(cancel_box)
                task.sleep(1)
                return True

        bottom_buttons = [
            box for box in task.all_texts
            if 0.563 <= (box.x + box.width / 2) / task.width <= 0.998
            and 0.881 <= (box.y + box.height / 2) / task.height <= 0.997
            and box.name.strip()
        ]
        refine_boxes = [box for box in bottom_buttons if "提炼" in box.name]
        if refine_boxes and len(refine_boxes) == len(bottom_buttons):
            task.log_info("安装装备界面只有提炼按钮，直接点击提炼")
            battle_log.process_shot(task, "装备分配")
            task.click_box(refine_boxes[0])
            return True

        new_equipment = _equipment_info(
            task,
            (0.217, 0.379, 0.469, 0.436),
            (0.188, 0.444, 0.323, 0.489),
            (0.179, 0.492, 0.542, 0.668),
        )
        if not new_equipment:
            task.log_info("未能识别待安装装备的名称或类型")
            if battle_log.once(task, "装备页读不到装备"):
                battle_log.anomaly(task, "装备页读不到装备", "未能识别待安装装备的名称或类型")
            if is_purchase_page:
                task.log_info("购买装备无法识别装备信息，点击「取消」")
                battle_log.process_shot(task, "装备分配")
                task.click_box(cancel_box)
                task.sleep(1)
                return True
            return False
        equipment_desc = new_equipment["description"]
        task.log_info(f"待安装装备描述: 「{equipment_desc}」")

        if new_equipment["quality"] is None:
            misses = getattr(task, "_equipment_quality_misses", 0) + 1
            task._equipment_quality_misses = misses
            if misses < _EQUIPMENT_QUALITY_RETRIES:
                task.log_info(f"待安装装备读不出品质（第{misses}次），等下一帧重读")
                return True
            task.log_info("待安装装备连续读不出品质，按稀有处理")
            new_equipment["quality"] = "稀有"
        task._equipment_quality_misses = 0

        slot = new_equipment["slot"]
        decision_key = (new_equipment["name"], slot)  # 描述每帧的 OCR 可能不同，不放进来
        last = getattr(task, "_equipment_decision", None)
        if last and last["key"] == decision_key and time.time() - last["time"] < _EQUIPMENT_DECISION_KEEP:
            # 实跑 10/01 09:22：选好主战员后页面还停着，下一帧拿刚记下的自己比较，又转给了别人。
            # 不续期：连着来两件同名装备时，第二件最多等这么久就重新判断
            task.log_info(f"「{new_equipment['name']}」已选好第{last['member'] + 1}号主战员，等确认")
            return False

        lv_texts = _find_member_level_tags(
            task,
            (0.609, 0.290, 0.652, 0.789),
            page="安装装备页面",
        )
        target_member_index = _find_target_member_index(
            task,
            lv_texts,
            (0.607, 0.192, 0.739, 0.856),
            feature_name="target_member_tiny",
        )
        if target_member_index is not None:
            task.node_status["target_member_misses"] = 0
        tracks_target_member = "刷存档主战员" in getattr(task, "default_config", {})
        preferred_member_index = (
            target_member_index if tracks_target_member else (0 if lv_texts else None)
        )

        current_name, _ = _current_equipment_for_slot(task, equipment, slot)
        current_quality = equipment["qualities"][slot]
        should_install_first = False
        install_reason = "未找到目标主战员"
        task._slot_readings = None
        if preferred_member_index is not None:
            live_qualities = _member_equipment_qualities(
                task, lv_texts[preferred_member_index]
            )
            # 页面刚出来时主战员卡片还在淡入，装着的格子也会读成空（实跑 10/01 11:53 把「非典型方块」的记录清掉了）：
            # 记录里有装备却读成空时，先等下一帧重读
            vanished = [s for s, q in enumerate(live_qualities) if q == "" and equipment["names"][s]]
            misses = getattr(task, "_slot_vanish_misses", 0)
            if vanished and misses < _EQUIPMENT_QUALITY_RETRIES - 1:
                task._slot_vanish_misses = misses + 1
                task.log_info(f"第{[s + 1 for s in vanished]}号装备位记录有装备却读成空，等下一帧重读")
                return True
            task._slot_vanish_misses = 0
            for equipment_slot, live_quality in enumerate(live_qualities):
                if live_quality is None:
                    continue
                equipment["qualities"][equipment_slot] = live_quality
                if not live_quality:
                    if equipment["names"][equipment_slot]:
                        task.log_info(
                            f"第{equipment_slot + 1}号装备位实际为空，清除记录装备"
                            f"「{equipment['names'][equipment_slot]}」"
                        )
                    equipment["names"][equipment_slot] = ""
                    equipment["descriptions"][equipment_slot] = ""
            current_name, _ = _current_equipment_for_slot(task, equipment, slot)
            current_quality = equipment["qualities"][slot]
            should_install_first, install_reason = _should_install_equipment(
                task,
                current_name,
                current_quality,
                new_equipment,
            )
            if new_equipment["quality"] == "独特":
                if _has_unique_elsewhere(live_qualities, slot):
                    should_install_first = False
                    install_reason = "该主战员其他装备位已有独特装备"
                elif new_equipment["rank"] is None and _reserves_unique(task):
                    should_install_first = False
                    install_reason = "独特名额留给优先级配置里的装备"
        target_readings = getattr(task, "_slot_readings", None)
        installs_first = should_install_first and preferred_member_index is not None
        # 队友链在两种时候先算出来：主战员不要这件，或主战员只是「品质升级」——先看队友有没有这一格空着的。
        # 顺序（用户 10/03 定）：列表装备、主战员自己的空位照旧最优先；队友这一格空着时，先补队友再给主战员升级。
        target_missing = tracks_target_member and target_member_index is None
        others = [index for index in range(len(lv_texts)) if index != preferred_member_index]
        other_index, other_reason, other_slots = None, None, None
        if (not installs_first or (install_reason.startswith("品质") and current_quality)) \
                and not (is_purchase_page and target_missing):
            other_index, other_reason, other_slots = _choose_other_member(task, lv_texts, others, new_equipment)
            task._slot_readings = target_readings
        if installs_first and other_reason == "这一格是空的":
            installs_first = False
            install_reason = f"{install_reason}；队友这一格空着，先补队友"

        if is_purchase_page and (target_missing or (not installs_first and other_index is None)):
            # 谁都用不上才取消：主战员要（列表/空位/升级），或队友要用（空位优先、品质更低）都值得买。
            # 认不出刷存档主战员时不乱装（万一「其他人」里就有他），照旧重取头像、四次后 ESC
            reason = "未识别到刷存档主战员" if target_missing else (
                f"{install_reason}；{other_reason}" if other_reason else install_reason)
            task.log_info(f"购买装备「{new_equipment['name']}」谁都用不上（{reason}），点击「取消」")
            _log_equipment(task, new_equipment, "取消", None, reason, current_name, current_quality,
                           is_purchase_page, equipment_price)
            if tracks_target_member and target_member_index is None:
                if _target_member_missing(task, new_equipment):
                    battle_log.process_shot(task, "装备分配")
                    task.send_key("esc")
                else:
                    battle_log.process_shot(task, "装备分配")
                    task.click_box(cancel_box)
            else:
                # 认出了主战员、确实不值得买：本轮商店不再点它
                task.node_status.setdefault("shop_cancelled", []).append(new_equipment["ocr_name"])
                battle_log.process_shot(task, "装备分配")
                task.click_box(cancel_box)
            task.sleep(1)
            return True

        if installs_first:
            chosen_index = preferred_member_index
            if not tracks_target_member or target_member_index is not None:
                equipment["names"][slot] = new_equipment["name"]
                equipment["descriptions"][slot] = equipment_desc
                equipment["qualities"][slot] = new_equipment["quality"] or ""
            member_label = (
                "刷存档主战员"
                if target_member_index is not None
                else "第一主战员"
            )
            task.log_info(
                f"{slot + 1}号位装备「{new_equipment['name']}」优于当前装备「{current_name}」，"
                f"原因={install_reason}，"
                f"安装给{member_label}"
            )
            _log_equipment(task, new_equipment, member_label, preferred_member_index, install_reason,
                           current_name, current_quality, is_purchase_page, equipment_price)
        else:
            if tracks_target_member and target_member_index is None:
                install_reason = "未识别到刷存档主战员"
            chosen_index = other_index
            if chosen_index is None:
                refine_box = next(
                    (b for b in task.all_texts
                     if 0.522 <= (b.x + b.width / 2) / task.width <= 0.999
                     and 0.879 <= (b.y + b.height / 2) / task.height <= 0.996
                     and "提炼" in b.name),
                    None
                )
                if not refine_box:
                    task.log_info("未找到可选择的主战员或提炼按钮")
                    return False
                task.log_info(f"{slot + 1}号位装备「{new_equipment['name']}」没有主战员需要"
                              f"（{install_reason}，{other_reason}），点击提炼")
                _log_equipment(task, new_equipment, "提炼", None, f"{install_reason}；{other_reason}",
                               current_name, current_quality, is_purchase_page, equipment_price,
                               other_slots=other_slots)
                battle_log.process_shot(task, "装备分配")
                task.click_box(refine_box)
                task.sleep(1)
                return True
            task.log_info(
                f"{slot + 1}号位无需替换当前装备「{current_name}」，原因={install_reason}，"
                f"安装给第{chosen_index + 1}号主战员（{other_reason}）"
            )
            _log_equipment(task, new_equipment, "其他主战员", chosen_index, f"{install_reason}；{other_reason}",
                           current_name, current_quality, is_purchase_page, equipment_price,
                           other_slots=other_slots)

        chosen = lv_texts[chosen_index]
        battle_log.process_shot(task, "装备分配")
        _move_and_click(task, 0.756, (chosen.y + chosen.height / 2) / task.height)
        task.sleep(1)
        if is_purchase_page and _acceleration_on(task):
            # 点一下就结束这一帧。下一帧只点购买，不再重新选主战员。
            task._equipment_purchase_pending = {"time": time.time()}
            return True
        if is_purchase_page:
            task.log_info(
                f"购买装备分配完成，价格={equipment_price}，"
                f"当前信用点={current_credit}，点击「购买」"
            )
            battle_log.process_shot(task, "装备分配")
            task.click_box(purchase_box)
            task.sleep(1)
            return True
        task._equipment_decision = {"key": decision_key, "member": chosen_index, "time": time.time()}
        return False

    task._equipment_purchase_pending = None
    candidates = []
    candidate_specs = [
        (
            (0.409, 0.219, 0.678, 0.276),
            (0.384, 0.275, 0.562, 0.319),
            (0.382, 0.324, 0.723, 0.496),
            (0.518, 0.454),
        ),
        (
            (0.410, 0.551, 0.699, 0.608),
            (0.384, 0.613, 0.573, 0.653),
            (0.380, 0.658, 0.720, 0.836),
            (0.521, 0.600),
        ),
    ]
    for name_region, type_region, description_region, click_position in candidate_specs:
        candidate = _equipment_info(
            task, name_region, type_region, description_region
        )
        if not candidate:
            task.log_info("选择装备界面的候选装备信息不完整，等待下轮重新识别")
            return True
        candidate["click_position"] = click_position
        candidates.append(candidate)
    task.log_info(
        f"检测到选择装备界面，候选装备: "
        f"{[(candidate['ocr_name'], candidate['slot'] + 1) for candidate in candidates]}"
    )

    chosen_index = None
    for slot in range(3):
        current_name, current_rank = _current_equipment_for_slot(task, equipment, slot)
        for index, candidate in enumerate(candidates):
            if candidate["slot"] != slot or candidate["rank"] is None:
                continue
            if not current_name or candidate["rank"] < current_rank:
                chosen_index = index
                task.log_info(
                    f"优先选择{slot + 1}号位装备「{candidate['name']}」，当前装备「{current_name}」"
                )
                break
        if chosen_index is not None:
            break

    if chosen_index is None:
        empty_slot_candidates = [
            candidate for candidate in candidates
            if not _current_equipment_for_slot(task, equipment, candidate["slot"])[0]
        ]
        if empty_slot_candidates:
            chosen_candidate = random.choice(empty_slot_candidates)
            click_position = chosen_candidate["click_position"]
            task.log_info(
                f"候选装备均未命中升级条件，优先选择空缺的"
                f"{chosen_candidate['slot'] + 1}号位装备「{chosen_candidate['ocr_name']}」"
            )
        else:
            task.log_info("候选装备均未命中升级条件且对应位置均非空，随机选择一个装备")
            click_position = random.choice([spec[3] for spec in candidate_specs])
    else:
        click_position = candidates[chosen_index]["click_position"]
    _move_and_click(task, *click_position)
    task.sleep(2)
    return False


# 卡牌操作关键词 → 配置 key 映射
_SELECT_CARD_CONFIG_KEYS = {
    "移除": "移除卡牌列表",
    "复制": "复制卡牌列表",
    "闪光": "闪光卡牌列表",
    "灵光": "闪光卡牌列表",
}


def _scroll_to_target_member_for_card_removal(task: TriggerTask):
    """移除卡牌前滚动成员列表，直到目标成员出现或到达底部。"""
    feature_name = "target_member_in_select_card"
    page = "移除卡牌目标主战员查找"
    if not task.feature_exists(feature_name):
        task.log_info(f"{page}: 尚未保存{feature_name}特征，跳过查找")
        return

    search_region = (0.079, 0.092, 0.209, 0.675)
    search_box = task.box_of_screen(*search_region)
    scroll_x = (search_region[0] + search_region[2]) / 2
    scroll_y = (search_region[1] + search_region[3]) / 2
    scrollbar_white_ratio = region_white_ratio(
        task, (0.976, 0.119, 0.988, 0.858)
    )
    single_page = scrollbar_white_ratio < 0.01
    task.log_info(
        f"{page}: 滚动条区域白色像素占比={scrollbar_white_ratio:.2%}，"
        f"是否仅一页卡牌={single_page}"
    )

    max_scrolls = 20
    scroll_count = 0
    while True:
        target_member = task.find_one(
            feature_name=feature_name,
            box=search_box,
            threshold=0.5,
        )
        if target_member:
            task.log_info(
                f"{page}: 找到目标主战员，相似度={target_member.confidence:.4f}"
            )
            return
        if single_page:
            task.log_info(f"{page}: 当前仅一页，未找到目标主战员")
            return
        if _point_is_white(task, 0.982, 0.846, page):
            task.log_info(f"{page}: 已到达底部，未找到目标主战员")
            return
        if scroll_count >= max_scrolls:
            task.log_info(f"{page}: 向下滚动已达到{max_scrolls}次限制")
            return
        _scroll_card_page(
            task,
            scroll_x,
            scroll_y,
            -3,
            page,
            distance=(search_region[3] - search_region[1]) / 4,
        )
        scroll_count += 1


def _note_select_card_page_left(task: TriggerTask):
    """当前帧不在选卡页面：标记离开过，下次再进选卡页就是新的一页（"选完即止"的记忆要重置）。"""
    memory = getattr(task, "_select_card_memory", None)
    if memory is not None:
        memory["seen"] = 0.0


def handle_select_card(task: TriggerTask):
    """统一卡牌选择页面: 在(0.198,0.039)处检测文本，按移除/复制/闪光等关键字匹配配置并选择卡牌。"""
    box = find_box_at_point(task, 0.198, 0.039)
    m = _SELECT_CARD_PROMPT.search(box.name) if box else None
    if not m:
        _note_select_card_page_left(task)
        return False
    count_text = m.group(1)
    action = m.group(2)
    count = int(count_text) if count_text else 1
    config_key = _SELECT_CARD_CONFIG_KEYS.get(action)
    if config_key is None:
        _note_select_card_page_left(task)
        return False
    task.log_info(f"检测到卡牌{action}选择，需选择{count}张，配置key={config_key}")

    # 临时收集（2026-10-04，用户要求）：遇到需选2张的选卡页面自动存现场包，验证 2 张页面的 UI 和行为。
    # 素材收够后把这段和 recorder.TRIGGER_KINDS 里的「选2张页面」一起删掉。
    if count >= 2 and battle_log.once(task, "选2张页面收集"):
        battle_log.anomaly(task, "选2张页面", f"检测到{action}选择，需选择{count}张")

    # 日志打印右下角选牌操作提示
    action_tip = find_box_at_point(task, 0.945, 0.918)
    if action_tip:
        task.log_info(f"右下角选牌操作提示: 「{action_tip.name}」")

    if (
        action in ("移除", "复制", "闪光", "灵光")
        and task.name == "自动卡厄思模式"
    ):
        _scroll_to_target_member_for_card_removal(task)

    select_card(task, _get_card_list(task, config_key), count=count, action=action)
    memory = getattr(task, "_select_card_memory", None)
    if memory is not None:
        memory["prompt"] = box.name
    return True


def handle_copy_card_choice(task: TriggerTask):
    """复制卡牌选择页面: 按类型特征识别卡牌，并按复制卡牌列表优先级选择。"""
    box = find_box_at_point(task, 0.498, 0.133)
    copy_card_prompt = _get_game_text(task, "请选择要复制的卡牌")
    if not (box and copy_card_prompt in box.name):
        return False

    task.log_info("检测到复制卡牌选择页面")
    target_boxes, target_click_positions = find_target_card(task)
    if target_boxes:
        click_position = target_click_positions[0]
        task.log_info(
            f"复制卡牌选择: 检测到target卡牌，点击位置{click_position}"
        )
        _move_and_click(task, *click_position)
        return True

    cards = recognize_cards(task, page="复制卡牌选择页面")

    priority = _get_config_value(task, '复制卡牌列表', [])
    for pri_name in priority:
        for card in cards:
            if card["name"] and pri_name in card["name"]:
                task.log_info(f"复制卡牌选择: 按优先级选择「{card['name']}」(匹配「{pri_name}」)")
                _move_and_click(task, card["x"], card["y"])
                task.sleep(0.5)
                return True

    task.log_info("复制卡牌选择: 未命中任何优先级，return False")
    return False


def handle_copy_member(task: TriggerTask):
    """选择要复制卡牌的主战员页面。"""
    box = find_box_at_point(task, 0.502, 0.932)
    copy_member_prompt = _get_game_text(task, "选择要复制卡牌的主战员")
    if not (box and copy_member_prompt in box.name):
        return False

    task.log_info("检测到卡牌复制主战员选择事件，进行相应操作")

    confirm_box = task.box_of_screen(0.145, 0.044, 0.856, 0.214)
    click_positions = [(0.228, 0.510), (0.504, 0.504), (0.755, 0.508)]
    click_positions = _prioritize_target_member_click(
        task, click_positions, (0.173, 0.232, 0.858, 0.508)
    )

    for i, (cx, cy) in enumerate(click_positions):
        task.log_info(f"点击位置({cx}, {cy})")
        _move_and_click(task, cx, cy)
        task.sleep(0.5)

        feature = task.wait_feature("copymemberconfirm", box=confirm_box, time_out=2)
        if feature:
            task.log_info(f"点击位置({cx}, {cy})后成功找到copymemberconfirm")
            return True
        else:
            task.log_info(f"点击位置({cx}, {cy})后未找到copymemberconfirm，继续尝试")

    task.log_info("所有尝试均未找到copymemberconfirm")
    return True


def handle_convert_card(task: TriggerTask):
    """转换卡牌页面: 跳过转换。"""
    box = find_box_at_point(task, 0.226, 0.046)
    if box and _get_game_text(task, '转换的卡牌') in box.name:
        task.log_info("检测到卡牌转换选择，进行跳过操作")
        _move_and_click(task, 0.776, 0.926)
        task.sleep(0.5)
        _move_and_click(task, 0.661, 0.632)
        return True
    return False


def _dice_reroll(task: TriggerTask):
    """掷骰失败页的「重新掷骰」：返回 (按钮文字框, 费用, 右上角持有数)，读不到的为 None。"""
    button = next((box for box in task.all_texts if "重新掷" in box.name
                   and (box.y + box.height / 2) / task.height > 0.85), None)
    cost_text = _get_region_text(task, (0.38, 0.86, 0.49, 0.94))
    cost = int(cost_text) if cost_text.isdigit() else None
    owned = re.search(r"(\d+)\s*/\s*\d+", _get_region_text(task, (0.86, 0.06, 0.99, 0.15)))
    return button, cost, int(owned.group(1)) if owned else None


def handle_negotiation(task: TriggerTask):
    """掷骰失败页面: 右上角持有数够付「重新掷骰」的费用就重掷，不够或读不到就点下一步。"""
    title = find_box_at_point(task, 0.498, 0.683)
    if title and title.name in "失败":
        button, cost, owned = _dice_reroll(task)
        if button and cost is not None and owned is not None and owned >= cost:
            task.log_info(f"掷骰失败，持有{owned}，重新掷骰需{cost}，重新掷骰")
            battle_log.record(task, "掷骰", choice="重新掷骰", cost=cost, owned=owned)
            task.click_box(button)
            task.sleep(2)
            return True
        task.log_info(f"掷骰失败，持有{owned}，重新掷骰需{cost}，跳过掷骰子")
        battle_log.record(task, "掷骰", choice="下一步", cost=cost, owned=owned)
        _move_and_click(task, 0.665, 0.899)
        return True
    return False


def handle_continue(task: TriggerTask):
    """通用"继续"按钮。零式系统「雪上凝结的约定」的结算页上这个按钮写的是「为记忆的尽头」。"""
    continue_region = (0.459, 0.858, 0.992, 0.988)
    continue_text = _get_game_text(task, '继续')
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if (_clean_match(text_box.name, continue_text)
                or any(word in text_box.name for word in ("记忆的尽头", "記憶的盡頭")))
            and continue_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= continue_region[2]
            and continue_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= continue_region[3]
        ),
        None,
    )
    if box:
        task.log_info("检测到下一步操作，点击继续")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_confirm(task: TriggerTask):
    """通用"确认"按钮。"""
    confirm_region = (0.267, 0.867, 0.991, 0.979)
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _is_confirm_text(text_box.name)
            and confirm_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= confirm_region[2]
            and confirm_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= confirm_region[3]
        ),
        None,
    )
    if box:
        if is_button_active(task, box):
            task.log_info("检测到确认操作，点击确认")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("确认按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_convert(task: TriggerTask):
    """通用"转换"按钮: 按钮激活则点击转换，未激活则点击跳过(0.776,0.926)。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "转换"):
        if is_button_active(task, box):
            task.log_info("检测到转换按钮，点击转换")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("转换按钮未激活（灰色），点击跳过")
            _move_and_click(task, 0.776, 0.926)
            task.sleep(1)
            return True
    return False

def handle_remove(task: TriggerTask):
    """通用"移除"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "移除"):
        if is_button_active(task, box):
            task.log_info("检测到移除操作，点击移除")
            task.click_box(box)
            removed_count = max(
                1,
                getattr(task, "_pending_removed_card_count", 0),
            )
            _record_removed_cards(task, removed_count)
            task._pending_removed_card_count = 0
            task.sleep(1)
            return True
        else:
            task.log_info("移除按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_three_choice_card_remove(task: TriggerTask):
    """三选一卡牌移除页面：点击指定区域内已激活的“移除”按钮。"""
    region = (0.507, 0.889, 0.740, 0.967)
    remove_box = next(
        (
            box for box in task.all_texts
            if region[0] <= (box.x + box.width / 2) / task.width <= region[2]
            and region[1] <= (box.y + box.height / 2) / task.height <= region[3]
            and "移除" in box.name
        ),
        None,
    )
    if not remove_box:
        return False
    if not is_button_active(task, remove_box):
        task.log_info("三选一卡牌移除页面的移除按钮未激活（灰色），跳过点击")
        return False

    task.log_info("检测到三选一卡牌移除页面，点击移除")
    task.click_box(remove_box)
    _record_removed_cards(task, 1)
    task._pending_removed_card_count = 0
    return True

def _without_yi(text):
    """去掉「一」和横线：国际服「赋予灵光一闪」按钮亮起后，OCR 常把「一」读丢（实测读成「赋豫灵光闪」）。"""
    return re.sub(r'[一\-—_－]', '', text)


def _select_card_prompt_box(task: TriggerTask):
    """选卡页标题框；探针落空时按整页文字找。"""
    box = find_box_at_point(task, 0.198, 0.039)
    if box and _SELECT_CARD_PROMPT.search(box.name or ""):
        return box
    return next((text_box for text_box in getattr(task, "all_texts", None) or []
                 if _SELECT_CARD_PROMPT.search(text_box.name or "")), None)


def _select_card_ready_for_flash(task: TriggerTask):
    """选卡页已经选满，而且是这一页的记忆（上一页的 ready 不能拿来点「赋予灵光一闪」）。"""
    memory = getattr(task, "_select_card_memory", None)
    prompt = _select_card_prompt_box(task)
    if not memory or prompt is None or memory.get("action") not in ("闪光", "灵光"):
        return False
    if memory.get("prompt") != prompt.name:
        return False
    if memory.get("ready"):
        return True
    count = memory.get("count") or 1
    return len(memory.get("picked") or []) >= count


def handle_flash(task: TriggerTask):
    """通用"闪光"按钮。选卡页上这个按钮写的是「赋予灵光一闪」，必须等选牌逻辑选满再点
    （现场包 20261006-171000：没选牌就点下去，效果页白闪时又被跳过接走）。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _without_yi(_get_game_text(task, '闪光')) in _without_yi(box.name):
        if _select_card_prompt_box(task) is not None and not _select_card_ready_for_flash(task):
            task.log_info("选卡页还没选牌，不点「赋予灵光一闪」")
            return False
        if is_button_active(task, box):
            task.log_info("检测到闪光操作，点击闪光")
            task.click_box(box)
            memory = getattr(task, "_select_card_memory", None)
            if memory is not None:
                memory.update(ready=False, prompt=None, picked=[], seen=0.0)
            task.sleep(1)
            return True
        else:
            task.log_info("闪光按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_reflash(task: TriggerTask):
    """通用"重新闪光"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _get_game_text(task, '重新闪光') in box.name:
        if is_button_active(task, box):
            task.log_info("检测到重新闪光操作，点击重新闪光")
            task.click_box(box)
            task.sleep(2)
            return True
        else:
            task.log_info("重新闪光按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_grant_flash(task: TriggerTask):
    """通用"赋予闪光"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "赋予闪光"):
        if is_button_active(task, box):
            task.log_info("检测到赋予闪光操作，点击赋予闪光")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("赋予闪光按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_copy(task: TriggerTask):
    """通用"复制"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "复制"):
        if is_button_active(task, box):
            task.log_info("检测到复制操作，点击复制")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("复制按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_enter(task: TriggerTask):
    """通用"进入"按钮。"""
    enter_region = (0.017, 0.771, 0.996, 0.992)
    box = next(
        (
            text_box
            for text_box in task.all_texts
            if _clean_match(text_box.name, "进入")
            and enter_region[0]
            <= (text_box.x + text_box.width / 2) / task.width
            <= enter_region[2]
            and enter_region[1]
            <= (text_box.y + text_box.height / 2) / task.height
            <= enter_region[3]
        ),
        None,
    )
    if box:
        task.log_info("检测到进入按钮，点击进入")
        task.click_box(box)
        reset_mission_status(task)
        task.sleep(1)
        return True
    return False

def handle_equipment_recast(task: TriggerTask):
    """装备重铸页面: 点击确认重铸。"""
    box = find_box_at_point(task, 0.501, 0.128)
    if box and _get_game_text(task, '装备重铸') in box.name:
        task.log_info("检测到装备重铸页面，点击跳过")
        _move_and_click(task, 0.749, 0.932)
        task.sleep(1)
        return True
    return False


_TREASURE_LABEL_REGION = (0.45, 0.33, 0.95, 0.45)  # 宝箱房里宝箱上方「F1」「F2」按键标记所在区域
_TREASURE_LABEL_TRIES = 2


def _click_labeled_treasure(task: TriggerTask):
    """宝箱房（「传送门的另一侧是个堆满宝物的华丽空间」）有三个宝箱：中间那个靠 treasure 模板点开，
    两边的宝箱上方带「F1」「F2」按键标记，模板匹配不上，以前开完中间的就直接选「离开」。
    这里在宝箱区域找这些标记，点标记下方的箭头图标。同一节点每个标记最多点 2 次，打不开就不再管。"""
    x1, y1, x2, y2 = _TREASURE_LABEL_REGION
    labels = [
        b for b in task.all_texts
        if re.fullmatch(r'F[1-3]', b.name.strip().upper())
        and x1 <= (b.x + b.width / 2) / task.width <= x2
        and y1 <= (b.y + b.height / 2) / task.height <= y2
    ]
    node = getattr(task, "node_status", {}).get("node_count", 0)
    tried = getattr(task, "_treasure_label_tries", None)
    if not tried or tried.get("node") != node:
        tried = task._treasure_label_tries = {"node": node}
    for label in sorted(labels, key=lambda b: b.x):
        key = label.name.strip().upper()
        if tried.get(key, 0) >= _TREASURE_LABEL_TRIES:
            continue
        tried[key] = tried.get(key, 0) + 1
        x = (label.x + label.width / 2) / task.width
        y = (label.y + label.height / 2) / task.height + 0.04  # 标记正下方的箭头图标
        task.log_info(f"宝箱房：点击带「{key}」标记的宝箱（第{tried[key]}次）")
        _move_and_click(task, x, y)
        task.sleep(2)
        return True
    return False


def _is_info_option(description):
    """事件选项是不是只看说明（效果写成「确认【…】资讯」），不推进事件。"""
    return re.search(r"确认.{0,2}【.*资讯", description) is not None


def handle_event_task(task: TriggerTask):
    """事件任务页面: 识别事件选项特征和描述，按任务优先级选择推进。"""
    bottom_box = find_box_at_point(task, 0.516, 0.971)
    if bottom_box and re.search(r'\d+/\d+', bottom_box.name):
        return False

    rewards = task.find_feature(feature_name="taskreward")
    if rewards:
        reward = rewards[0]
        cx = (reward.x + reward.width / 2) / task.width
        cy = (reward.y + reward.height / 2) / task.height
        if 0.437 <= cx <= 0.902 and 0.350 <= cy <= 0.614:
            task.log_info("检测到任务奖励图标，优先点击")
            task.click_box(reward)
            return True

    tasks_info = recognize_event_options(task, page="事件任务页面")
    if not tasks_info:
        return False

    selectable_tasks = []
    for task_info in tasks_info:
        event_x = task_info["x"]
        event_y = task_info["y"]
        forbidden_region = (
            max(0.0, event_x - 0.131),
            max(0.0, event_y - 0.257),
            min(1.0, event_x - 0.090),
            min(1.0, event_y - 0.189),
        )
        forbidden_feature = task.find_one(
            feature_name="forbidden_event",
            box=task.box_of_screen(*forbidden_region),
        )
        if forbidden_feature:
            task.log_info(
                f"事件选项已被禁止，过滤描述「{task_info['description']}」，"
                f"forbidden_event相似度={forbidden_feature.confidence:.4f}"
            )
            continue
        selectable_tasks.append(task_info)
    all_options = tasks_info
    tasks_info = selectable_tasks
    if not tasks_info:
        task.log_info("事件任务页面的所有选项均被禁止，跳过本次选择")
        return False
    # 「询问方法 / 确认【盗猎者的乐趣】资讯」这类选项只是看一段说明，看过一次就变灰、点不动，
    # 但仍会被当成已选中（Y 坐标小于 0.925）一直点（实跑 10/01 10:05）：有别的选项时不选它
    advancing = [t for t in tasks_info if not _is_info_option(t["description"])]
    if advancing and len(advancing) < len(tasks_info):
        task.log_info(f"跳过只看说明的选项：{[t['description'] for t in tasks_info if t not in advancing]}")
        tasks_info = advancing

    check_region = task.box_of_screen(0.396, 0.286, 0.960, 0.718)
    check_features = [
        feature
        for feature_name in ("check", "check2")
        if (feature := task.find_one(feature_name=feature_name, box=check_region))
    ]
    check_feature = max(
        check_features,
        key=lambda feature: feature.confidence,
        default=None,
    )
    if check_feature:
        task.log_info(
            f"事件任务页面检测到check特征，相似度={check_feature.confidence:.4f}，优先点击"
        )
        task.click_box(check_feature)
        task.sleep(1)
        return True

    def click_event_option(event_task, reason):
        battle_log.record(task, "事件选项", options=[t["description"] for t in all_options],
                          chosen=event_task["description"], reason=reason)
        left, top, right, bottom = event_task["description_region"]
        description_x = (left + right) / 2
        description_y = (top + bottom) / 2
        _move_and_click(task, description_x, description_y)

    def handle_initial_node_task(description_keyword, purpose):
        """初始节点按描述选择任务；未找到目标描述时点击ESC重新开始。"""
        matched_task = next(
            (
                task_info
                for task_info in tasks_info
                if description_keyword in task_info["description"]
            ),
            None,
        )
        if matched_task:
            task.log_info(
                f"{purpose}：选择包含“{description_keyword}”的事件任务"
            )
            click_event_option(matched_task, f"{purpose}：包含「{description_keyword}」")
        else:
            task.log_info(
                f"{purpose}：未找到包含“{description_keyword}”的事件任务，"
                "点击ESC重新开始"
            )
            battle_log.reroll(task, purpose, options=[t["description"] for t in tasks_info])
            _open_escape_menu(task, 0.053)
        task.sleep(1)
        return True

    # 零式系统 boss 后的「雕琢记忆 / 奉行既定的启示」和「离开 / 事件结束」：总是雕琢（实跑 10/01 10:28
    # 被「任务/装备优先级」里的「结束」带去点离开，雕琢卡片又被当成已选中，两边来回点）。点一次选中、再点一次确认。
    # 已经雕琢过（记忆雕琢页离开时记下 carve_done，路线页清掉）就选离开，否则会在事件页和雕琢页之间来回进出
    carve_task = next((t for t in tasks_info if "雕琢记忆" in t["description"]), None)
    if carve_task is not None and getattr(task, "node_status", {}).get("carve_done", False):
        leave_task = next((t for t in tasks_info if "事件结束" in t["description"]
                           or t["description"].startswith("离开")), None)
        if leave_task is not None:
            task.log_info(f"本节点已雕琢过，选择离开: {leave_task['description']}")
            click_event_option(leave_task, "已雕琢过，离开")
            task.sleep(1)
            return True
    if carve_task is not None:
        task.log_info(f"选择雕琢记忆: {carve_task['description']}")
        click_event_option(carve_task, "雕琢记忆")
        task.sleep(1)
        return True

    upper_event_task = next(
        (task_info for task_info in tasks_info if task_info["y"] < 0.925),
        None,
    )
    if upper_event_task is not None:
        task.log_info(
            f"检测到Y坐标小于0.925的任务，立即选择: {upper_event_task['description']}"
        )
        click_event_option(upper_event_task, "Y坐标小于0.925的任务，立即选择")
        task.sleep(1)
        return True

    initial_card_name = _get_config_value(task, "刷初始卡牌", "")
    initial_card_name = initial_card_name.strip() if isinstance(initial_card_name, str) else ""
    node_status = getattr(task, "node_status", {})
    is_initial_node = (
        node_status.get("pass_final_boss_count", 0) == 0
        and node_status.get("node_count", 0) == 0
    )
    reroll_empty_deck = _get_config_value(task, "刷空档", False)
    if initial_card_name and reroll_empty_deck is True and is_initial_node:
        task.log_info(
            "“刷初始卡牌”和“刷空档”不能同时进行，"
            "本轮优先执行“刷初始卡牌”"
        )
    if initial_card_name and is_initial_node:
        return handle_initial_node_task(
            "传说卡牌",
            f"刷初始卡牌「{initial_card_name}」",
        )

    if reroll_empty_deck is True and is_initial_node:
        return handle_initial_node_task("移除2张", "刷空档")

    # 检查任务区域中是否有 treasure 特征
    treasure_box = task.box_of_screen(0.477, 0.336, 0.841, 0.540)
    treasure_features = task.find_feature(
        feature_name="treasure",
        box=treasure_box,
        threshold=0.7,
    )
    if treasure_features:
        task.log_info("检测到事件任务区域中有treasure特征，优先点击")
        battle_log.record(task, "事件选项", options=[t["description"] for t in all_options], chosen="宝箱",
                          reason="treasure 特征")
        task.click_box(treasure_features[0])
        task.sleep(2)
        return True
    if _click_labeled_treasure(task):
        return True

    # 读取拉黑任务列表
    blacklist = _get_config_value(task, '拉黑任务', ["咒术卡牌"])
    blacklist = list(blacklist) if isinstance(blacklist, (list, tuple)) else []
    if blacklist:
        # 过滤掉描述包含拉黑关键词的任务
        filtered_tasks = [
            t for t in tasks_info
            if not any(is_subsequence(bk, t['description']) for bk in blacklist)
        ]
        if len(filtered_tasks) < len(tasks_info):
            task.log_info(f"拉黑任务关键词: {blacklist}，过滤前{len(tasks_info)}个，过滤后{len(filtered_tasks)}个")
            for t in tasks_info:
                if t not in filtered_tasks:
                    task.log_info(f"  已拉黑: 描述: {t['description']}")
        # 如果全部被拉黑，兜底用原列表
        if not filtered_tasks:
            task.log_info("所有任务均被拉黑，兜底使用原列表")
            filtered_tasks = tasks_info
        tasks_info = filtered_tasks

    equipment_priority = []
    for slot in range(1, 4):
        equipment_priority.extend(
            _get_card_list(task, f"装备{slot}号位优先级")
        )
    priority = [
        *equipment_priority,
        *_get_card_list(task, "任务优先级"),
    ]
    chosen, chosen_reason = None, None
    for keyword in priority:
        for t in tasks_info:
            if is_subsequence(keyword, t['description']):
                chosen, chosen_reason = t, f"任务/装备优先级「{keyword}」"
                task.log_info(f"优先选择「{keyword}」-> 描述: {t['description']}")
                break
        if chosen is not None:
            break

    if chosen is None and task.name == "自动卡厄思模式":
        attack_event_features = task.find_feature(
            feature_name="attack_event",
            threshold=0.95,
        ) or []
        if attack_event_features:
            attack_event = max(
                attack_event_features,
                key=lambda feature: feature.confidence,
            )
            task.log_info(
                f"未命中任务优先级，检测到attack_event特征，"
                f"相似度={attack_event.confidence:.4f}，点击进入战斗任务"
            )
            battle_log.record(task, "事件选项", options=[t["description"] for t in all_options], chosen="战斗任务",
                              reason="未命中任务优先级，attack_event 特征")
            task.click_box(attack_event)
            task.sleep(1)
            return True

    if chosen is None:
        chosen, chosen_reason = random.choice(tasks_info), f"未命中优先级，随机（拉黑 {blacklist}）"
        task.log_info(
            f"未命中优先级描述，从{len(tasks_info)}个可选任务中随机选择: "
            f"{chosen['description']}"
        )

    click_event_option(chosen, chosen_reason)
    task.sleep(1)
    return True


def _log_route_choice(task, node_type, nodes, reason, **fields):
    """详细日志：路线选择（决定）+ 进入节点（状态快照：生命值、信用点、目标主战员装备、本局已移除/中立牌数）。"""
    battle_log.record(task, "路线选择", nodes=nodes, chosen=node_type, reason=reason, **fields)
    ns = getattr(task, "node_status", None) or {}
    equipment = _equipment_state(task)
    battle_log.node_entered(
        task, hp=_get_current_hp(task), credit=_get_current_credit(task),
        next_node=ns.get("node_count", 0) + 1, next_node_type=node_type,
        equipment=[f"{name or '空'}/{quality or '?'}" for name, quality
                   in zip(equipment.get("names", []), equipment.get("qualities", []))],
        removed_cards=ns.get("removed_card_count", 0), neutral_cards=ns.get("neutral_card_count", 0),
        flash_done=ns.get("flash_done_cards", []),
        meditation=_member_deck_state(task).get("冥想", {}),
    )


def handle_route_selection(task: TriggerTask):
    """路线选择页面: 识别节点类型，按优先级排序后依次点击所有节点，每次间隔1秒。
    同时负责节点计数：离开路线页面时 node_count +1。"""
    position_box = task.box_of_screen(0.335, 0.568, 0.453, 0.751)
    position_feature = task.find_feature(feature_name="position", box=position_box)
    cant_receive = find_box_at_point(task, 0.186, 0.850)
    is_route_page = position_feature or (cant_receive and "无法接收到梦境号" in cant_receive.name)

    # 如果当前页面不是路线选择页面，但 enter_new_node 为 True（刚离开路线页面），计数+1
    if not is_route_page:
        if hasattr(task, 'node_status') and task.node_status.get('enter_new_node', False):
            task.node_status['enter_new_node'] = False
            task.node_status['node_count'] += 1
            task.log_info(f"离开路线选择页面，当前节点计数: {task.node_status['node_count']}")
        return False

    # 是路线选择页面，标记进入新节点
    if hasattr(task, 'node_status'):
        task.node_status['enter_new_node'] = True

    task.log_info("检测到路线选择页面，按优先级依次点击节点")

    # 更新节点状态：进入路线选择页面时 flash_or_rest 置为 True
    if hasattr(task, 'node_status'):
        task.node_status['flash_or_rest'] = True
        task.node_status['carve_done'] = False
        task.node_status['memory_processed'] = False
        task.log_info("检测到路线选择页面，更新 node_status['flash_or_rest']=True")
        # 检查"进入商店"配置，若为 True 则同时更新 shop 状态
        if _get_config_value(task, '进入商店', False):
            task.node_status['shop'] = True
            task.log_info(f"进入商店配置为True，更新 node_status['shop']=True")
    task.sleep(1)

    route_box = task.box_of_screen(0.656, 0.053, 0.977, 0.908)
    node_feature_types = {
        "safezone": "休息",
        "enemy": "小怪",
        "elite": "精英",
        "event": "事件",
        "settlement": "结算",
    }
    nodes = []
    for feature_name, node_type in node_feature_types.items():
        for feature_box in task.find_feature(feature_name=feature_name, box=route_box):
            nodes.append({
                "feature_name": feature_name,
                "node_type": node_type,
                "box": feature_box,
                "special_features": [],
            })

    # 点完普通节点后路线页还会停留 1~3 秒，节点图标已经淡出，一个都匹配不到（实跑 22:30 一轮里误判 4 次 boss，
    # 把 reach_final_boss 置成 True，之后的普通战斗都被当成 boss 战）。刚点过节点就只等页面切走。
    if not nodes and time.time() - getattr(task, '_route_node_click_time', 0) < 5:
        task.log_info("刚点过路线节点，路线页图标已淡出，等待页面切换（不当作boss节点）")
        task.sleep(0.5)
        return True

    # 找不到任何普通节点类型特征时，当前路线节点即为boss。
    if not nodes:
        task.log_info("检测到最终boss节点，点击进入")
        if hasattr(task, 'node_status'):
            task.node_status['reach_final_boss'] = True
            task.node_status['node_type'] = "boss"

        # 检查"第几层boss前自动暂停"配置
        pause_config = _get_config_value(task, '第几层boss前自动暂停', "不暂停")
        if pause_config != "不暂停":
            try:
                pause_layer = int(pause_config)
                current_layer = task.node_status.get('pass_final_boss_count', 0)
                if pause_layer - 1 == current_layer:
                    task.log_info(f"配置在第{pause_layer}层boss前暂停（当前已通过{current_layer}层），暂停工具")
                    from ok import og
                    og.executor.pause()
                    task.sleep(5)
                    return True
            except (ValueError, TypeError):
                pass

        _log_route_choice(task, "boss", [], "找不到普通节点：boss 节点")
        _move_and_click(task, 0.815, 0.492)
        task.sleep(2)
        return True

    def relative_center(box):
        return (
            (box.x + box.width / 2) / task.width,
            (box.y + box.height / 2) / task.height,
        )

    # 特殊特征优先级：负数高于普通节点，正数低于普通节点。
    special_feature_priorities = {
        "shop": -1,
        "kalei": -1,
        "seal": -1,
        "hard": 1,
    }

    # 每个特殊特征只归属到距离最近的一个节点类型特征。
    for special_name in special_feature_priorities:
        for special_box in task.find_feature(feature_name=special_name, box=route_box):
            special_x, special_y = relative_center(special_box)
            nearest_node = min(
                nodes,
                key=lambda node: (
                    (relative_center(node["box"])[0] - special_x) ** 2
                    + (relative_center(node["box"])[1] - special_y) ** 2
                ),
            )
            nearest_node["special_features"].append(special_name)
            task.log_info(
                f"特殊特征「{special_name}」分配给"
                f"「{nearest_node['node_type']}」节点"
            )

    priority = _get_route_priority(task)
    task.log_info(f"路线优先级配置: {priority}")
    task.log_info(
        f"识别到的路线节点: "
        f"{[(node['node_type'], node['special_features']) for node in nodes]}"
    )

    priority_index = {node_type: index for index, node_type in enumerate(priority)}

    def sort_key(node):
        # 商店节点是固定最高优先级，不受用户配置的节点类型顺序影响。
        shop_priority = 0 if "shop" in node["special_features"] else 1
        if node["node_type"] == "结算":
            type_priority = len(priority) + 1
        else:
            type_priority = priority_index.get(node["node_type"], len(priority))
        special_priority = min(
            (special_feature_priorities[name] for name in node["special_features"]),
            default=0,
        )
        center_x, center_y = relative_center(node["box"])
        return shop_priority, type_priority, special_priority, center_y, center_x

    # 先根据左下角小地图的完整连通关系规划路线，再用右侧当前可点击节点
    # 校验规划结果。两边识别不一致时，使用当前节点识别结果兜底。
    map_info = recognize_map_connections(task)
    route_plan = find_best_map_route_by_priority(map_info, priority)
    visible_nodes = sorted(nodes, key=lambda item: relative_center(item["box"])[1])
    node = None
    if route_plan and route_plan["next_row"] is not None:
        planned_index = route_plan["next_row"] - 1
        if 0 <= planned_index < len(visible_nodes):
            planned_node = visible_nodes[planned_index]
            expected_specials = {
                name.removesuffix("_in_map")
                for name in route_plan["next_special_features"]
            }
            actual_specials = set(planned_node["special_features"])
            type_matches = (
                planned_node["node_type"] == route_plan["next_node_type"]
            )
            specials_match = actual_specials == expected_specials
            task.log_info(
                f"小地图规划路线={route_plan['route']}，"
                f"下一列第{route_plan['next_row']}个节点，"
                f"预计={route_plan['next_node_type']}+{sorted(expected_specials)}，"
                f"当前节点识别={planned_node['node_type']}+{sorted(actual_specials)}"
            )
            if type_matches and specials_match:
                node = planned_node
                task.log_info("小地图规划与当前节点识别一致，按规划结果进入")
            else:
                task.log_info(
                    "小地图规划与当前节点识别不一致，"
                    "改用当前节点的路线优先级兜底选择"
                )
        else:
            task.log_info(
                f"小地图规划要求进入下一列第{route_plan['next_row']}个节点，"
                f"但当前仅识别到{len(visible_nodes)}个节点，改用优先级兜底"
            )
    else:
        task.log_info("小地图路线规划失败，改用当前节点的路线优先级兜底")

    reason = "小地图规划"
    if node is None:
        node = sorted(nodes, key=sort_key)[0]
        reason = f"路线优先级 {priority}"

    # 更新 node_type 为最优先的节点类型
    if hasattr(task, 'node_status'):
        task.node_status['node_type'] = node["node_type"]
        task.log_info(f"更新 node_type 为「{node['node_type']}」")

    center_x, center_y = relative_center(node["box"])
    click_x = center_x - 0.095
    click_y = center_y - 0.0065
    task.log_info(
        f"点击{node['node_type']}节点"
        f"（特殊特征: {node['special_features']}，位置: {click_x:.3f}, {click_y:.3f}）"
    )
    _log_route_choice(task, node["node_type"], [(n["node_type"], n["special_features"]) for n in visible_nodes],
                      reason, chosen_specials=node["special_features"],
                      plan=route_plan["route"] if route_plan else None)
    _move_and_click(task, click_x, click_y)
    task._route_node_click_time = time.time()

    task.sleep(2)

    return True


def handle_obtain_reward(task: TriggerTask):
    """获得奖励页面: 点击领取。若此时 reach_final_boss 为 True，说明已通关关底boss，过层+1并重置层状态。"""
    box = find_box_at_point(task, 0.924, 0.922)
    if not (box and _clean_match(box.name, "获得")):
        # 按钮有高/低两套渲染位置，固定检测点会落空（10/03 命运结算页卡死）：区域内按文字找
        box = find_button_by_text(task, ("获得",))
    if box and _clean_match(box.name, "获得"):
        task.log_info("检测到获得奖励页面，点击领取")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_leave(task: TriggerTask):
    """离开按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if not (box and _clean_match(box.name, "离开")):
        # 按钮有高/低两套渲染位置，固定检测点会落空（10/03 命运结算页卡死）：区域内按文字找
        box = find_button_by_text(task, ("离开",))
    if box and _clean_match(box.name, "离开"):
        if _shop_opening(task):
            task.log_info("刚点了德朗商店，等商店页面出来，先不点离开")
            return True
        if is_button_active(task, box):
            task.log_info("检测到离开按钮，点击离开")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("离开按钮未激活（灰色），跳过点击")
            return False
    return False
def handle_next_step(task: TriggerTask):
    """通用"下一步"按钮: 在区域(0.833,0.885,0.954,0.957)内检测文本，编辑距离<=2即匹配。"""
    x1, y1, x2, y2 = 0.833, 0.885, 0.954, 0.957
    for b in task.all_texts:
        cx = (b.x + b.width / 2) / task.width
        cy = (b.y + b.height / 2) / task.height
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            if _edit_distance(b.name, "下一步", max_dist=2):
                task.log_info(f"检测到下一步按钮「{b.name}」，点击")
                task.click_box(b)
                task.sleep(1)
                return True
    return False


def handle_craft(task: TriggerTask):
    """合成按钮。"""
    box = find_box_at_point(task, 0.938, 0.903)
    if box and _clean_match(box.name, "合成"):
        if is_button_active(task, box):
            task.log_info("检测到合成按钮，点击合成")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("合成按钮未激活（灰色），跳过点击")
            return False
    return False

def handle_select(task: TriggerTask):
    """通用"选择"按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "选择"):
        if is_button_active(task, box):
            task.log_info("检测到选择按钮，点击选择")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("选择按钮未激活（灰色），跳过点击")
            return False
    return False


def _find_rest_feature(task: TriggerTask):
    """在休息区域查找rest特征，命中时输出置信度。"""
    search_box = task.box_of_screen(0.157, 0.503, 0.467, 0.863)
    rest_feature = task.find_one(feature_name="rest", box=search_box)
    if rest_feature:
        task.log_info(f"检测到rest特征，匹配置信度: {rest_feature.confidence:.2%}")
    return rest_feature


def _wait_for_rest_confirm(task: TriggerTask):
    """等待休息操作后的确认按钮出现。
    wait_ocr 的结果不经过 _simplify_texts，框架只在软件界面语言为繁体时才自动转简体，
    所以繁中服要同时匹配「確認」。"""
    confirm_boxes = task.wait_ocr(
        0.170, 0.554, to_x=0.855, to_y=0.769,
        match=re.compile(r"确认|確認"), time_out=2,
    )
    if not confirm_boxes:
        task.log_info("等待休息确认按钮超时")
        return False
    return True


# 休息区读不到生命值/信用点时，最多跳过几帧等数字显示出来
_REST_READ_RETRIES = 3


def _retry_rest_reading(task: TriggerTask, what):
    """刚进入休息区时生命值、信用点常常还没显示，读不到时本帧先不做选择，返回 True 等下一帧重读；
    连续 _REST_READ_RETRIES 帧仍读不到时返回 False，由调用方按保守方式（休息）处理。"""
    retries = getattr(task, "_rest_read_retries", 0)
    if retries >= _REST_READ_RETRIES:
        task._rest_read_retries = 0
        task.log_info(f"休息区连续{retries}帧读不到{what}，按保守方式处理")
        return False
    task._rest_read_retries = retries + 1
    task.log_info(f"休息区读不到{what}，等下一帧重读（第{retries + 1}次）")
    return True


def handle_rest(task: TriggerTask):
    """休息界面: 根据血量、信用点和冥想状态选择休息或冥想。"""
    rest_feature = _find_rest_feature(task)
    free_text = _get_region_text(task, (0.154, 0.602, 0.359, 0.847))
    flash_or_rest = (
        hasattr(task, 'node_status')
        and task.node_status.get('flash_or_rest', False)
    )
    can_rest = bool(rest_feature and "免费" in free_text and flash_or_rest)

    meditation_region = (0.671, 0.433, 0.945, 0.801)
    meditate_feature = task.find_one(
        feature_name="meditate",
        box=task.box_of_screen(*meditation_region),
    )
    meditation_cost_boxes = [
        text_box for text_box in task.all_texts
        if re.fullmatch(r"\d+", text_box.name.strip())
        and meditation_region[0] <= (text_box.x + text_box.width / 2) / task.width <= meditation_region[2]
        and meditation_region[1] <= (text_box.y + text_box.height / 2) / task.height <= meditation_region[3]
    ]
    if meditate_feature and meditation_cost_boxes:
        feature_center = (
            meditate_feature.x + meditate_feature.width / 2,
            meditate_feature.y + meditate_feature.height / 2,
        )
        meditation_cost_box = min(
            meditation_cost_boxes,
            key=lambda text_box: (
                (text_box.x + text_box.width / 2 - feature_center[0]) ** 2
                + (text_box.y + text_box.height / 2 - feature_center[1]) ** 2
            ),
        )
        meditation_cost = int(meditation_cost_box.name.strip())
    else:
        meditation_cost = None

    meditation_state = _member_deck_state(task).get("冥想", {})
    has_pending_meditation = (
        isinstance(meditation_state, dict)
        and any(value is True for value in meditation_state.values())
    )
    current_credit = _get_current_credit(task)
    meditation_credit_threshold = _get_config_value(
        task, "多少信用点以上冥想", 300,
    )
    try:
        meditation_credit_threshold = int(meditation_credit_threshold)
    except (TypeError, ValueError):
        meditation_credit_threshold = 300
    can_meditate = bool(
        flash_or_rest
        and meditate_feature
        and meditation_cost is not None
        and has_pending_meditation
        and current_credit > meditation_credit_threshold
        and current_credit > meditation_cost
    )

    if can_rest and can_meditate:
        hp_percent = _get_current_hp_percent(task)
        if hp_percent is False and _retry_rest_reading(task, "生命值"):
            return True
        task._rest_read_retries = 0
        # 多帧仍读不到生命值时选择休息：原来会选冥想，低血量时不回血有风险
        choose_rest = hp_percent is False or hp_percent < 50
        task.log_info(
            f"休息与冥想均可用，当前血量="
            f"{hp_percent if hp_percent is not False else '未识别'}%，"
            f"选择{'休息' if choose_rest else '冥想'}"
        )
    else:
        choose_rest = can_rest

    rest_fields = dict(hp=_get_current_hp(task), credit=current_credit, can_rest=bool(can_rest),
                       can_meditate=can_meditate, meditation_cost=meditation_cost)
    if choose_rest:
        task.log_info("检测到休息界面，点击休息")
        battle_log.record(task, "休息区", choice="休息", **rest_fields)
        task.move_relative(
            (rest_feature.x + rest_feature.width / 2) / task.width,
            (rest_feature.y + rest_feature.height / 2) / task.height,
        )
        task.click_box(rest_feature)
        if not _wait_for_rest_confirm(task):
            return True
        task.node_status['flash_or_rest'] = False
        return True

    if can_meditate:
        pending_cards = [
            name for name, pending in meditation_state.items() if pending is True
        ]
        task.log_info(
            f"检测到可冥想卡牌{pending_cards}，当前信用点={current_credit}，"
            f"冥想费用={meditation_cost}，点击冥想"
        )
        battle_log.record(task, "休息区", choice="冥想", cards=pending_cards, **rest_fields)
        task.click_box(meditate_feature)
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
        task._shop_clicked_at = time.time()
        task.sleep(2)
        return True
    return False


_SHOP_OPENING = 3   # 点了「德朗商店」后这么多秒内不点「离开」：商店页还没出来，休息区的离开按钮仍在画面上


def _shop_opening(task: TriggerTask):
    """实跑 10/01 12:57：点了德朗商店，下一帧休息区的「离开」被点掉，点了 3 次才进去。"""
    return time.time() - getattr(task, "_shop_clicked_at", 0) < _SHOP_OPENING


def handle_shop(task: TriggerTask):
    """德朗商店: 优先移除卡牌，其次按配置购买卡牌或装备，最后尝试免费刷新。"""
    box = find_box_at_point(task, 0.729, 0.261)
    soldout = find_box_at_point(task, 0.727, 0.286)
    if (box and "移除卡牌" in box.name) or (soldout and "售" in soldout.name):
        task.log_info("handle_shop: 通过页面判定（移除卡牌或售罄）")
        if soldout and "售" in soldout.name:
            task.log_info(f"德朗商店: 移除卡牌已售罄")
            task.node_status['shop'] = False
        else:
            current_credit = _get_current_credit(task)
            removed_card_count = task.node_status.get("removed_card_count", 0)
            task.log_info(f"handle_shop: 当前信用点={current_credit}")
            cost_box = find_box_at_point(task, 0.724, 0.319)
            task.log_info(f"handle_shop: 0.724,0.319处费用文本='{cost_box.name if cost_box else None}'")
            if cost_box and cost_box.name.isdigit():
                cost = int(cost_box.name)
                if removed_card_count >= 5:
                    task.log_info(
                        f"本局已移除{removed_card_count}张卡牌，"
                        "达到5张上限，不再使用信用点移除卡牌"
                    )
                elif cost <= current_credit and task.node_status['shop'] is True:
                    task.log_info(f"德朗商店: 移除卡牌需{cost}信用点，当前{current_credit}，足够，点击移除")
                    battle_log.record(task, "商店", item="移除卡牌", price=cost, credit=current_credit)
                    task.click_box(box)
                    task.sleep(1)
                    task.node_status['shop'] = False
                    return True
                else:
                    task.log_info(f"德朗商店: 移除卡牌需{cost}信用点，当前{current_credit}，不足，继续挑选其他商品")
            else:
                task.log_info("handle_shop: 移除卡牌费用读取失败，继续挑选其他商品")
            task.node_status['shop'] = False

        current_credit = _get_current_credit(task)
        task.log_info(f"德朗商店挑选商品: 当前信用点={current_credit}")
        credit_icons = sorted(
        task.find_feature(
            feature_name="credit_icon",
            box=task.box_of_screen(0.019, 0.761, 0.979, 0.890),
        ) or [],
        key=lambda feature: feature.x,
        )

        card_type_features = []
        card_type_region = task.box_of_screen(0.032, 0.669, 0.968, 0.799)
        for feature_name in (
            "attack_in_shop",
            "skill_in_shop",
            "enhance_in_shop",
        ):
            for feature in task.find_feature(
            feature_name=feature_name,
            box=card_type_region,
            ) or []:
                card_type_features.append((feature_name, feature))

        card_icon_indexes = set()
        for feature_name, feature in card_type_features:
            if not credit_icons:
                break
            feature_x = feature.x + feature.width / 2
            feature_y = feature.y + feature.height / 2
            closest_index = min(
            range(len(credit_icons)),
            key=lambda index: (
                credit_icons[index].x + credit_icons[index].width / 2 - feature_x
            ) ** 2 + (
                credit_icons[index].y + credit_icons[index].height / 2 - feature_y
            ) ** 2,
            )
            card_icon_indexes.add(closest_index)
            task.log_info(f"德朗商店: {feature_name}特征绑定第{closest_index + 1}个信用点图标")

        card_priority = _get_card_reward_priority(task)
        equipment_priorities = [_equipment_priority(task, slot) for slot in range(3)]
        for index, credit_icon in enumerate(credit_icons):
            icon_x = (credit_icon.x + credit_icon.width / 2) / task.width
            icon_y = (credit_icon.y + credit_icon.height / 2) / task.height
            price_box = find_box_at_point(task, icon_x + 0.099, icon_y - 0.001)
            price = _parse_discounted_price(price_box.name) if price_box else None
            item_name = _get_region_text(task, (
            max(0.0, icon_x - 0.013),
            max(0.0, icon_y - 0.247),
            min(1.0, icon_x + 0.120),
            min(1.0, icon_y - 0.105),
            )).strip()
            is_card = index in card_icon_indexes
            item_type = "卡牌" if is_card else "装备"
            task.log_info(f"德朗商店第{index + 1}个商品: 类型={item_type}，名称=「{item_name}」，价格={price}")
            if not item_name or price is None or price >= current_credit:
                continue

            if is_card:
                if _neutral_card_limit_reached(task):
                    neutral_card_count = task.node_status.get(
                        "neutral_card_count", 0,
                    )
                    neutral_card_limit = _neutral_card_limit(task)
                    task.log_info(
                        f"本局已获得{neutral_card_count}张中立牌，"
                        f"达到固定上限{neutral_card_limit}张，跳过商店卡牌"
                    )
                    continue
                matched_name = next(
                (name for name in card_priority
                 if name and (name in item_name or item_name in name)),
                None,
                )
            else:
                matched_name = None
                for slot, priority in enumerate(equipment_priorities):
                    canonical_name, rank = _match_equipment_name(item_name, priority)
                    if rank is None:
                        continue
                    # 和购买页同一个标准：刷存档主战员这一格已有配置里更靠前的装备就不买
                    # （实跑 10/01 12:09：商店点「黑曜石剑」，购买页嫌它不如现有的又取消，来回 14 次）
                    current_name, current_rank = _current_equipment_for_slot(task, _equipment_state(task), slot)
                    if current_name and current_rank <= rank:
                        task.log_info(f"德朗商店: 装备「{item_name}」不如{slot + 1}号位现有的「{current_name}」，跳过")
                        continue
                    matched_name = canonical_name
                    break
            if not matched_name:
                continue
            # 购买页上决定不买、点了取消的装备（比如没认出刷存档主战员），本轮不再点，免得来回点（实跑 10/01 11:02）
            cancelled = task.node_status.get("shop_cancelled", [])
            if not is_card and any(name in item_name or item_name in name for name in cancelled):
                task.log_info(f"德朗商店: 装备「{item_name}」本轮在购买页取消过，跳过")
                continue

            task.log_info(
            f"德朗商店: {item_type}「{item_name}」命中配置「{matched_name}」，"
            f"价格{price}小于当前信用点{current_credit}，点击信用点图标"
            )
            battle_log.record(task, "商店", item=item_name, item_type=item_type, matched=matched_name,
                              price=price, credit=current_credit)
            task.click_box(credit_icon)
            task.sleep(1)
            return True

        free_box = next(
        (text_box for text_box in task.all_texts
         if 0.012 <= (text_box.x + text_box.width / 2) / task.width <= 0.258
         and 0.892 <= (text_box.y + text_box.height / 2) / task.height <= 0.979
         and "免费" in text_box.name),
        None,
        )
        if free_box:
            task.log_info("德朗商店没有符合要求的商品，点击「免费」刷新")
            battle_log.record(task, "商店", item="免费刷新", credit=current_credit)
            task.click_box(free_box)
            task.sleep(1)
            return True
    return False


_FLASH_CHOICE_RETRY_SECONDS = 4.0  # 点了灵光选项后这么久内不再换着点，等页面响应
_FLASH_CHOICE_TITLE_REGION = (0.30, 0.08, 0.70, 0.18)  # 「请选择灵光一闪效果」标题带


def _flash_page_signature(task):
    """和文字闸门同一套画面签名，用来判断闪光页是不是已经变了。"""
    import speedup
    return speedup._signature(task, getattr(task, "all_texts", None) or [])


def _flash_page_moved(task):
    """加速模式下，点过的闪光页文字布局已经变了（详情弹出或离开）就不再干等那 4 秒。"""
    previous = getattr(task, "_flash_choice_sig", None)
    if not previous or not _acceleration_on(task):
        return False
    import speedup
    current = _flash_page_signature(task)
    return speedup._similarity(previous, current) < speedup._SAME_PAGE


def _remember_flash_click(task):
    task._flash_choice_clicked_at = time.time()
    if _acceleration_on(task):
        task._flash_choice_sig = _flash_page_signature(task)


def _is_flash_choice_page(task: TriggerTask):
    """卡牌闪光三选一页：右上「查看原件 / 查看内容 / 查看之前的闪光」，或标题「请选择灵光一闪效果」。"""
    box1 = find_box_at_point(task, 0.890, 0.051)
    box2 = find_box_at_point(task, 0.896, 0.131)
    labels = (_get_game_text(task, "查看原件"), _get_game_text(task, "查看之前的闪光"))
    if any(box and any(label in box.name for label in labels) for box in (box1, box2)):
        return True
    title = _get_region_text(task, _FLASH_CHOICE_TITLE_REGION)
    return "请选择" in title and "效果" in title and ("灵光" in title or "闪光" in title)


def handle_view_original(task: TriggerTask):
    """卡牌闪光（查看原件）事件: 按类型特征识别卡牌，并按闪光优先级选择。"""
    if not _is_flash_choice_page(task):
        return False

    # 这个页面会连续识别好几帧，描述里的数字每次 OCR 不完全一样（实况把「168%×4」读成 1168%，
    # 下一帧又读回 168%），每帧重新决策会先选 3 号再改点 1 号，把选择顶掉。
    # 点过选项后先等页面响应（详情弹窗/切页）；超过重试间隔页面还没动，说明可能没点上，才允许再点。
    if time.time() - getattr(task, "_flash_choice_clicked_at", 0.0) < _FLASH_CHOICE_RETRY_SECONDS:
        if _flash_page_moved(task):
            # 锁定还留着，避免下一帧又重新选；这一帧不再占住，后面的处理函数可以点确认。
            task.log_info("卡牌闪光页面: 页面已变化，解除等待")
            return False
        task.log_info("卡牌闪光页面: 刚点过灵光选项，等页面响应，不重复点击")
        return True

    cards = recognize_cards(task, page="卡牌闪光页面")
    if not cards:
        since = getattr(task, "_flash_choice_empty_since", None)
        now = time.time()
        if since is None:
            task._flash_choice_empty_since = now
            since = now
        waited = now - since
        if waited < _FLASH_CHOICE_RETRY_SECONDS:
            # 效果页入场白闪：牌还没出来时点「跳过」会把闪光优先级（如琶音）直接跳掉
            task.log_info(f"卡牌闪光页面: 牌面还没出来，等下一帧（已 {waited:.1f} 秒）")
            return True
        task._flash_choice_empty_since = None
        task.log_info("卡牌闪光页面: 等牌面超时，交给后面的处理函数")
        return False
    task._flash_choice_empty_since = None

    flash_rules = _flash_rules(task)
    first_rule = next((re.sub(r"\s+", "", k) for k in _get_card_list(task, '闪光优先级')
                       if isinstance(k, str) and re.sub(r"\s+", "", k)), None)
    chosen_card, choose_reason = None, None
    for rule in flash_rules:
        chosen_card = next((card for card in candidates if _flash_rule_matches(rule, card)), None)
        if chosen_card:
            choose_reason = f"闪光优先级「{rule[0]}」"
            task.log_info(f"优先选择「{chosen_card['name']}」({rule[0]})")
    # 拉黑的版本不参与挑选；三个版本都被拉黑时照常选，免得卡在这一页
    blacklist = _flash_blacklist_rules(task)
    candidates = [card for card in cards if not any(_flash_rule_matches(rule, card) for rule in blacklist)]
    if len(candidates) < len(cards):
        names = "、".join(f"第{i + 1}个" for i, card in enumerate(cards) if card not in candidates)
        if candidates:
            task.log_info(f"卡牌闪光页面: 拉黑卡牌，排除{names}版本")
        else:
            task.log_info("卡牌闪光页面: 三个版本都被拉黑了，照常选")
            candidates = cards

            if (
                _get_config_value(task, "首层刷特定闪光", False) is True
                and rule[0] == first_rule
            ):
                task.node_status["get_specific_flash"] = True
                task.log_info("已命中闪光优先级第一项，记录已获得特定闪光")
            break

    target_boxes, target_click_positions = find_target_card(task)
    matched_meditation_cards = _matching_meditation_card_names(task, cards)
    if matched_meditation_cards:
        meditation_state = _member_deck_state(task)["冥想"]
        meditation_completed = chosen_card is None and not target_boxes
        for configured_name in matched_meditation_cards:
            meditation_state[configured_name] = meditation_completed
            task.log_info(
                f"冥想卡牌「{configured_name}」状态更新为"
                f"{'待冥想' if meditation_completed else '无需冥想'}"
            )

    if target_boxes:
        click_position = target_click_positions[0]
        task.log_info(
            f"卡牌闪光事件: 检测到target卡牌，点击位置{click_position}"
        )
        _remember_flash_click(task)
        battle_log.process_shot(task, "闪光选择")
        _move_and_click(task, *click_position)
        return True

    if not chosen_card:
        chosen_card, choose_reason = choose_flash_version(candidates)
        task.log_info(f"闪光优先级都没命中，选择「{chosen_card['name']}」（{choose_reason}）")

    options = [{"type": c.get("type"), "description": c.get("description")} for c in cards]
    key = (chosen_card["name"], cards.index(chosen_card))
    if getattr(task, "_last_flash_choice", (None, 0))[0] != key or time.time() - task._last_flash_choice[1] > 30:
        # 这个页面会连续识别好几帧，同一次选择只记一条
        battle_log.record(task, "闪光选择", card=chosen_card["name"], reason=choose_reason,
                          chosen=cards.index(chosen_card) + 1, options=options)
    task._last_flash_choice = (key, time.time())
    _remember_flash_click(task)
    battle_log.process_shot(task, "闪光选择")
    _move_and_click(task, chosen_card['x'], chosen_card['y'])
    return True


def choose_flash_version(cards):
    """闪光优先级都没命中时挑版本：先保留原来的类型（3 个版本里多数的类型，闪光可能把攻击牌变成技能牌，
    会打乱出牌优先级），同类型里挑描述中最大的百分比数值（伤害/护盾/治愈），一样大时取靠前的。返回 (牌, 理由)。"""
    types = [card.get("type") or "" for card in cards]
    main_type = max(types, key=types.count) if types else ""  # 一样多时取靠前的，结果固定
    same = [card for card in cards if (card.get("type") or "") == main_type] or cards

    def top_percent(card):
        return max((int(n) for n in re.findall(r"(\d+)\s*[%％]", card.get("description") or "")), default=-1)

    best = max(same, key=lambda card: (top_percent(card), -cards.index(card)))
    value = top_percent(best)
    return best, f"保留类型「{main_type}」" + (f"，最大数值 {value}%" if value >= 0 else "，都读不到数值取第一个")


_ESCAPE_INTENT_SECONDS = 30  # 决定撤退后多久内点「逃脱」算数（菜单里点一次、确认页再点一次）


def _open_escape_menu(task: TriggerTask, y: float):
    """决定撤退：记下时间再点右上角打开菜单，handle_escape 只在这之后才点「逃脱」。"""
    task._escape_requested_at = time.time()
    _move_and_click(task, 0.959, y)


def handle_escape(task: TriggerTask):
    """逃脱页面: 检测到逃脱按钮后点击逃脱。
    只有自己决定撤退（_open_escape_menu）后才点。菜单也会被意外打开：出牌时关卡牌弹窗按的 ESC 落到了战斗里、
    切窗口等。以前看到就点，满血放弃了好几轮（结算页写「信號消失」，实跑 20:49、22:05 等）。这时按 ESC 关掉菜单。"""
    escape_box = find_box_at_point(task, 0.952, 0.928)
    if escape_box and (
        _get_game_text(task, '逃脱') in escape_box.name
        or "脱逃" in escape_box.name
    ):
        if time.time() - getattr(task, '_escape_requested_at', 0) > _ESCAPE_INTENT_SECONDS:
            task.log_info("检测到逃脱页面，但没有要撤退（菜单是意外打开的），按 ESC 关掉")
            battle_log.anomaly(task, "意外打开撤退菜单", "没有要撤退，按 ESC 关掉菜单")
            task.send_key("esc")
            task.sleep(1)
            return True
        task.log_info("检测到逃脱页面，点击逃脱")
        task.click_box(escape_box)
        task.node_status["is_escaped"] = True
        task.sleep(0.5)
        return True
    return False


# def handle_battle_failed(task: TriggerTask):
#     """战斗失败页面: 记录失败并重置boss状态。"""
#     box = find_box_at_point(task, 0.291, 0.718)
#     if box and box.name == "战斗失败":
#         task.log_info("检测到战斗失败，记录失败并重置boss状态")
#         if hasattr(task, 'node_status'):
#             task.node_status['total_rounds'] += 1
#             task.log_info(f"战斗失败，total_rounds={task.node_status['total_rounds']}")
#             task.node_status['pass_final_boss_count'] = 0
#             task.node_status['reach_final_boss'] = False
#             task.node_status['final_boss_battle'] = False
#     return False

def handle_expedition_result(task: TriggerTask):
    """探险结果页面: 如果0.625,0.122处有"探险结果"，则为探险结果页面。
    如果0.928,0.122处有"完成"，则success_rounds+1。"""
    expedition_result_text = _get_game_text(task, "探险结果")
    title_box = find_box_at_point(task, 0.625, 0.122)
    if not (title_box and expedition_result_text in title_box.name):
        return False
    task.sleep(2)
    task.all_texts = _simplify_texts(task.ocr())
    title_box = find_box_at_point(task, 0.625, 0.122)
    if not (title_box and expedition_result_text in title_box.name):
        return False

    task.log_info("检测到探险结果页面")
    complete_box = find_box_at_point(task, 0.928, 0.122)
    failed_box = find_box_at_point(task, 0.296, 0.719)
    if hasattr(task, 'node_status'):
        task.node_status['total_rounds'] += 1
    # 只打第一层：打过第一层 boss 后撤退，撤退时游戏显示失败
    first_layer_done = task.node_status.get('pass_final_boss_count', 0) >= 1
    outcome = "失败"  # 写进「一轮结束」的 result
    if complete_box and "完成" in complete_box.name:
        if hasattr(task, 'node_status'):
            _count_round_success(task)
            task.log_info("出击模式探险结果: 成功")
            outcome = "成功"
    elif complete_box and "失败" in complete_box.name:
        if not _get_config_value(task, '只打第一层', False):
            task.log_info("出击模式探险结果: 失败")
        elif not first_layer_done:
            task.log_info("出击模式探险结果: 失败")
        else:
            task.log_info("出击模式探险结果: 只打第一层已完成")
            outcome = "只打第一层已完成"
    elif not complete_box and not failed_box:
        if _get_config_value(task, '只打第一层', False) and first_layer_done: # 完成第一层任务
            task.log_info("卡厄思模式探险结果: 成功")
            outcome = "成功"
        elif not _get_config_value(task, '只打第一层', False) and not task.node_status.get('is_escaped', 0): # 完成了任务且没有逃脱
            _count_round_success(task)
            task.log_info("卡厄思模式探险结果: 成功")
            outcome = "成功"
        else:
            task.log_info("卡厄思模式探险结果: 失败")
    else:
        task.log_info("卡厄思模式探险结果: 失败")
    task.log_info(f"探险完成，成功次数/总次数={task.node_status['success_rounds']}/{task.node_status['total_rounds']}")
    if hasattr(task, 'node_status'):
        success = task.node_status.get('round_success_counted', False)
        # 失败不截图：结算页看不出原因，要从这一轮的「进入节点」「战斗结束」等记录往回查
        battle_log.end_round(task, success=success, result=outcome,
                             escaped=task.node_status.get('is_escaped', False),
                             reached_boss=task.node_status.get('reach_final_boss', False),
                             passed_boss=task.node_status.get('pass_final_boss_count', 0),
                             nodes=task.node_status.get('node_count', 0),
                             rounds=f"{task.node_status['success_rounds']}/{task.node_status['total_rounds']}")
        reset_mission_status(task)
    return False


def _initial_node_status():
    """返回 node_status 的初始副本。"""
    return {"shop": False, "flash_or_rest": False, "reach_final_boss": False, "final_boss_battle": False,
            "pass_final_boss_count": 0, "total_rounds": 0, "success_rounds": 0,
            "node_count": 0, "enter_new_node": False, "node_type": "", "is_escaped": False,
            "save_target_member": False, "target_mask_card_position": -1,
            "get_specific_flash": False, "removed_card_count": 0,
            "neutral_card_count": 0,
            "flash_done_cards": []}  # 本局不用再为它进闪光的牌（闪光卡牌列表里的牌名）


def _initial_member_status():
    """返回本局主战员状态（装备、卡组、会合选到的队友职能）的初始副本。"""
    return {
        "equipment": {
            "names": ["", "", ""],
            "descriptions": ["", "", ""],
            "qualities": ["", "", ""],
        },
        "deck": {},
        "recruit_roles": {},
    }


def _count_round_success(task: TriggerTask):
    """本轮记一次成功。结算页会被连续识别好几帧、探险结果页也会再判一次成功，
    所以用 round_success_counted 保证每轮最多记一次（曾出现成功次数大于总次数，如 3/2）。"""
    if task.node_status.get('round_success_counted', False):
        return
    task.node_status['round_success_counted'] = True
    task.node_status['success_rounds'] += 1


def _finish_only_first_layer(task: TriggerTask) -> bool:
    """检查并完成只打第一层的退出操作：如果 pass_final_boss_count >= 1 且配置'只打第一层'为 True，则成功次数+1、点击退出并返回 True。"""
    if not (
        hasattr(task, 'node_status')
        and task.node_status.get('pass_final_boss_count', 0) >= 1
    ):
        return False

    if (
        _get_config_value(task, "首层刷特定闪光", False) is True
        and task.node_status.get("get_specific_flash", False) is False
    ):
        _count_round_success(task)
        task.log_info("未刷到指定闪光且已通关第一层，退出重刷")
        _open_escape_menu(task, 0.051)
        task.sleep(1)
        return True

    if _get_config_value(task, '只打第一层', False):
        _count_round_success(task)
        task.log_info(f"只打第一层任务已完成，success_rounds + 1 (当前: {task.node_status['success_rounds']}), 退出结算页面")
        _open_escape_menu(task, 0.051)
        task.sleep(1)
        return True
    return False


def reset_all_status(task: TriggerTask):
    """重置所有状态：恢复节点状态和目标主战员状态。"""
    if getattr(task, 'node_status', None) is not None:
        task.node_status = _initial_node_status()
    task.member_status = _initial_member_status()
    _reset_meditation_state(task)
    task._pending_removed_card_count = 0


def reset_mission_status(task: TriggerTask):
    """重置任务状态：保留任务统计和目标成员特征状态，重置其他状态。"""
    ns = getattr(task, 'node_status', None)
    if ns is not None:
        keep = {'total_rounds': ns.get('total_rounds', 0),
                'success_rounds': ns.get('success_rounds', 0),
                'save_target_member': ns.get('save_target_member', False)}
        task.node_status = _initial_node_status()
        task.node_status['total_rounds'] = keep['total_rounds']
        task.node_status['success_rounds'] = keep['success_rounds']
        task.node_status['save_target_member'] = keep['save_target_member']
    task.member_status = _initial_member_status()
    _reset_meditation_state(task)
    task._pending_removed_card_count = 0


def reset_layer_status(task: TriggerTask):
    """重置层状态：保留通关计数、任务统计和目标成员特征状态。"""
    ns = getattr(task, 'node_status', None)
    if ns is not None:
        keep = {'pass_final_boss_count': ns.get('pass_final_boss_count', 0),
                'total_rounds': ns.get('total_rounds', 0),
                'success_rounds': ns.get('success_rounds', 0),
                'save_target_member': ns.get('save_target_member', False),
                'target_mask_card_position': ns.get('target_mask_card_position', -1),
                'get_specific_flash': ns.get('get_specific_flash', False),
                'removed_card_count': ns.get('removed_card_count', 0),
                'neutral_card_count': ns.get('neutral_card_count', 0),
                'flash_done_cards': ns.get('flash_done_cards', []),
                'round_success_counted': ns.get('round_success_counted', False)}
        task.node_status = _initial_node_status()
        task.node_status['round_success_counted'] = keep['round_success_counted']
        task.node_status['pass_final_boss_count'] = keep['pass_final_boss_count']
        task.node_status['total_rounds'] = keep['total_rounds']
        task.node_status['success_rounds'] = keep['success_rounds']
        task.node_status['save_target_member'] = keep['save_target_member']
        task.node_status['target_mask_card_position'] = keep['target_mask_card_position']
        task.node_status['get_specific_flash'] = keep['get_specific_flash']
        task.node_status['removed_card_count'] = keep['removed_card_count']
        task.node_status['neutral_card_count'] = keep['neutral_card_count']
        task.node_status['flash_done_cards'] = list(keep['flash_done_cards'])


def handle_close_button(task: TriggerTask):
    """通用关闭按钮: 检测到关闭按钮则点击关闭。"""
    box = find_box_at_point(task, 0.512, 0.929)
    if box and box.name == "关闭":
        task.log_info("检测到关闭按钮，点击关闭")
        task.click_box(box)
        task.sleep(1)
        return True
    return False


def handle_card_assign(task: TriggerTask):
    """卡牌分配页面: 按奖励优先级刷新或跳过，并优先分配给目标主战员。"""
    title_box = find_box_at_point(task, 0.863, 0.133)
    assign_prompt = _get_game_text(task, "请选择要接受卡牌的主战员")
    if not (title_box and assign_prompt in title_box.name):
        return False

    task.log_info("检测到卡牌分配页面")

    purchase_title_region = (0.326, 0.057, 0.671, 0.210)
    purchase_title_boxes = [
        b for b in task.all_texts
        if purchase_title_region[0] <= (b.x + b.width / 2) / task.width <= purchase_title_region[2]
        and purchase_title_region[1] <= (b.y + b.height / 2) / task.height <= purchase_title_region[3]
    ]
    is_purchase_page = any("购买卡牌" in b.name for b in purchase_title_boxes)
    purchase_box = None
    cancel_box = None
    card_price = None
    current_credit = None
    if is_purchase_page:
        task.log_info("检测到购买卡牌页面")
        current_credit = _get_current_credit(task)
        purchase_bottom_boxes = [
            b for b in task.all_texts
            if 0.016 <= (b.x + b.width / 2) / task.width <= 0.995
            and 0.878 <= (b.y + b.height / 2) / task.height <= 0.996
        ]
        cancel_box = next((b for b in purchase_bottom_boxes if "取消" in b.name), None)
        purchase_box = next((b for b in purchase_bottom_boxes if "购买" in b.name), None)
        price_box = next(
            (b for b in purchase_bottom_boxes if re.fullmatch(r"\d+", b.name.strip())),
            None,
        )
        if price_box:
            card_price = _parse_discounted_price(price_box.name)
        task.log_info(
            f"购买卡牌页面: 当前信用点={current_credit}，"
            f"OCR价格=「{price_box.name if price_box else ''}」，实际价格={card_price}"
        )

        if not (cancel_box and purchase_box):
            task.log_info("购买卡牌页面未完整识别取消和购买按钮")
            if cancel_box:
                task.log_info("购买卡牌页面触发识别失败取消事件，点击「取消」")
                task.click_box(cancel_box)
                task.sleep(1)
                return True
            return False
        if card_price is None:
            task.log_info("购买卡牌页面未识别到价格，按价格低于当前信用点继续购买")
        if _neutral_card_limit_reached(task):
            neutral_card_count = task.node_status.get("neutral_card_count", 0)
            neutral_card_limit = _neutral_card_limit(task)
            task.log_info(
                f"本局已获得{neutral_card_count}张中立牌，"
                f"达到固定上限{neutral_card_limit}张，点击「取消」"
            )
            task.click_box(cancel_box)
            task.sleep(1)
            return True

    assigned_cards = recognize_cards(
        task,
        region=(0.101, 0.217, 0.291, 0.365),
        page="卡牌分配页面",
    )
    assigned_card = assigned_cards[0] if assigned_cards else None
    card_name = assigned_card["name"] if assigned_card else ""
    card_desc = assigned_card["description"] if assigned_card else ""
    task.log_info(f"待分配卡牌: 名称=「{card_name}」，描述=「{card_desc}」")

    bottom_boxes = [
        b for b in task.all_texts
        if 0.290 <= (b.x + b.width / 2) / task.width <= 0.998
        and 0.878 <= (b.y + b.height / 2) / task.height <= 0.997
    ]
    refresh_text = _get_game_text(task, "刷新")
    refresh_box = next((b for b in bottom_boxes if refresh_text in b.name), None)
    skip_box = next((b for b in bottom_boxes if "跳过" in b.name), None)
    refresh_count = None
    for bottom_box in bottom_boxes:
        count_match = re.search(r'(\d+)/(\d+)', bottom_box.name)
        if count_match:
            refresh_count = (int(count_match.group(1)), int(count_match.group(2)))
            break

    reward_priority_config = _get_card_list(task, "卡牌奖励优先级")
    has_reward_priority = any(
        isinstance(item, str) and item.strip()
        for item in reward_priority_config
    )
    priority = _get_card_reward_priority(task)
    matched_card_name = next(
        (config_name for config_name in priority
         if card_name and config_name
         and (config_name in card_name or card_name in config_name)),
        None,
    )
    if matched_card_name:
        task.log_info(f"卡牌「{card_name}」命中奖励优先级「{matched_card_name}」")
    else:
        task.log_info(f"卡牌「{card_name}」未命中奖励优先级")
        if is_purchase_page:
            task.log_info("购买卡牌未命中奖励优先级，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True
        if (
            has_reward_priority
            and refresh_box
            and refresh_count
            and refresh_count[0] > 0
        ):
            task.log_info(f"剩余刷新次数: {refresh_count[0]}/{refresh_count[1]}，点击刷新")
            battle_log.record(task, "卡牌分配", card=card_name, chosen="刷新", reason="未命中卡牌奖励优先级",
                              refresh=list(refresh_count))
            task.click_box(refresh_box)
            return True
        if skip_box:
            task.log_info("无可用刷新或刷新次数，点击跳过非优先级卡牌")
            battle_log.record(task, "卡牌分配", card=card_name, chosen="跳过", reason="未命中卡牌奖励优先级，没有刷新次数")
            task.click_box(skip_box)
            return True

    lv_texts = _find_member_level_tags(
        task,
        (0.426, 0.292, 0.473, 0.783),
        page="卡牌分配页面",
    )
    if not lv_texts:
        task.log_info("未找到主战员leveltag特征")
        if is_purchase_page:
            task.log_info("购买卡牌页面未找到可分配战员，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True
        return False

    target_member_index = _find_target_member_index(
        task, lv_texts, (0.484, 0.169, 0.652, 0.858)
    )

    available_members = []
    for index, level_box in enumerate(lv_texts):
        level_center_x = (level_box.x + level_box.width / 2) / task.width
        level_center_y = (level_box.y + level_box.height / 2) / task.height
        unavailable_box = find_box_at_point(
            task,
            level_center_x + 0.0615,
            level_center_y - 0.0795,
        )
        if unavailable_box and "无法获得" in unavailable_box.name:
            task.log_info(f"第{index + 1}号主战员无法获得该卡牌，排除")
            continue
        available_members.append((index, level_box))

    if not available_members:
        task.log_info("所有主战员均无法获得该卡牌")
        if is_purchase_page:
            task.log_info("购买卡牌无法分配给任何战员，点击「取消」")
            task.click_box(cancel_box)
            task.sleep(1)
            return True
        if skip_box:
            task.log_info("尝试点击跳过")
            task.click_box(skip_box)
            return True
        task.log_info("未找到跳过按钮")
        return False

    target_available = next(
        (member for member in available_members if member[0] == target_member_index),
        None,
    )
    chosen_idx, chosen_lv = target_available or available_members[0]
    if target_available:
        task.log_info(f"优先选择刷存档主战员（第{chosen_idx + 1}号）接受卡牌")
    else:
        task.log_info(f"优先选择第{chosen_idx + 1}号主战员接受卡牌")

    if (
        is_purchase_page
        and card_price is not None
        and current_credit <= card_price
    ):
        task.log_info(
            f"购买卡牌需要{card_price}信用点，当前{current_credit}，信用点不足，点击「取消」"
        )
        task.click_box(cancel_box)
        task.sleep(1)
        return True

    tracks_target_member = "刷存档主战员" in getattr(task, "default_config", {})
    if (
        target_member_index is not None and chosen_idx == target_member_index
    ) or (
        not tracks_target_member and chosen_idx == 0
    ):
        deck = _member_deck_state(task)
        deck[matched_card_name or card_name] = card_desc
    battle_log.record(task, "卡牌分配", card=card_name, matched=matched_card_name, member_index=chosen_idx + 1,
                      member="刷存档主战员" if target_available else "其他主战员",
                      available=[index + 1 for index, _ in available_members],
                      purchase=is_purchase_page, price=card_price if is_purchase_page else None)
    _move_and_click(task, 0.756, (chosen_lv.y + chosen_lv.height / 2) / task.height)
    task.sleep(1)
    if is_purchase_page:
        task.log_info(
            f"购买卡牌需要{card_price}信用点，当前{current_credit}，点击「购买」"
        )
        task.click_box(purchase_box)
        _record_neutral_card(task)
        task.sleep(1)
        return True
    _record_neutral_card(task)
    return False

def handle_held_cards_page(task: TriggerTask):
    """持有卡牌页面: 检测到持有卡牌则关闭页面。"""
    box = find_box_at_point(task, 0.500, 0.056)
    if box and box.name == _get_game_text(task, '持有卡牌'):
        task.log_info("检测到持有卡牌页面，点击关闭")
        _move_and_click(task, 0.966, 0.053)
        return True
    return False

def monster_panel_open(task: TriggerTask):
    """怪物信息面板是否开着（面板标题栏右侧的「弱点」）。"""
    box = find_box_at_point(task, 0.387, 0.107)
    return bool(box and "弱点" in box.name)


def close_monster_panel(task: TriggerTask, tries=0):
    """关怪物信息面板：偶数次点面板外的 (0.502, 0.092)，奇数次按 ESC。
    体型大的 Boss（如维亚迪乌斯）身体会伸到 (0.502, 0.092)，点下去反而又把面板点开，实跑中来回卡了两个小时。"""
    if tries % 2 == 0:
        _move_and_click(task, 0.502, 0.092)
    else:
        task.send_key("esc")


def handle_weakness_info(task: TriggerTask):
    """怪物信息页面: 检测到弱点信息则关闭页面；连续关不掉时轮流改用 ESC。"""
    if not monster_panel_open(task):
        task._weakness_tries = 0
        return False
    tries = getattr(task, "_weakness_tries", 0)
    task._weakness_tries = tries + 1
    task.log_info("检测到怪物信息页面，" + ("点击关闭" if tries % 2 == 0 else "点击没关掉，按 ESC 关闭"))
    close_monster_panel(task, tries)
    return True

def handle_minimizemap(task: TriggerTask):
    """地图页面: 检测到小地图按钮则点击关闭小地图。"""
    boxes = task.find_feature(feature_name="minimizemap")
    if boxes:
        task.log_info("检测到地图页面，点击关闭小地图")
        task.click_box(boxes[0])
        return True
    return False

def handle_non_battle_page(task: TriggerTask):
    """非出击/卡厄思页面: 检测到故事/营救/方舟城市时自动停止当前模式，优先级最高。"""
    box = find_box_at_point(task, 0.887, 0.160)
    if box and box.name == "故事":
        task.log_info("检测到故事页面，停止当前模式")
        task.disable()
        return True
    box = find_box_at_point(task, 0.101, 0.046)
    if box and box.name == "营救":
        task.log_info("检测到营救页面，停止当前模式")
        task.disable()
        return True
    box = find_box_at_point(task, 0.124, 0.049)
    if box and box.name == "方舟城市":
        task.log_info("检测到方舟城市页面，停止当前模式")
        task.disable()
        return True
    return False

def handle_unknown_page(task: TriggerTask):
    """检测到待确认的未知页面: 确认按钮不可点击时随机点击页面中央区域。"""
    box = find_box_at_point(task, 0.916, 0.931)
    if box and _clean_match(box.name, "确认") and not is_button_active(task, box):
        task.log_info("检测到待确认的未知页面，确认按钮不可点击，随机点击页面区域")
        import random
        rx = random.uniform(0.043, 0.972)
        ry = random.uniform(0.149, 0.843)
        _move_and_click(task, rx, ry)
        task.sleep(1)
        return True
    return False
