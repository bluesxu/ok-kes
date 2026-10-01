from ok import TriggerTask

import battle_log

from utils import (
    log_unhandled_page,
    _move_and_click, _simplify_texts, _get_config_value, _get_card_list, _get_route_priority, _get_game_text, _get_region_text, _get_current_hp_percent, _finish_only_first_layer,
    find_box_at_point, find_text, recognize_cards,
    _card_has_type_below, select_card,
    log_credit, log_node_status, handle_battle_crash, handle_close_page, handle_refine_equipment_credit,
    handle_center_confirm, handle_dont_show_again, handle_settlement, handle_skip,
    handle_destiny_choice, handle_main_member_flash,
    handle_card_reward, handle_equipment,
    handle_select_card, handle_copy_card_choice, handle_copy_member,
    handle_convert_card,
    handle_negotiation, handle_continue, handle_confirm, handle_enter,
    handle_event_task, handle_route_selection, handle_obtain_reward,
    handle_leave, handle_next_step, handle_select, handle_rest, handle_view_original, handle_weakness_info,
    handle_close_button,
    handle_card_assign, handle_non_battle_page, handle_minimizemap, handle_held_cards_page, handle_craft,
    handle_remove, handle_three_choice_card_remove, handle_flash, handle_reflash, handle_grant_flash, handle_copy, handle_convert,
    handle_equipment_recast,
    handle_stuck_log, handle_expedition_result,
    is_button_active, is_subsequence, _clean_match,
    handle_shop,
    handle_escape,
    # handle_stage_clear,
    handle_auto_stop,
)

import re
import random


# ------------------------- 卡厄思模式独有页面处理函数 -------------------------


STORAGE_CAPACITY_KEY = "存档储存上限大于等于多少pt"
# 游戏更新前的「存储数据价值 N 层级」门槛。界面上已换成「存档储存上限大于等于多少pt」，
# 还显示层级的页面（赛季卡厄思、获得法典、旧版零式系统卡片）按原来的默认值判断
_DATA_VALUE_LEVEL = 12
_STORAGE_READ_RETRIES = 3  # 零式系统页连续几帧读不到存档价值/上限，就不再重新合成，直接进入


def _match_storage_data_value(task: TriggerTask, text: str):
    """匹配当前游戏语言中的“存储数据价值+数字”，不要求末尾层级文本。"""
    storage_data_text = re.escape(_get_game_text(task, "存储数据"))
    return re.search(rf"{storage_data_text}价值\s*(\d+)", text)


def _match_storage_capacity(task: TriggerTask, text: str):
    """匹配“存储数据…上限+数字pt”（国际服「存檔資料儲存上限170pt」）。
    2026-09-30 更新后法典卡片不再显示存储数据价值层级，改为显示存档储存上限。"""
    storage_data_text = re.escape(_get_game_text(task, "存储数据"))
    return re.search(rf"{storage_data_text}.{{0,4}}上限\s*(\d+)\s*pt", text, re.IGNORECASE)


def handle_season_chaos_initial_page(task: TriggerTask):
    """赛季卡厄思初始页面：存档储存上限（旧版为存储数据价值层级）不足时再次观测。"""
    title_text = _get_region_text(task, (0.075, 0.014, 0.305, 0.090))
    if "卡厄思" not in title_text:
        return False

    observe_region = (0.659, 0.715, 0.993, 0.851)
    observe_boxes = [
        box for box in task.all_texts
        if observe_region[0] <= (box.x + box.width / 2) / task.width <= observe_region[2]
        and observe_region[1] <= (box.y + box.height / 2) / task.height <= observe_region[3]
        and "再次观测" in box.name
    ]
    if not observe_boxes:
        return False

    task.log_info("检测到赛季卡厄思初始页面")
    value_text = _get_region_text(task, (0.684, 0.308, 0.982, 0.754))
    value, unit = _discovery_option_value(task, value_text)
    if value is None:
        task.log_info(f"未识别到存档储存上限或存储数据价值，区域文本=「{value_text}」")
        if battle_log.once(task, "赛季初始页读不到"):
            battle_log.anomaly(task, "赛季初始页读不到存档价值", f"区域文本=「{value_text}」")
        return False

    required = _discovery_required(task)[unit]
    task.log_info(f"当前存档={value}{unit}，要求大于等于{required}{unit}")
    if value >= required:
        if battle_log.once(task, "赛季初始页进入"):
            battle_log.record(task, "赛季初始页", value=value, unit=unit, required=required, decision="进入")
        return False

    task.log_info("存档价值不足，点击再次观测")
    battle_log.reroll(task, "赛季初始页", value=value, unit=unit, required=required)
    task.click_box(observe_boxes[0])
    task.sleep(1)
    return True


def _log_zero_system_enter(task, value, unit, required):
    if battle_log.once(task, "零式系统进入"):
        battle_log.record(task, "零式系统", value=value, unit=unit, required=required, decision="进入")


def handle_zero_system_initial_page(task: TriggerTask):
    """零式系统初始页面：存储数据价值不足时进入重新合成。"""
    title_text = _get_region_text(task, (0.076, 0.011, 0.291, 0.106))
    if _get_game_text(task, "零式系统") not in title_text:
        return False

    enter_text = _get_region_text(task, (0.681, 0.850, 0.988, 0.972))
    if "进入" not in enter_text:
        return False

    task.log_info("检测到零式系统初始页面")
    value_text = _get_region_text(task, (0.685, 0.317, 0.980, 0.825))
    capacity_match = _match_storage_capacity(task, value_text)
    value_match = _match_storage_data_value(task, value_text)
    if capacity_match:
        task._storage_read_misses = 0
        capacity = int(capacity_match.group(1))
        required_pt = int(_get_config_value(task, STORAGE_CAPACITY_KEY, 0))
        task.log_info(f"零式系统当前存档储存上限={capacity}pt，要求大于等于{required_pt}pt")
        value, unit, required = capacity, "pt", required_pt
        if capacity >= required_pt:
            _log_zero_system_enter(task, value, unit, required)
            return False
    elif value_match:
        task._storage_read_misses = 0
        required_level = _DATA_VALUE_LEVEL
        data_value_level = int(value_match.group(1))
        task.log_info(
            f"零式系统当前存储数据价值={data_value_level}层级，"
            f"要求大于等于{required_level}层级"
        )
        value, unit, required = data_value_level, "层级", required_level
        if data_value_level >= required_level:
            _log_zero_system_enter(task, value, unit, required)
            return False
    else:
        # 以前读不到就重新合成，游戏改版后读不到的是格式而不是数字，会一直重新合成下去
        misses = getattr(task, "_storage_read_misses", 0) + 1
        task._storage_read_misses = misses
        task.log_info(f"零式系统未识别到存储数据价值或存档储存上限（第{misses}次），区域文本=「{value_text}」")
        if misses >= _STORAGE_READ_RETRIES:
            task._storage_read_misses = 0
            task.log_info("连续读不到存档价值，不再重新合成，直接进入")
            battle_log.anomaly(task, "零式系统读不到存档价值", f"区域文本=「{value_text}」")
            return False
        task.sleep(1)
        return True

    task.log_info("存档价值未达要求，点击进入重新合成")
    battle_log.reroll(task, "零式系统", value=value, unit=unit, required=required)
    _move_and_click(task, 0.968, 0.153)
    task.sleep(1)
    return True


def handle_disable_adaptation_distortion(task: TriggerTask):
    """刷空档时关闭已启用的适应畸变。"""
    if _get_config_value(task, "刷空档", False) is not True:
        return False

    page_text = _get_region_text(task, (0.480, 0.050, 0.573, 0.136))
    adaptation_text = _get_game_text(task, "适应畸变")
    if adaptation_text not in page_text:
        return False

    relative_x, relative_y = 0.575, 0.093
    pixel_x = min(task.width - 1, max(0, round(relative_x * task.width)))
    pixel_y = min(task.height - 1, max(0, round(relative_y * task.height)))
    blue, green, red = (
        int(value) for value in task.frame[pixel_y, pixel_x, :3]
    )
    rgb = (red, green, blue)
    target_rgb = (255, 186, 55)
    color_matches = max(
        abs(actual - target)
        for actual, target in zip(rgb, target_rgb)
    ) <= 35
    task.log_info(
        f"适应畸变开关颜色RGB={rgb}，"
        f"是否接近{target_rgb}={color_matches}"
    )
    if not color_matches:
        return False

    task.log_info("刷空档检测到适应畸变已开启，点击关闭")
    _move_and_click(task, relative_x, relative_y)
    task.sleep(1)
    return False


def _save_runtime_feature(task: TriggerTask, feature_name: str, region, target_region=None):
    """将当前帧指定区域保存为运行时特征，可按目标区域尺寸缩放。"""
    import os
    import cv2
    from ok.feature.Box import Box
    from ok.feature.Feature import Feature
    from ok.util.config import Config
    from ok.util.file import get_relative_path

    x1, y1, x2, y2 = region
    left = round(x1 * task.width)
    top = round(y1 * task.height)
    right = round(x2 * task.width)
    bottom = round(y2 * task.height)
    feature_mat = task.frame[top:bottom, left:right, :3].copy()
    if feature_mat.size == 0:
        task.log_error(f"保存特征「{feature_name}」失败：截图区域为空")
        return False

    if target_region is not None:
        target_x1, target_y1, target_x2, target_y2 = target_region
        left = round(target_x1 * task.width)
        top = round(target_y1 * task.height)
        right = round(target_x2 * task.width)
        bottom = round(target_y2 * task.height)
        feature_mat = cv2.resize(
            feature_mat,
            (right - left, bottom - top),
            interpolation=cv2.INTER_AREA,
        )

    feature_set = task.executor.feature_set
    with feature_set.lock:
        feature_set.feature_dict[feature_name] = Feature(feature_mat, left, top)
        feature_set.box_dict[feature_name] = Box(
            left, top, right - left, bottom - top, 1.0, feature_name
        )

    config_folder = get_relative_path(Config.config_folder)
    os.makedirs(config_folder, exist_ok=True)
    feature_path = os.path.join(config_folder, f"{feature_name}.png")
    if cv2.imwrite(feature_path, feature_mat):
        task.log_info(
            f"已将主战员头像保存为特征「{feature_name}」：{feature_path}"
        )
    else:
        task.log_info(f"保存特征图片失败：{feature_path}")
    return True


def handle_archive_target_member(task: TriggerTask):
    """信息统计页面：按配置记录刷存档时使用的目标主战员特征。"""
    search_box = task.box_of_screen(0.005, 0.018, 0.080, 0.343)
    member_info = task.find_one(
        feature_name=["memberinfo", "memberinfo2"], box=search_box
    )
    if not member_info:
        return False

    task.log_info(
        f"检测到信息统计页面，点击「{member_info.name}」，相似度={member_info.confidence:.4f}"
    )
    task.click_box(member_info)
    task.sleep(0.5)
    _move_and_click(task, 0.201, 0.056)
    task.sleep(0.5)

    target_name = _get_config_value(task, "刷存档主战员", "海德玛丽")
    if not target_name:
        task.log_info("未配置刷存档主战员，关闭信息统计页面")
        _move_and_click(task, 0.960, 0.054)
        task.sleep(1)
        return True

    member_positions = [(0.159, 0.368), (0.432, 0.368), (0.705, 0.369)]
    member_regions = [
        (0.233, 0.151, 0.301, 0.269),
        (0.502, 0.151, 0.570, 0.269),
        (0.780, 0.151, 0.848, 0.269),
    ]
    task.all_texts = _simplify_texts(task.ocr())
    member_names = []
    for x, y in member_positions:
        name_box = find_box_at_point(task, x, y)
        member_names.append(name_box.name.strip() if name_box else "")
    task.log_info(f"信息统计页面主战员：{member_names}")

    for index, member_name in enumerate(member_names):
        if member_name and (target_name in member_name or member_name in target_name):
            if _save_runtime_feature(task, "target_member", member_regions[index]):
                _save_runtime_feature(
                    task,
                    "target_member_small",
                    member_regions[index],
                    target_region=(0.534, 0.225, 0.587125, 0.316667),
                )
                _save_runtime_feature(
                    task,
                    "target_member_in_select_card",
                    member_regions[index],
                    target_region=(
                        0.117708333,
                        0.178703704,
                        0.175520833,
                        0.278703704,
                    ),
                )
                _save_runtime_feature(
                    task,
                    "target_member_tiny",
                    member_regions[index],
                    target_region=(
                        0.65790625,
                        0.470962963,
                        0.70009375,
                        0.545037037,
                    ),
                )
                _save_runtime_feature(
                    task,
                    "target_member_in_mask_card",
                    member_regions[index],
                    target_region=(
                        0.663635417,
                        0.481148148,
                        0.694364583,
                        0.534851852,
                    ),
                )
                _save_runtime_feature(
                    task,
                    "target_member_large",
                    member_regions[index],
                    target_region=(0.218, 0.320, 0.28675, 0.439444444),
                )
                task.log_info(
                    f"第{index + 1}个主战员「{member_name}」命中刷存档主战员「{target_name}」"
                )
            task.node_status["save_target_member"] = True
            _move_and_click(task, 0.960, 0.054)
            task.sleep(1)
            return True

    task.log_info(f"三个主战员均未命中刷存档主战员「{target_name}」")
    _move_and_click(task, 0.960, 0.054)
    task.node_status["save_target_member"] = True
    task.sleep(1)
    return True


def handle_save_target_member(task: TriggerTask):
    """主界面：进入信息统计页面获取刷存档目标主战员头像特征。"""
    if not _get_current_hp_percent(task):
        return False

    target_name = _get_config_value(task, "刷存档主战员", "海德玛丽")
    if not target_name:
        return False
    if task.node_status.get("save_target_member", False):
        return False

    task.log_info("前往获取刷存档主战员头像特征")
    _move_and_click(task, 0.182, 0.081)
    task.sleep(2)
    return True

def handle_battle_auto_check(task: TriggerTask):
    """战斗页面: 检测手牌数并检查自动战斗是否开启，如关闭则开启。"""
    box = find_box_at_point(task, 0.512, 0.969)
    if not (box and re.search(r'\d+/10', box.name)):
        return False

    attack_box = task.find_one(
        feature_name="start_attack_event",
        box=task.box_of_screen(0.394, 0.779, 0.600, 0.858),
    )
    if attack_box:
        task.log_info(
            f"战斗页面检测到start_attack_event特征，"
            f"相似度={attack_box.confidence:.4f}，点击发起攻击"
        )
        task.click_box(attack_box)
        task.sleep(1)
        return True

    from ok.feature.Box import Box
    from ok.util.color import calculate_color_percentage
    auto_box = Box(
        x=int(0.877 * task.width),
        y=int(0.050 * task.height),
        width=int(4),
        height=int(4)
    )
    white_ratio = calculate_color_percentage(
        task.frame,
        {'r': (255, 255), 'g': (255, 255), 'b': (255, 255)},
        box=auto_box
    )
    task.log_info(f"自动战斗按钮区域白色占比: {white_ratio:.2%}")
    if white_ratio > 0.02:
        task.log_info("自动战斗处于关闭状态，点击开启")
        _move_and_click(task, 0.880, 0.056)
        task.sleep(0.5)

    # 如果已到达最终boss节点，标记boss战状态
    if hasattr(task, 'node_status') and task.node_status.get('reach_final_boss', False):
        task.node_status['final_boss_battle'] = True
        task.log_info("检测到最终boss战斗开始，final_boss_battle=True")
    return True


_CHAOS_SYNTH_POINT = (0.460, 0.924)  # 获得法典页底部「卡厄思合成」按钮（花卡厄思宝珠重抽三个选项）


def _discovery_option_value(task, text):
    """获得法典选项的 (比较值, 单位)：优先读「存档资料储存上限 N pt」（2026-09-30 更新后），
    其次旧版「存储数据价值 N」；「无法获得存档资料」的选项两者都没有，返回 (None, None)。"""
    capacity = _match_storage_capacity(task, text)
    if capacity:
        return int(capacity.group(1)), "pt"
    level = _match_storage_data_value(task, text)
    if level:
        return int(level.group(1)), "层级"
    return None, None


def _discovery_required(task):
    return {"pt": int(_get_config_value(task, STORAGE_CAPACITY_KEY, 0) or 0), "层级": _DATA_VALUE_LEVEL}


def handle_discovery_select(task: TriggerTask): #忘了按个页面要用
    """获得法典页面：选存档储存上限达到配置值的选项（几个都达标时选上限最低的），都不达标就点「卡厄思合成」重抽。
    2026-09-30 更新后选项显示「存档资料储存上限 N pt」，底部没有「取消」了，只有一般合成、卡厄思合成和确认。"""
    title_text = _get_region_text(task, (0.313, 0.010, 0.670, 0.193))
    if "获得法典" not in title_text:
        return False

    task.log_info(f"检测到发现选择页面，标题区域文本=「{title_text}」")
    required = _discovery_required(task)
    option_regions = [
        (0.058, 0.358, 0.315, 0.800),
        (0.370, 0.356, 0.629, 0.796),
        (0.682, 0.356, 0.940, 0.796),
    ]
    qualified, seen = [], []
    for index, region in enumerate(option_regions):
        option_text = _get_region_text(task, region)
        value, unit = _discovery_option_value(task, option_text)
        seen.append(value and f"{value}{unit}")
        if value is None:
            task.log_info(f"发现选项{index + 1}没有存档储存上限（无法获得存档资料），区域文本=「{option_text}」")
            continue
        task.log_info(f"发现选项{index + 1}存档={value}{unit}，要求大于等于{required[unit]}{unit}")
        if value >= required[unit]:
            qualified.append((value, -index, index, region))

    if qualified:
        # 达标的选项里选上限最低的（只要达到门槛就够，上限越高越难拿满）；一样时选靠左的
        value, _, index, region = min(qualified, key=lambda option: (option[0], option[2]))
        task.log_info(f"发现选项{index + 1}满足要求（{value}），点击该选项")
        battle_log.record(task, "获得法典", options=seen, chosen=index + 1, required=required)
        _move_and_click(task, (region[0] + region[2]) / 2, (region[1] + region[3]) / 2)
        task.sleep(1)
        return True

    task.log_info("没有发现选项满足存档储存上限要求，点击「卡厄思合成」重抽")
    battle_log.reroll(task, "获得法典", options=seen, required=required)
    _move_and_click(task, *_CHAOS_SYNTH_POINT)
    task.sleep(1)
    return True


# 本函数已按「存档资料储存上限」判断：speedup 看到这个标记就不再用自己的修正包装它
handle_zero_system_initial_page.handles_storage_capacity = True


def handle_zero_system_home(task: TriggerTask):
    """零式系统首页: 点击法典。"""
    title = find_box_at_point(task, 0.120, 0.046)
    codex = find_box_at_point(task, 0.812, 0.469)
    if title and _get_game_text(task, '零式系统') in title.name and codex and codex.name == "法典":
        task.log_info("检测到零式系统首页，点击法典")
        task.click_box(codex)
        task.sleep(2)
        return True
    return False


def handle_codex_search(task: TriggerTask):
    """法典搜索页面: 点击搜索新坐标。"""
    title = find_box_at_point(task, 0.5, 0.438)
    if not (title and title.name == "法典"):
        return False
    task.log_info("检测到法典搜索页面，点击搜索新坐标")
    _move_and_click(task, 0.5, 0.760)
    task.sleep(2)
    return True


def handle_memory_elimination(task: TriggerTask):
    """记忆消除页面: 点击记忆消除按钮。"""
    box = find_box_at_point(task, 0.589, 0.703)
    if box and _get_game_text(task, '记忆消除') in box.name:
        task.log_info("检测到记忆消除页面，点击记忆消除")
        task.click_box(box)
        task.sleep(0.5)
        return True
    return False


def handle_threat_detection(task: TriggerTask):
    """「威胁侦测」过场页（零式系统选完记忆卡后提示「记忆的尽头BOSS怪物已变更为…」）：点屏幕没反应，按 ESC 关掉。"""
    if not find_text(task, r'威胁侦测'):
        return False
    task.log_info("检测到威胁侦测页面，按 ESC 关闭")
    task.send_key("esc")
    task.sleep(1)
    return True


_CARVE_BUTTON_COLOR = (0.86, 0.795)   # 右下「记忆雕琢」按钮上文字和水晶之间的底色：橙色可点，灰色不可点


def _carve_button_active(task: TriggerTask):
    frame = getattr(task, "frame", None)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    x, y = int(_CARVE_BUTTON_COLOR[0] * width), int(_CARVE_BUTTON_COLOR[1] * height)
    blue, _, red = (float(v) for v in frame[y - 6:y + 6, x - 6:x + 6, :3].reshape(-1, 3).mean(axis=0))
    return red > blue + 60


def handle_memory_carving(task: TriggerTask):
    """「记忆雕琢」页（零式系统 boss 后选「雕琢记忆」进来）：一直点右边的「记忆雕琢」，按钮变灰（水晶不够）后点「离开」。
    左边的「记忆加工」不点。"""
    title = find_box_at_point(task, 0.501, 0.128)
    if not (title and "记忆雕琢" in title.name):
        return False
    button = next((box for box in task.all_texts if "记忆雕琢" in box.name
                   and (box.x + box.width / 2) / task.width > 0.6 and (box.y + box.height / 2) / task.height > 0.7), None)
    rate = next((box.name for box in task.all_texts if "成功机率" in box.name), "")
    active = _carve_button_active(task)
    if button and active:
        task.log_info(f"记忆雕琢：点击「记忆雕琢」（{rate}）")
        battle_log.record(task, "记忆雕琢", choice="雕琢", rate=rate)
        task.click_box(button)
        task.sleep(2)
        return True
    if active is None:
        return False
    leave = next((box for box in task.all_texts if _clean_match(box.name, "离开")
                  and (box.y + box.height / 2) / task.height > 0.85), None)
    if leave is None:
        return False
    task.log_info("记忆雕琢：按钮已变灰，点击离开")
    battle_log.record(task, "记忆雕琢", choice="离开", rate=rate)
    task.click_box(leave)
    task.sleep(1)
    return True


def handle_event_dialog(task: TriggerTask):
    """剧情对话停住时按空格翻页，与出击模式共用 utils_sortie 里的实现
    （utils_sortie 在模块开头就导入本模块，这里放到调用时再导入，避免循环导入）。"""
    from utils_sortie import handle_event_dialog as handle
    return handle(task)


def handle_chaos_craft(task: TriggerTask):
    """卡厄思合成页面: 检测"卡厄思合成"(0.774,0.925)或"免费合成"(0.563,0.922)按钮，点击并等待。"""
    box = find_box_at_point(task, 0.774, 0.925)
    if box and "卡厄思合成" in box.name:
        task.log_info(f"检测到卡厄思合成页面，点击「{box.name}」")
        task.click_box(box)
        task.sleep(2)
        return True
    box = find_box_at_point(task, 0.563, 0.922)
    if box and "免费合成" in box.name:
        task.log_info(f"检测到卡厄思合成页面，点击「{box.name}」")
        task.click_box(box)
        task.sleep(2)
        return True
    return False


def handle_conquer_difficulty(task: TriggerTask):
    """征服新难度页面: 检测到'征服新难度'则点击空白处关闭。"""
    box = find_box_at_point(task, 0.502, 0.572)
    if box and "征服新难度" in box.name:
        task.log_info("检测到征服新难度页面，点击关闭")
        _move_and_click(task, 0.502, 0.943)
        task.sleep(1)
        return True
    return False


# ------------------------- 卡厄思模式独有页面处理函数（续） -------------------------

def _log_engraving(task, choice, desc, wanted, **fields):
    """详细日志：面具卡牌刻印页的一次操作（确认 / 刷新 / 跳过）；「确认」这页会停好几帧，每轮只记一次。"""
    if choice == "确认" and not battle_log.once(task, f"面具刻印确认:{desc}"):
        return
    battle_log.record(task, "面具刻印", choice=choice, description=desc, wanted=wanted, **fields)


def handle_chaos_mask_engraving(task: TriggerTask):
    """面具卡牌刻印获取页面: 如果0.499,0.126处文本包含"面具卡牌刻印"，则为该页面。
    如果0.921,0.931处_clean_match"确认"后，检测0.495,0.221处是否包含"替换"：
      - 有"替换"（A逻辑）：点击刻印1，获取刻印1描述；命中配置则点击刻印2再判断，
        未命中则检查新刻印描述或刷新/跳过。
      - 无"替换"（原逻辑）：直接检查刻印描述，命中则return False，否则刷新或跳过。"""
    title_box = find_box_at_point(task, 0.499, 0.126)
    if not (title_box and "面具卡牌刻印" in title_box.name):
        return False

    task.log_info("检测到面具卡牌刻印获取页面")

    # 检查确认按钮是否可用
    confirm_box = find_box_at_point(task, 0.921, 0.931)
    if not (confirm_box and _clean_match(confirm_box.name, "确认")):
        return False

    specify_text = _get_config_value(task, '面具卡牌刻印', "自身攻击卡牌伤害总量提升30%")
    new_desc_region = (0.589, 0.601, 0.902, 0.762)
    slot_desc_region = (0.589, 0.336, 0.898, 0.487)

    # 检测是否处于替换模式
    replace_box = find_box_at_point(task, 0.495, 0.221)
    if replace_box and _get_game_text(task, "替换") in replace_box.name:
        task.log_info("检测到替换模式（A逻辑）")

        # ===== A逻辑：刻印1 =====
        task.log_info("点击刻印1")
        _move_and_click(task, 0.399, 0.472)
        task.sleep(0.5)

        slot_desc = _get_region_text(task, slot_desc_region)
        task.log_info(f"刻印1描述: {slot_desc}")

        if specify_text not in slot_desc:
            # 刻印1未命中，检查新刻印
            new_desc = _get_region_text(task, new_desc_region)
            task.log_info(f"新刻印描述: {new_desc}")
            if specify_text in new_desc:
                task.log_info(f"新刻印命中「{specify_text}」，交给确认按钮")
                _log_engraving(task, "确认", new_desc, specify_text)
                return False
            # 检查刷新
            refresh_box = find_box_at_point(task, 0.916, 0.810)
            if refresh_box:
                match = re.search(r'(\d+)/3', refresh_box.name)
                if match:
                    remaining = int(match.group(1))
                    task.log_info(f"刻印1未命中，剩余刷新次数: {remaining}")
                    if remaining > 0:
                        task.log_info(f"点击刷新")
                        _log_engraving(task, "刷新", None, specify_text, remaining=remaining)
                        _move_and_click(task, 0.916, 0.810)
                        task.sleep(1)
                        return True
            # 无法刷新，点击跳过
            task.log_info("无法刷新，点击跳过")
            _log_engraving(task, "跳过", None, specify_text)
            _move_and_click(task, 0.747, 0.932)
            task.sleep(1)
            return True

        # ===== 刻印1命中，继续刻印2 =====
        task.log_info("刻印1命中，点击刻印2")
        _move_and_click(task, 0.399, 0.610)
        task.sleep(0.5)

        slot2_desc = _get_region_text(task, slot_desc_region)
        task.log_info(f"刻印2描述: {slot2_desc}")

        if specify_text in slot2_desc:
            # 刻印2也命中，点击跳过
            task.log_info("刻印2也命中配置，点击跳过")
            _log_engraving(task, "跳过", slot2_desc, specify_text, reason="两个刻印都已是指定刻印")
            _move_and_click(task, 0.747, 0.932)
            task.sleep(1)
            return True

        # 刻印2未命中，检查新刻印
        new_desc = _get_region_text(task, new_desc_region)
        task.log_info(f"新刻印描述: {new_desc}")
        if specify_text in new_desc:
            task.log_info(f"新刻印命中「{specify_text}」，交给确认按钮")
            _log_engraving(task, "确认", new_desc, specify_text)
            return False

        # 检查刷新
        refresh_box = find_box_at_point(task, 0.916, 0.810)
        if refresh_box:
            match = re.search(r'(\d+)/3', refresh_box.name)
            if match:
                remaining = int(match.group(1))
                task.log_info(f"刻印2未命中，剩余刷新次数: {remaining}")
                if remaining > 0:
                    task.log_info(f"点击刷新")
                    _log_engraving(task, "刷新", None, specify_text, remaining=remaining)
                    _move_and_click(task, 0.916, 0.810)
                    task.sleep(1)
                    return True

        # 无法刷新，点击跳过
        task.log_info("无法刷新，点击跳过")
        _log_engraving(task, "跳过", None, specify_text)
        _move_and_click(task, 0.747, 0.932)
        task.sleep(1)
        return True

    # ===== 原逻辑：无替换模式 =====
    task.log_info("无替换模式，执行原逻辑")
    desc_text = _get_region_text(task, (0.583, 0.463, 0.937, 0.621))
    task.log_info(f"刻印描述: {desc_text}")

    if specify_text in desc_text:
        task.log_info(f"刻印描述包含「{specify_text}」，交给确认按钮处理")
        _log_engraving(task, "确认", desc_text, specify_text)
        return False

    # 未命中，检查可刷新次数
    refresh_box = find_box_at_point(task, 0.913, 0.667)
    if refresh_box:
        match = re.search(r'(\d+)/3', refresh_box.name)
        if match:
            remaining = int(match.group(1))
            task.log_info(f"未命中指定刻印，剩余刷新次数: {remaining}")
            if remaining > 0:
                task.log_info(f"剩余刷新次数{remaining}>0，点击刷新")
                _log_engraving(task, "刷新", desc_text, specify_text, remaining=remaining)
                _move_and_click(task, 0.913, 0.667)
                task.sleep(1)
                return True

    task.log_info("无可刷新次数，跳过")
    return False


def handle_mask_card(task: TriggerTask):
    """面具获得卡牌页面: 根据0.507,0.090处的面具获得提示判断页面。
    检测0.120,0.228,0.945,0.418范围内是否有三个"人格面具"文本，
    如果不足三个则说明已选择过人格面具，点击跳过。
    否则提取三张卡牌描述区域文本，匹配配置中"指定面具卡牌"内容。
    匹配成功则点击对应卡牌，否则刷新或跳过。"""
    box = find_box_at_point(task, 0.507, 0.090)
    is_mask_card_page = (
        box
        and "面具" in box.name
        and "获得" in box.name
        and "卡牌" in box.name
    )
    if not is_mask_card_page:
        return False

    task.log_info("检测到面具卡牌获得页面")
    specify_text = _get_config_value(task, '指定面具卡牌', "丢弃最多2张卡牌")
    specify_text = specify_text.strip() if isinstance(specify_text, str) else ""
    if not specify_text:
        task.log_info("指定面具卡牌配置为空，直接点击跳过")
        skip_box = find_text(task, r'跳过')
        if skip_box:
            task.click_box(skip_box)
            task.sleep(0.5)
        return True

    cards = recognize_cards(task, page="人格面具卡牌获得页面")
    mask_cards = [card for card in cards if "人格面具" in card["name"]]
    if len(mask_cards) < 3:
        task.log_info("已选择过人格面具，点击跳过")
        skip_box = find_text(task, r'跳过')
        if skip_box:
            task.click_box(skip_box)
            task.sleep(0.5)
            # _move_and_click(task, 0.654, 0.626)
        return True

    task.log_info("检测到3张人格面具卡牌")

    mask_position = task.node_status.get("target_mask_card_position", -1)
    if mask_position == -1:
        target_region = task.box_of_screen(0.006, 0.010, 0.081, 0.131)
        for index, card in enumerate(mask_cards):
            task.log_info(
                f"人格面具卡牌归属检测: 长按从左到右第{index + 1}张卡牌"
            )
            click_x = int(card["x"] * task.width)
            click_y = int(card["y"] * task.height)
            task.move_relative(card["x"], card["y"])
            task.mouse_down(click_x, click_y, key="left")
            task.sleep(2)
            task.mouse_up(key="left")
            task.sleep(1)

            target_member = task.find_one(
                feature_name="target_member_in_mask_card",
                box=target_region,
                threshold=0.6,
            ) if task.feature_exists("target_member_in_mask_card") else None
            if target_member:
                task.node_status["target_mask_card_position"] = index
                mask_position = index
                task.log_info(
                    f"人格面具卡牌归属检测: 第{index + 1}张属于目标主战员，"
                    f"相似度={target_member.confidence:.4f}"
                )
            else:
                task.log_info(
                    f"人格面具卡牌归属检测: 第{index + 1}张不属于目标主战员"
                )

            task.log_info("人格面具卡牌详情页面触发关闭事件，点击固定关闭位置")
            _move_and_click(task, 0.513, 0.931)
            task.sleep(2)
            if target_member:
                break

    if not 0 <= mask_position < len(mask_cards):
        task.log_info("未识别到目标主战员的人格面具卡牌，默认启用第一张")
        mask_position = 0
    enabled_mask_cards = [mask_cards[mask_position]]
    task.log_info(f"本次启用从左到右第{mask_position + 1}张人格面具卡牌")

    for card in enabled_mask_cards:
        desc_text = card["description"]
        task.log_info(f"卡牌{mask_position + 1}描述: {desc_text}")
        if is_subsequence(specify_text, desc_text):
            task.log_info(
                f"卡牌{mask_position + 1}描述子序列命中「{specify_text}」，点击该卡牌"
            )
            _move_and_click(task, card["x"], card["y"])
            task.sleep(0.5)
            return True

    # 未匹配到指定卡牌，检查剩余刷新次数
    refresh_box = find_box_at_point(task, 0.313, 0.931)
    if refresh_box:
        match = re.search(r'(\d+)/3', refresh_box.name)
        if match:
            remaining = int(match.group(1))
            task.log_info(f"未匹配到指定面具卡牌，剩余刷新次数: {remaining}")
            if remaining > 0:
                task.log_info(f"剩余刷新次数{remaining}>0，点击刷新")
                _move_and_click(task, 0.313, 0.931)
                task.sleep(1)
                return True

    task.log_info("无刷新次数或无需刷新，点击跳过")
    skip_box = find_text(task, r'跳过')
    if skip_box:
        task.click_box(skip_box)
        task.sleep(0.5)
        # _move_and_click(task, 0.654, 0.626)
    return True


def handle_data_collected(task: TriggerTask):
    """存储数据收集完成页面：按存档价值阈值决定是否删除。"""
    box = find_box_at_point(task, 0.505, 0.111)
    if box and _get_game_text(task, '存储数据收集完成') in box.name:
        retain_threshold = _get_config_value(
            task, '保留大于多少TB的存档', 62000
        )
        if retain_threshold <= 0:
            task.log_info("保留大于多少TB的存档设置为0，保留全部存档")
            return False

        for feature_name in ["deletecards", "deletecards2", "deletecards3"]:
            features = task.find_feature(feature_name=feature_name) or []
            for feature in features:
                feature_center_x = (feature.x + feature.width / 2) / task.width
                feature_center_y = (feature.y + feature.height / 2) / task.height
                value_box = find_box_at_point(
                    task,
                    feature_center_x - 0.107,
                    feature_center_y - 0.025,
                )
                value_match = (
                    re.search(r'\d+', value_box.name.replace(',', ''))
                    if value_box else None
                )
                archive_value = int(value_match.group()) if value_match else None
                if archive_value is not None and archive_value > retain_threshold:
                    task.log_info(
                        f"{feature_name}对应存档价值{archive_value}TB高于保留阈值"
                        f"{retain_threshold}TB，跳过删除"
                    )
                    continue
                if archive_value is None:
                    task.log_info(
                        f"{feature_name}未识别到存档价值，按原逻辑点击删除"
                    )
                else:
                    task.log_info(
                        f"{feature_name}对应存档价值{archive_value}TB不高于阈值"
                        f"{retain_threshold}TB，点击删除"
                    )
                task.click_box(feature)
                task.sleep(1)
                return True
        task.log_info("检测到存储数据收集完成，由通用按钮处理")
        return False
    return False


def handle_stage_end_data_details(task: TriggerTask):
    """关卡结束数据详情页面：检测到存储数据后关闭详情。"""
    page_text = _get_region_text(task, (0.111, 0.103, 0.338, 0.243))
    if _get_game_text(task, "存储数据") not in page_text:
        return False

    task.log_info("检测到关卡结束数据详情页面，点击关闭")
    _move_and_click(task, 0.883, 0.156)
    task.sleep(1)
    return True


# def handle_cares_tip(task: TriggerTask):
#     """卡厄思 TIP 提示页面: 点击关闭。"""
#     box = find_box_at_point(task, 0.502, 0.286)
#     if box and box.name == "TIP":
#         _move_and_click(task, 0.884, 0.915)
#         return True
#     return False


def handle_expedition_unlock(task: TriggerTask):
    """解锁探险记录页面: 点击确定。"""
    box = find_box_at_point(task, 0.5, 0.151)
    if box and _get_game_text(task, '解锁的探险记录') in box.name:
        task.log_info("检测到解锁探险记录页面，点击页面")
        _move_and_click(task, 0.5, 0.95)
        task.sleep(1)
        return True
    return False


# ------------------------- 精神崩溃/创伤中心（卡厄思模式特有） -------------------------

def handle_mental_breakdown(task: TriggerTask):
    """精神崩溃发生页面: 根据配置决定是否治疗崩溃。"""
    box = find_box_at_point(task, 0.496, 0.186)
    if box and _get_game_text(task, '精神崩溃发生') in box.name:
        if _get_config_value(task, '治疗崩溃', True):
            task.log_info("检测到精神崩溃发生，去创伤中心治疗")
            _move_and_click(task, 0.706, 0.915)
            task.sleep(1)
            return True
    return False


def handle_trauma_center(task: TriggerTask):
    """创伤中心: 优先使用旅行券治疗；若配置"优先使用金币治疗"为True，则始终使用金币治疗。"""
    box = find_box_at_point(task, 0.125, 0.049)
    if not (box and _get_game_text(task, '创伤中心') in box.name):
        return False
    task.log_info("检测到创伤中心，采取策略，优先使用旅行券")
    if find_text(task, _get_game_text(task, '没有恢复中的战员')):
        _move_and_click(task, 0.044, 0.046)
        return True
    _move_and_click(task, 0.420, 0.339)
    task.sleep(0.5)
    travel_ticket = task.ocr(0.933, 0.904, 0.971, 0.943)
    if travel_ticket:
        has_ticket = int(travel_ticket[0].name[0]) > 0
        prefer_gold = _get_config_value(task, '优先使用金币治疗', False)
        if prefer_gold:
            task.log_info("优先使用金币治疗配置为True，点击金币治疗")
            _move_and_click(task, 0.702, 0.924)
        else:
            _move_and_click(task, 0.798 if has_ticket else 0.702, 0.924)
        task.sleep(0.5)
    return True


def handle_treating(task: TriggerTask):
    """治疗进行中页面: 选择治疗方法。"""
    if find_text(task, _get_game_text(task, '选择哪种方法进行治疗')):
        task.log_info("检测到治疗进行中")
        _move_and_click(task, 0.765, 0.500)
        return True
    return False


def handle_treat_approve(task: TriggerTask):
    """治疗完成页面: 点击批准。"""
    if find_text(task, _get_game_text(task, '点击批准')):
        task.log_info("检测到治疗完成，点击批准")
        _move_and_click(task, 0.768, 0.810)
        return True
    return False


def handle_go_to_chaos_core(task: TriggerTask):
    """前往卡厄思核心按钮。"""
    box = find_box_at_point(task, 0.945, 0.918)
    if box and _clean_match(box.name, "前往卡厄思核心"):
        if is_button_active(task, box):
            task.log_info("检测到前往卡厄思核心按钮，点击进入")
            task.click_box(box)
            task.sleep(1)
            return True
        else:
            task.log_info("前往卡厄思核心按钮未激活（灰色），跳过点击")
            return False
    return False


def handle_chaos_reward_claim(task: TriggerTask):
    """卡厄思模式奖励领取页面: 如果0.568,0.711处文本包含"获得"，则为奖励领取页面。
    识别0.959,0.281处"\\d/\\d"作为当前/最大战利品验证卡，
    如果验证卡大于0则点击获得，否则重置"领取奖励(只使用验证卡)"为False并取消。"""
    claim_box = find_box_at_point(task, 0.568, 0.711)
    if not (claim_box and "获得" in claim_box.name):
        return False

    task.log_info("检测到卡厄思模式奖励领取页面")

    # 读取0.959,0.281处的战利品验证卡文本，格式如 "2/3"
    verify_box = find_box_at_point(task, 0.959, 0.281)
    if verify_box and re.search(r'(\d+)/(\d+)', verify_box.name):
        match = re.search(r'(\d+)/(\d+)', verify_box.name)
        current_cards = int(match.group(1))
        max_cards = int(match.group(2))
        task.log_info(f"战利品验证卡: {current_cards}/{max_cards}")

        if current_cards > 0:
            task.log_info(f"当前战利品验证卡{current_cards}>0，点击获得")
            task.click_box(claim_box)
            task.sleep(1)
            return True
        else:
            task.log_info("当前战利品验证卡为0，将领取奖励(只使用验证卡)设置为False，点击取消")
            task.config['领取奖励(只使用验证卡)'] = False
            from ok.gui.Communicate import communicate
            communicate.task_list_updated.emit()
            _move_and_click(task, 0.352, 0.708)
            task.sleep(1)
            return True
    else:
        task.log_info("未检测到战利品验证卡信息，点击取消")
        _move_and_click(task, 0.352, 0.708)
        task.sleep(1)
        return True


def handle_chaos_reward_settlement(task: TriggerTask):
    """卡厄思奖励结算页面: 如果0.552,0.067处包含"结算"，则为卡厄思奖励结算页面。
    如果0.851,0.389处包含"获得"且当前战利品验证卡>0，则点击获得按钮。
    如果领取奖励关闭且只打第一层完成，则退出结算页面。"""
    title_box = find_box_at_point(task, 0.552, 0.067)
    if not (title_box and "结算" in title_box.name):
        return False

    task.log_info("检测到卡厄思奖励结算页面")

    if not _get_config_value(task, '领取奖励(只使用验证卡)', False):
        task.log_info("领取奖励(只使用验证卡)配置为False")
        return False if task.node_status.get('is_escaped', False) else _finish_only_first_layer(task)

    # 检查0.851,0.389处是否有"获得"按钮
    reward_box = find_box_at_point(task, 0.851, 0.389)
    if not (reward_box and "获得" in reward_box.name):
        task.log_info("未检测到获得按钮")
        return False if task.node_status.get('is_escaped', False) else _finish_only_first_layer(task)

    task.log_info("检测到获得按钮，点击获得")
    task.click_box(reward_box)
    task.sleep(1)
    return True


# 卡厄思模式 PAGE_HANDLERS
PAGE_HANDLERS = [
    handle_auto_stop,
    handle_save_target_member,
    handle_route_selection,
    # handle_stage_clear,
    log_credit,
    log_node_status,
    handle_stuck_log, #画面卡住检测及兜底处理
    handle_close_page, #点击屏幕关闭页面，优先于其他普通页面处理
    handle_threat_detection, #威胁侦测过场页，按 ESC 关闭
    handle_event_dialog, #剧情对话停住时按空格翻页（实跑 10/01 11:47 停了 2 分半，ESC 没用）

    handle_refine_equipment_credit, #提炼装备信用点页面，优先于确认按钮
    handle_dont_show_again, #带「今天不再显示 / 下次登入前不再显示」的确认框：先勾上再确认
    handle_center_confirm, #页面中央确认按钮
    handle_archive_target_member, #信息统计页面，记录刷存档目标主战员
    handle_chaos_mask_engraving, #面具卡牌刻印获取页面
    handle_memory_carving, #记忆雕琢页：一直雕琢到按钮变灰再离开，优先于离开按钮
    handle_equipment, #装备选择
    handle_card_assign,
    handle_confirm, #确认按钮
    handle_mask_card, #面具卡牌获得页面
    handle_convert, #转换按钮
    handle_shop, #德朗商店
    handle_rest, #休息/商店入口
    handle_close_button, #关闭按钮
    handle_remove, #移除按钮
    handle_three_choice_card_remove, #三选一卡牌移除页面（低优先级兜底）
    handle_flash, #闪光按钮
    handle_reflash, #重新闪光按钮
    handle_grant_flash, #赋予闪光按钮
    handle_copy, #复制按钮
    handle_leave, #离开按钮
    handle_mental_breakdown, #精神崩溃，优先级高于下一步按钮
    handle_data_collected, #存储数据收集完成，优先级高于下一步按钮
    # handle_battle_failed, #战斗失败，优先级高于下一步
    handle_expedition_result, #探险结果页面，优先级高于下一步
    handle_next_step, #下一步按钮
    handle_craft, #合成按钮
    handle_select, #选择按钮
    handle_go_to_chaos_core, #前往卡厄思核心
    handle_equipment_recast, #装备重铸按钮

    handle_minimizemap,
    handle_weakness_info,
    handle_non_battle_page,
    handle_battle_crash,
    handle_battle_auto_check,
    handle_settlement,
    handle_destiny_choice,
    handle_main_member_flash,
    handle_card_reward,
    handle_select_card,
    handle_copy_member,
    handle_convert_card,
    handle_discovery_select,
    handle_negotiation,
    handle_chaos_reward_settlement, #卡厄思奖励结算页面，优先级高于继续按钮
    handle_chaos_reward_claim, #卡厄思模式奖励领取页面
    handle_continue,
    handle_season_chaos_initial_page, #赛季卡厄思初始页面，优先于进入按钮
    handle_zero_system_initial_page, #零式系统初始页面，优先于进入按钮
    handle_disable_adaptation_distortion, #刷空档时关闭适应畸变，优先于进入按钮
    handle_enter,
    handle_obtain_reward,
    handle_view_original,
    handle_trauma_center,
    handle_treating,
    handle_treat_approve,
    handle_zero_system_home,
    handle_codex_search,
    handle_expedition_unlock,
    # handle_cares_tip,
    handle_memory_elimination,
    handle_chaos_craft,
    handle_conquer_difficulty,
    handle_copy_card_choice,
    handle_skip,
    handle_event_task,
    handle_held_cards_page,
    handle_escape,
    handle_stage_end_data_details, #关卡结束数据详情页面，低优先级
    log_unhandled_page, #放最后：连续 10 秒没有处理函数认领画面时记「未识别页面」
]
