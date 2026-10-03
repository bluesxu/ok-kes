"""
卡厄思模式、出击模式的加速补丁。

本仓库里 ChaosMode、SortieMode 在 __init__ 中直接调用 install；
也可以用 speedup_patch/install_speedup.py 把本文件装进未修改的官方版。

原版每次点击后按写死的时长等待（click_box 自带 1 秒，处理函数再 sleep 0.5~2 秒），
_move_and_click 点击前还固定悬停 0.5 秒，实际界面通常不到 1 秒就切换完成。

本补丁的做法：
1. 延迟支付等待：处理函数里的 sleep 先记账不真睡，之后它若还要看画面或再操作，
   先把欠的时间补足，保持原有语义；处理函数结束时还欠着的等待交给第 2 步。
2. 文字闸门：只做了一次点击的帧，下一轮 OCR 后先判断页面是否已响应——
   被点的文字还在原处就继续等；页面文字变了，再多识别一次确认不再变化才交给处理函数。
   无论如何不会晚于原版的等待时长。游戏背景一直有动画，所以只看文字，不看像素。
3. 点击前悬停 0.5 秒缩短为可配置值；非战斗时检测间隔 1 秒降为可配置值。
4. 并行模板匹配：路线节点、小地图、牌库和选卡页的卡牌类型都是在同一帧、同一区域里逐个匹配多种图标，
   请求组内第一个图标时整组并行算完并缓存，后续直接取结果；每个图标的计算与原来完全相同。
5. 路线页识别前原本固定等 1 秒让图标入场动画放完：改为至少等 0.4 秒、且连续两次识别到的图标位置一致就继续，
   最长仍是 1 秒；没识别到任何图标（Boss 节点）时等满 1 秒。
6. 删卡/复制卡翻牌库：滚动步长仍是原来的半行；每次滚动后不再固定等 0.5 秒，卡牌区域一停止移动就识别，
   没检测到移动时仍等满 0.5 秒。最下面一行卡的描述在区域外读不到，原规则会整行丢弃（实测只保留 3%），
   删卡/复制卡只需要卡名，所以这两种流程里图标匹配度 >0.9 且读到卡名即保留（沿用原本给咒术卡的规则）。
7. 要求移除多张但目标卡不够时：原逻辑会点“跳过”，连已选中的目标卡也不删；现在只要选中了至少 1 张，
   就不点“跳过”，交给原有的“移除”按钮处理确认删除已选中的卡（不拿其他卡凑数）。一张没选中仍照原逻辑跳过；
   若保留部分选择后“移除”按钮没点成，下一次照原逻辑跳过，避免反复。
8. 出击模式战斗出牌：原逻辑按数字键选中卡牌后固定等 0.5~1 秒才回车，回车后再固定等 1~2 秒。
   实测按键后 0.01~0.04 秒卡牌就上滑到位，回车后约 0.2~0.3 秒手牌数就减少了。
   改为按数字键后等手牌区出现明显变化（卡牌上滑）就回车，回车后等手牌数减少就继续；
   两段都以原时长封顶，判断不出结果（例如这张牌没打出去）时照原逻辑等满。
   有的牌打出后会弹出选择页面（如“请选择功能”），盖住手牌数：回车后连续几次读不到手牌数就结束等待，
   这次出牌流程剩下的按键也不再发送（兜底出牌会把手牌数到 1 的按键都按一遍），由下一轮马上处理弹窗。
   为了不把牌卡在上滑状态：按数字键前先等手牌区静止（上一张牌的动画会被误判成上滑），
   按键后连续两次截图都有变化才算上滑，上滑后再等 0.15 秒才回车。
   结束回合（按 E）前再确认：出过牌 3 秒内不按（新抽的牌可能还没到手、网络卡顿），按过 E 后 5 秒内不再按
   （连按的 E 可能落到我方下一回合），并且要连续两轮都判断该结束回合才按。
   出牌逻辑确认 AP 已用完时（end_turn_sure）只要求出过牌 1 秒、不再确认第二轮：实跑中每回合最后一张牌到按 E 要等 4~5 秒。

另外修正一处与速度无关的问题：OCR 有时把一个按钮的文字切成两个框（实测国际服的「赋予灵光一闪」被切成
「赋予灵光-」和「一闪」），而处理函数是拿固定的一个点去找按钮，这个点会落进两框之间的空隙，
于是按钮找不到也没人点，选完卡后一直停在选卡页。只在原逻辑一个框都没找到时，把同一行紧挨着的文字框
合并后再判断一次。这一项关闭“加速模式”也生效。
还修正了几处国际服上的问题（BOSS 选择页标题、休息区读不到生命值/信用点、等「確認」按钮），
详见 _patch_bug_fixes，同样关闭“加速模式”也生效。
自动卡厄思模式另有两条与速度无关的规则，关闭“加速模式”也生效：
商店页信用点小于 80 就点「离开」退出，不再继续移除、购买或刷新；每次商店操作结束、回到商店页时再查一次，读不到数字不退出。
配置面板里的「刷新商店」默认关闭：关闭时不点商店里的「免费」刷新，没货就离开；打开后恢复原版刷新。
分解存档资料的确认框：没勾上「下次登入前不再显示」就先勾上，再点确认。点过已勾上的框会取消勾选，所以先看框是不是橙色。
零式系统法典卡片：游戏更新后不再显示「存储数据价值 N」，改为「存档资料储存上限 N pt」，原版读不到就一直重新合成。
改为按新增配置「存档储存上限大于等于多少pt」（0 为不限制）判断；两种都读不到时等 3 帧，仍读不到就直接进入。
按钮文字切成两个框、国际服的几处问题、分解存档确认框、零式系统法典卡片，本仓库源码里也已直接修好，保留这里是为了装进官方版时同样生效。
两边同时存在不冲突：大多只在原函数没处理时才兜底；休息区读不到数字时，这里的立即重读会先于源码的下一帧重读生效。

只作用于装了本补丁的任务（卡厄思模式、出击模式），关闭任务配置里的“加速模式”即恢复原有的时序逻辑。

注意：本文件不能定义顶层类，框架会把 ok_tasks 下含类的 .py 当作任务加载。
"""
import functools
import inspect
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

import battle_log
import config_io
import utils

ENABLE_KEY = "加速模式"
HOVER_KEY = "点击前悬停等待(秒)"
INTERVAL_KEY = "非战斗检测间隔(秒)"
SHOP_REFRESH_KEY = "刷新商店"
STORAGE_CAPACITY_KEY = "存档储存上限大于等于多少pt"
MEMORY_PRIORITY_KEY = "记忆卡牌优先级"
MEMORY_DEFAULT_KEY = "记忆卡牌默认选择"
MEMORY_PROCESS_KEY = "记忆加工优先级"

_GRID = 10              # 文字中心按 10x10 网格量化后比较页面
_SAME_PAGE = 0.6        # 与点击前文字布局相似度 >= 0.6 视为页面还没响应
_STABLE = 0.75          # 相邻两次识别相似度 >= 0.75 视为文字已稳定
_BUTTON_TOLERANCE = 0.02
_GATE_INTERVAL = 0.05   # 闸门等待期间尽快再识别
_BATTLE_INTERVAL = 0.5  # 战斗中帧间隔：原版 1 秒；0.5 让回合边界（「结束回合」按钮回来/敌人行动完）发现更快，CPU 略升
_STATS_EVERY = 30
_EXPECTED_RUN_SOURCE = (
    "self.all_texts = _simplify_texts(self.ocr())",
    "self._check_upload_if_needed()",
)
# run() 里用的页面处理函数列表（卡厄思是 utils_chaos，出击是 utils_sortie）
_HANDLERS_PATTERN = re.compile(r"for handle_page in (\w+)\.PAGE_HANDLERS:")
# 战斗中的页面处理函数：检测间隔保持原版的 1 秒，避免战斗时多占 CPU
_BATTLE_HANDLERS = frozenset((
    "handle_battle_auto_check",                                    # 卡厄思：等自动战斗
    "handle_battle_page", "handle_battle_hand_select", "handle_discard_hand_card",  # 出击：出牌
    "handle_curiosity_activate", "handle_extra_card_use", "handle_card_function_select",
    "handle_return_to_draw_pile",
))
# 处理函数会在同一帧、同一区域、同一阈值下依次匹配的图标组
_PARALLEL_GROUPS = (
    ("safezone", "enemy", "elite", "event", "settlement", "shop", "kalei", "seal", "hard"),  # 路线页节点与标记
    ("position_in_map", "settlement_in_map", "enemy_in_map", "safezoom_in_map", "elite_in_map",
     "event_in_map"),  # 小地图节点（阈值 0.85）
    ("kalei_in_map", "shop_in_map", "seal_in_map", "hard_in_map"),  # 小地图特殊标记（阈值 0.65）
    ("attack_in_deck", "skill_in_deck", "enhance_in_deck", "hex_in_deck", "hex_in_deck_tw"),  # 牌库卡牌类型
    ("attack", "skill", "enhance", "hex", "abnormal"),  # 选卡页卡牌类型
    ("event1", "event2", "event3", "event4", "event5", "event6", "event7", "event8"),  # 事件选项（阈值 0.70）
)
_GROUP_OF = {name: group for group in _PARALLEL_GROUPS for name in group}
# 单例匹配预取：这些特征分散在不同处理函数的开头、各有各的固定区域，几乎每帧都会被串行查到（合计 ~60-90ms）。
# 第一次查到其中任意一个时，把整批在同一帧上并行跑完，其余调用查预取结果；区域/阈值对不上的调用照走原逻辑。
_PREFETCH_MATCHES = (
    ("position", (0.335, 0.568, 0.453, 0.751), 0),             # handle_route_selection: 路线页的当前位置图标
    ("memberinfo", (0.005, 0.018, 0.080, 0.343), 0),           # handle_archive_target_member: 主战员头像
    ("memberinfo2", (0.005, 0.018, 0.080, 0.343), 0),
    ("flash_in_sortie_safezoom", (0.702, 0.347, 0.963, 0.713), 0),  # 休息区闪光
    ("rest", (0.157, 0.503, 0.467, 0.863), 0),                 # 休息区
    ("finishturn", (0.844, 0.782, 0.998, 0.990), 0),           # 结束回合按钮
    ("minimizemap", None, 0),                                  # 小地图（全屏）
)
_PREFETCH_OF = {name: (box, threshold) for name, box, threshold in _PREFETCH_MATCHES}
_PREFETCH_KWARGS = {"feature_name", "box", "threshold"}
_match_pool = None
_ROUTE_TOLERANCE = 3        # 路线图标两次识别位置相差不超过 3 像素视为已停止入场动画
_ROUTE_MIN_SETTLE = 0.4     # 至少等 0.4 秒，防止图标依次出现时把“已出现的一部分”当成全部
_ROUTE_POLL = 0.05
_DECK_REGION = (0.274, 0.108, 0.929, 0.874)   # 与 recognize_cards_in_deck 的识别区域一致
_DECK_SIZE = (96, 112)      # 比较卡牌区域是否在动时用的缩略图尺寸
_DECK_MOVED = 0.03          # 与滚动前相比变化像素 >3%：列表已开始滚动
_DECK_STILL = 0.01          # 相邻两次截帧变化像素 <1%：列表已停
_DECK_POLL = 0.03
_NAME_ONLY_PAGES = ("select_card-移除", "select_card-复制")  # 只需要卡名的选卡流程
_NAME_ONLY_THRESHOLD = 0.90  # 与原本咒术卡的“只凭卡名保留”阈值一致
_SKIP_WORDS = ("跳过", "取消")  # 删卡流程找不够卡时原逻辑点的按钮
_MERGE_GAP = 0.02       # 同一行相邻文字框的最大间隔（相对屏幕宽度），超过这个距离不合并
# 出击模式战斗出牌：按数字键选中卡牌（卡牌上滑），回车打出
_HAND_REGION = (0.159, 0.660, 0.836, 0.950)   # 手牌区域
_HAND_SIZE = (192, 96)
_HAND_MOVED = 0.15      # 手牌区相对按键前变化超过 15%：卡牌已上滑到位（实测 0.01~0.04 秒）
_HAND_POLL = 0.02
_RAISE_SETTLE = 0.15    # 上滑到位后再稍等一下才回车（网络卡顿时游戏处理按键会慢一点）
_HAND_STILL = 0.02      # 按键前手牌区连续两次截图变化 <2% 视为静止（没有上一张牌的动画）
_HAND_STILL_MAX = 0.5   # 手牌区一直在动时，按键前最多等这么久
# 结束回合（按 E）前的确认：出牌太快时新抽的牌可能还没到手，网络卡顿时画面也会停一下
_END_TURN_AFTER_PLAY = 3.0  # 出过牌后至少这么久才结束回合
_END_TURN_AFTER_PLAY_SURE = 1.0  # 出牌逻辑确认 AP 已用完时，出过牌后只需等这么久
_END_TURN_REPEAT = 5.0      # 按过 E 后这么久内不再按（结束回合的动画期间按钮还在）
_END_TURN_CONFIRM = 0.8     # 要连续两轮都判断该结束回合，两次至少相隔这么久
_END_TURN_STALE = 4.0       # 第一次判断距今超过这么久就作废，重新确认
_COUNT_REGION = (0.470, 0.950, 0.560, 0.995)  # 手牌数「N/10」
_COUNT_PATTERN = re.compile(r"(\d+)\s*/\s*10")
_PLAY_POLL = 0.04
_PLAY_SETTLE = 0.10     # 手牌数减少后再稍等一下，让出牌动画开始
# 国际服修正（见 _patch_bug_fixes）
_BOSS_TITLE = re.compile(r"请选择.*遭遇的\s*boss", re.IGNORECASE)   # 繁中服「請選擇在核心遭遇的BOSS。」
_BOSS_POINTS = ((0.358, 0.706), (0.641, 0.706))                    # 两个 BOSS 名字的位置（与原函数相同）
_REREAD_TRIES = 4                                                   # 休息区读不到生命值/信用点时重新识别的次数
_REREAD_INTERVAL = 0.25
_CONFIRM_TEXT = re.compile(r"确认|確認")
_CHAOS_MODE = "自动卡厄思模式"
_SHOP_EXIT_CREDIT = 80          # 商店页信用点低于这个数就离开，不再继续买
_SHOP_CREDIT_POINTS = ((0.794, 0.054), (0.734, 0.053))  # 与 utils._get_current_credit 相同
_DECOMPOSE_TITLE = ("分解存档", "分解存檔")
_DECOMPOSE_CHECK = ("不再显示", "不再顯示", "不再显", "不再顯")
_CHECKBOX_LEFT = 0.029   # 勾选框中心在「下次登入前不再显示」文字左缘的左侧
_CHECKBOX_SAMPLE = 0.012 # 在勾选框中心周围取样，避开中间的白色对勾
_LEFT_BATTLE_POLLS = 3  # 回车后连续几次读不到手牌数：多半弹出了选择页面（如“请选择功能”），交给下一轮处理
_PUNCTUATION = re.compile(r"[^一-鿿\w]")  # 与 utils._clean_match 相同的清理规则
_PARTIAL_RETRY_SECONDS = 10     # 保留部分选择后这段时间内再次进入选卡，视为“移除”没点成
_STORAGE_READ_RETRIES = 3       # 零式系统页连续几帧读不到存档价值/上限，就不再重新合成，直接进入
# 「选择刻印的记忆」页（零式系统「雪上凝结的约定」）：标题区域，三张记忆卡的区域（牌名 + 描述）
_MEMORY_TITLE = (0.35, 0.03, 0.65, 0.20)
_MEMORY_CARDS = ((0.138, 0.20, 0.360, 0.76), (0.389, 0.20, 0.611, 0.76), (0.640, 0.20, 0.862, 0.76))
_MEMORY_CARD_Y = 0.48
_MEMORY_CONFIRM = (0.80, 0.86, 0.99, 0.99)
_MEMORY_REFRESH_Y = (0.79, 0.86)   # 每张记忆卡下方的「重新搜索 3/3」，只换掉这一张
_MEMORY_DEFAULT_PRIORITY = ["触发韧性伤害"]


def install(task):
    """给卡厄思模式任务实例装上加速逻辑，重复调用无副作用。"""
    if getattr(task, "_speedup", None) is not None:
        return
    task.default_config[ENABLE_KEY] = True
    task.default_config[HOVER_KEY] = 0.1
    task.default_config[INTERVAL_KEY] = 0.3
    task.config_description[ENABLE_KEY] = "点击后页面一响应就继续，不再固定等待；关闭后完全恢复原版逻辑"
    task.config_description[HOVER_KEY] = "鼠标移到目标后等待多久再点击，原版固定 0.5 秒"
    task.config_description[INTERVAL_KEY] = "非战斗时两次识别的最短间隔，原版 1 秒；战斗中保持 1 秒"
    # 加速选项只影响本机，不写进导出的配置码和上传的统计
    config_io.UI_ONLY_CONFIG_KEYS.update({ENABLE_KEY, HOVER_KEY, INTERVAL_KEY})
    if getattr(task, "name", None) == _CHAOS_MODE:
        battle_log.install(task)  # 详细日志：出击模式在 SortieMode 里装，卡厄思模式在这里装（官方版 ChaosMode 也能用上）
        task.default_config[SHOP_REFRESH_KEY] = False
        task.config_description[SHOP_REFRESH_KEY] = "关闭后商店不再点击「免费」刷新，没货就离开；开启后恢复原版刷新"
        config_io.UI_ONLY_CONFIG_KEYS.add(SHOP_REFRESH_KEY)
        # 刷存档的门槛，跟着配置码导出
        task.default_config.setdefault(STORAGE_CAPACITY_KEY, 0)
        task.config_description[STORAGE_CAPACITY_KEY] = (
            "零式系统法典卡片、获得法典选项上「存档资料储存上限」低于这个值就重新合成；"
            "几个选项都达标时选上限最低的。0 为不限制"
        )
        config_type = getattr(task, "config_type", None)
        if isinstance(config_type, dict):
            config_type.setdefault(STORAGE_CAPACITY_KEY, {"min": 0, "max": 999})
            config_type.setdefault(MEMORY_DEFAULT_KEY, {"type": "drop_down", "options": ["1", "2", "3", "随机"]})
        task.default_config.setdefault(MEMORY_PRIORITY_KEY, list(_MEMORY_DEFAULT_PRIORITY))
        task.default_config.setdefault(MEMORY_DEFAULT_KEY, "1")
        task.config_description[MEMORY_PRIORITY_KEY] = (
            "「选择刻印的记忆」页按列表顺序选第一张包含该关键词的记忆卡（牌名或描述，如「贪婪」「全体攻击」），"
            "三张都不含时先用卡下方的「重新搜索」刷新"
        )
        task.config_description[MEMORY_DEFAULT_KEY] = "记忆卡牌优先级都没匹配上时选第几张"
        task.default_config.setdefault(MEMORY_PROCESS_KEY, [])
        task.config_description[MEMORY_PROCESS_KEY] = (
            "记忆雕琢完成后的「记忆加工」页按列表顺序选第一张描述包含该关键词的卡（如「攻击力」「伤害量」），"
            "几张都包含时选百分比大的；都不包含时选百分比最大的"
        )

    orig = {
        name: getattr(task, name)
        for name in ("sleep", "click", "click_box", "move", "scroll", "mouse_down", "mouse_up", "send_key", "run",
                     "find_feature")
    }
    handlers = _handlers_module(task)
    st = {
        "orig_sleep": orig["sleep"], "active": False, "handlers": handlers, "gate_ok": handlers is not None,
        "in_run": False, "in_action": False, "owed_until": 0.0, "actions": 0,
        "moved": False, "held": False,
        "action_sig": None, "action_box": None, "action_time": 0.0,
        "gate": None, "prev_sig": None, "hit": None, "battle": False,
        "planned": 0.0, "actual": 0.0, "count": 0, "saved_total": 0.0,
        "match_cache": None, "deck_scroll": False, "deck_before": None,
        # 删卡目标不够时保留已选中的卡：kept_pending 为拦下“跳过”时已选中的张数，partial_at 为保留的时刻
        "removal_flow": False, "kept_pending": None, "allow_skip": False, "partial_at": 0.0,
        # 出击模式出牌：pay_hook 决定下一次补等待时怎么等（等卡牌上滑 / 等手牌数减少）
        "battle_play": False, "hand_before": None, "hand_count": None, "pay_hook": None,
        "battle_left": False,  # 出牌途中已离开战斗页面（弹出了选择页面）
        # 结束回合的确认：上次出牌/按 E 的时刻、第一次判断该结束回合的时刻
        "last_play_key": 0.0, "last_end_turn": 0.0, "end_turn_seen": 0.0, "end_turn_reason": None,
        "end_turn_sure": False,  # 出牌逻辑确认 AP 已用完（utils_battle._end_turn 设置）
        "end_turn_deferred": False,  # 这次的 E 没有真正发出去（utils_battle._end_turn 据此决定记不记「结束回合」）
    }
    task._speedup = st
    if not st["gate_ok"]:
        task.log_info(f"加速补丁：{task.name} 的 run() 与预期不符（可能已更新版本），只启用悬停和检测间隔优化")

    def sleep(timeout):
        if not st["active"] or timeout is None or timeout <= 0:
            return orig["sleep"](timeout)
        if st["battle_play"] and st["battle_left"]:
            return True  # 出牌途中已弹出选择页面：这次出牌流程剩下的等待不再需要
        if st["held"] or task.executor.paused:
            _pay_owed(st)
            return orig["sleep"](timeout)
        if st["moved"]:
            st["moved"] = False
            _pay_owed(st)
            hover = _float_config(task, HOVER_KEY, 0.1, 0.0, 1.0)
            st["saved_total"] += max(0.0, timeout - hover)
            return orig["sleep"](min(timeout, hover))
        # 连续多次 sleep 依次累加，与原版串行等待一致
        st["owed_until"] = max(st["owed_until"], time.time()) + timeout
        task.executor.reset_scene(check_enabled=False)
        return True

    def action(name, point_of):
        def wrapped(*args, **kwargs):
            if not st["active"] or st["in_action"]:
                return orig[name](*args, **kwargs)
            st["in_action"] = True
            try:
                _pay_owed(st)  # 出牌时这里会判断上一张牌是否已打出、是否弹出了选择页面
                if name == "send_key" and st["battle_play"] and st["battle_left"]:
                    st["end_turn_deferred"] = _key_of(args, kwargs) == "e"
                    return True  # 出牌途中已弹出选择页面：剩下的出牌按键不再发送，交给下一轮处理弹窗
                if name == "send_key" and st["battle_play"] and _key_of(args, kwargs) == "e" \
                        and not _end_turn_allowed(task, st):
                    st["end_turn_deferred"] = True
                    return True  # 这一轮先不结束回合，下一轮再判断
                if name == "mouse_down":
                    st["held"] = True
                    st["moved"] = False
                else:
                    if name == "mouse_up":
                        st["held"] = False
                    if name == "scroll" and st["deck_scroll"]:
                        # 牌库翻页：在欠的等待补完、真正滚动前截一帧，作为判断列表是否已滚动的参照
                        st["deck_before"] = _deck_capture(task)
                    if name == "send_key" and st["battle_play"]:
                        # 出牌：记下按键前的手牌区和手牌数，决定这次按键之后怎么等
                        _before_battle_key(task, st, args[0] if args else kwargs.get("key"))
                    _note_action(task, st, point_of(task, args, kwargs))
                return orig[name](*args, **kwargs)
            finally:
                st["in_action"] = False
        return wrapped

    def click_box(*args, **kwargs):
        if st["removal_flow"] and not st["allow_skip"]:
            box = args[0] if args else kwargs.get("box")
            name = getattr(box, "name", None)
            pending = getattr(task, "_pending_removed_card_count", 0)
            if pending > 0 and isinstance(name, str) and any(word in name for word in _SKIP_WORDS):
                # 删卡目标不够：已选中的目标卡照删，不点“跳过”，下一轮由原有的“移除”按钮处理确认
                st["kept_pending"] = pending
                task.log_info(f"加速：已选中{pending}张要移除的卡牌，不点「{name.strip()}」，改为确认移除已选中的卡牌")
                return True
        return orig["click_box"](*args, **kwargs)

    def move(*args, **kwargs):
        if st["active"]:
            _pay_owed(st)
            st["moved"] = True
        return orig["move"](*args, **kwargs)

    def find_feature(*args, **kwargs):
        name = kwargs.get("feature_name")
        if not args and st["active"] and set(kwargs) <= _PREFETCH_KWARGS:
            hit = _prefetch_hit(task, st, orig["find_feature"], name, kwargs)
            if hit is not None:
                return hit
        group = _GROUP_OF.get(name) if isinstance(name, str) else None
        if group is None or args or not st["active"] or not set(kwargs) <= _PREFETCH_KWARGS:
            return orig["find_feature"](*args, **kwargs)
        key = (group, _box_key(kwargs.get("box")), kwargs.get("threshold", 0))
        common = {k: v for k, v in kwargs.items() if k != "feature_name"}
        if group is _PARALLEL_GROUPS[0] and st["owed_until"] > time.time():
            # 路线页：欠着的是识别前那 1 秒，用来等图标停稳，停稳即继续
            settled = _settle_route_icons(task, st, orig["find_feature"], group, common)
            if settled is not None:
                frame, results = settled
                st["match_cache"] = {"frame": frame, "key": key, "results": results}
                return list(results[name])
        # 与原逻辑取同一帧（取帧前会先补足欠的等待），整组在这一帧上并行匹配
        frame = task.executor.frame
        if frame is None:
            return orig["find_feature"](*args, **kwargs)
        cache = st["match_cache"]
        if cache is None or cache["frame"] is not frame or cache["key"] != key:
            cache = {"frame": frame, "key": key, "results": _match_group(orig["find_feature"], group, frame, common)}
            st["match_cache"] = cache
        return list(cache["results"][name])

    def run():
        _wrap_executor_next_frame(task.executor)
        enabled = _enabled(task)
        st.update(active=enabled, in_run=enabled, in_action=False, owed_until=0.0, actions=0,
                  moved=False, held=False, hit=None, action_sig=None, action_box=None, match_cache=None,
                  prefetch_cache=None, pay_hook=None, battle_play=False, battle_left=False, gated=False)
        if not enabled:
            st.update(gate=None, prev_sig=None, battle=False)
            task.trigger_interval = 1
            return orig["run"]()
        failed = True
        try:
            if st["gate_ok"]:
                _gated_run(task, st)
            else:
                orig["run"]()
            failed = False
        finally:
            st["in_run"] = False
            if failed:
                st.update(owed_until=0.0, gate=None, active=False)
            else:
                _finish_run(task, st)
                st["active"] = False

    task.sleep = sleep
    task.click = action("click", _click_point)
    task.click_box = click_box
    task.scroll = action("scroll", _no_point)
    task.mouse_down = action("mouse_down", _no_point)
    task.mouse_up = action("mouse_up", _no_point)
    task.send_key = action("send_key", _no_point)
    task.move = move
    task.find_feature = find_feature
    task.run = run
    _keep_stuck_detection_at_one_second()
    _patch_deck_scan()
    _patch_partial_removal()
    _patch_split_button_text()
    _patch_battle_play()
    _patch_bug_fixes()
    _patch_shop_exit()
    _patch_decompose_checkbox()
    _patch_storage_capacity()
    _patch_memory_imprint()
    recorder = _import("recorder")  # 官方版补丁不带现场记录
    if recorder is not None:
        recorder.install(task)


def _handlers_module(task):
    """本补丁会接管 run()，只在其源码仍是“OCR -> 依次尝试页面处理函数 -> 上传检查”时启用闸门。
    页面处理函数列表取自 run() 里用的那个模块（卡厄思是 utils_chaos，出击是 utils_sortie）；对不上就返回 None。"""
    try:
        source = inspect.getsource(type(task).run)
    except (OSError, TypeError):
        return None
    match = _HANDLERS_PATTERN.search(source)
    if match is None or not all(snippet in source for snippet in _EXPECTED_RUN_SOURCE):
        return None
    module = sys.modules.get(type(task).__module__)
    handlers = getattr(module, match.group(1), None)
    return handlers if hasattr(handlers, "PAGE_HANDLERS") else None


def _gated_run(task, st):
    """与原版 run() 相同，只在交给页面处理函数前多一道文字闸门。"""
    texts = utils._simplify_texts(task.ocr())
    if _gate_blocks(task, st, texts):
        st["gated"] = True  # 现场记录据此标出这一帧没交给页面处理函数
        return
    task.all_texts = texts
    for handle_page in st["handlers"].PAGE_HANDLERS:
        if handle_page(task):
            st["hit"] = handle_page.__name__
            if st["hit"] != "log_unhandled_page" and hasattr(battle_log, "handled_frame"):  # ESC 兜底不算接手
                battle_log.handled_frame(task)
            check_loop = getattr(utils, "check_loop", None)  # 官方原版 utils 没有通用循环检测
            if check_loop is not None:
                check_loop(task, st["hit"])
            return
    task._check_upload_if_needed()


def _finish_run(task, st):
    now = time.time()
    if st["owed_until"] > now:
        if st["gate_ok"] and st["actions"] == 1 and st["action_sig"] is not None:
            st["pay_hook"] = None  # 交给文字闸门，不再用出牌那套判断
            st["gate"] = {
                "sig": st["action_sig"], "box": st["action_box"],
                "start": st["action_time"], "until": st["owed_until"], "changed": False,
            }
            st["owed_until"] = 0.0
        else:
            _pay_owed(st)  # 多步操作或没有点击：保持原版等待时长
    if st["hit"] in _BATTLE_HANDLERS and not st["battle_left"]:
        st["battle"] = True  # 出牌后弹出了选择页面时不算战斗，下一轮按非战斗间隔尽快来处理
    elif st["hit"] is not None:
        st["battle"] = False
    if st["gate"] is not None:
        task.trigger_interval = _GATE_INTERVAL
    elif st["battle"]:
        task.trigger_interval = _BATTLE_INTERVAL
    else:
        task.trigger_interval = _float_config(task, INTERVAL_KEY, 0.3, 0.1, 1.0)


def _gate_blocks(task, st, texts):
    """返回 True 表示本轮先不交给页面处理函数。"""
    sig = _signature(task, texts)
    prev_sig, st["prev_sig"] = st["prev_sig"], sig
    gate = st["gate"]
    if gate is None:
        return False
    now = time.time()
    if now >= gate["until"]:
        _close_gate(task, st, now)
        return False
    if not gate["changed"]:
        if not _still_same_page(task, gate, texts, sig):
            gate["changed"] = True  # 页面刚变化，再识别一次确认已稳定
        return True
    if _still_same_page(task, gate, texts, sig):
        # 被点的文字又出现在原处：上一帧只是 OCR 闪了一下（按下动画等），页面其实没响应，继续等
        gate["changed"] = False
        return True
    if prev_sig is not None and _similarity(sig, prev_sig) >= _STABLE:
        _close_gate(task, st, now)
        return False
    return True


def _close_gate(task, st, now):
    gate, st["gate"] = st["gate"], None
    planned = gate["until"] - gate["start"]
    actual = now - gate["start"]
    st["planned"] += planned
    st["actual"] += actual
    st["count"] += 1
    st["saved_total"] += max(0.0, planned - actual)
    if st["count"] >= _STATS_EVERY:
        task.log_info(
            f"加速统计：最近{st['count']}次点击后的等待，原版{st['planned']:.1f}秒，实际{st['actual']:.1f}秒"
        )
        task.info_set("加速累计节省", f"{st['saved_total'] / 60:.1f} 分钟")
        st.update(planned=0.0, actual=0.0, count=0)


def _still_same_page(task, gate, texts, sig):
    box = gate["box"]
    if box is not None:
        # 点的是文字按钮：按钮文字还在原处，说明游戏还没响应这次点击
        name, cx, cy = box
        dx, dy = _BUTTON_TOLERANCE * task.width, _BUTTON_TOLERANCE * task.height
        return any(
            b.name.strip() == name
            and abs(b.x + b.width / 2 - cx) <= dx
            and abs(b.y + b.height / 2 - cy) <= dy
            for b in texts
        )
    return _similarity(sig, gate["sig"]) >= _SAME_PAGE


def _note_action(task, st, point):
    st["actions"] += 1
    st["moved"] = False
    st["action_time"] = time.time()
    texts = getattr(task, "all_texts", None) or []
    st["action_sig"] = _signature(task, texts)
    st["action_box"] = None
    if point is not None:
        px, py = point
        hits = [b for b in texts if b.x <= px <= b.x + b.width and b.y <= py <= b.y + b.height and b.name.strip()]
        if hits:
            b = min(hits, key=lambda b: b.width * b.height)
            st["action_box"] = (b.name.strip(), b.x + b.width / 2, b.y + b.height / 2)


def _pay_owed(st):
    remaining = st["owed_until"] - time.time()
    hook, st["pay_hook"] = st["pay_hook"], None
    st["owed_until"] = 0.0
    if remaining <= 0:
        return
    # 出牌时按“游戏是否已响应”来等，最长仍是原来的时长；等不出结果就照原逻辑等满
    if hook is not None and hook(time.time() + remaining):
        return
    st["orig_sleep"](remaining)


def _wrap_executor_next_frame(executor):
    """处理函数取新画面前，先补足之前记账的等待。"""
    if getattr(executor, "_speedup_wrapped", False):
        return
    original = executor.next_frame

    def next_frame(*args, **kwargs):
        st = getattr(executor.current_task, "_speedup", None)
        if st is not None and st["in_run"] and not st["in_action"]:
            _pay_owed(st)
        return original(*args, **kwargs)

    executor.next_frame = next_frame
    executor._speedup_original_next_frame = original
    executor._speedup_wrapped = True


def _match_group(find_feature, group, frame, common):
    futures = {n: _pool().submit(find_feature, feature_name=n, frame=frame, **common) for n in group}
    return {n: f.result() for n, f in futures.items()}


def _box_matches(task, box, rel):
    """调用点的 box 与预取清单里的相对区域是否一致（±1px）；rel 为 None 表示全屏（box 也要为 None）。"""
    if rel is None:
        return box is None
    if box is None or isinstance(box, str):
        return False
    expected = (rel[0] * task.width, rel[1] * task.height,
                (rel[2] - rel[0]) * task.width, (rel[3] - rel[1]) * task.height)
    return all(abs(getattr(box, attr, -1e9) - value) <= 1
               for attr, value in zip(("x", "y", "width", "height"), expected))


def _prefetch_matches(task, find_feature, frame):
    """把单例清单（各自固定的区域/阈值）在同一帧上并行跑一遍。"""
    futures = {}
    for name, (rel, threshold) in _PREFETCH_OF.items():
        kwargs = {"feature_name": name, "frame": frame, "threshold": threshold}
        if rel is not None:
            kwargs["box"] = task.box_of_screen(*rel)
        futures[name] = _pool().submit(find_feature, **kwargs)
    return {name: f.result() for name, f in futures.items()}


def _prefetch_hit(task, st, find_feature, name, kwargs):
    """name 命中单例预取清单且区域/阈值一致时，用本帧的批量预取结果回答；否则返回 None 走原逻辑。
    列表名（如 ["memberinfo", "memberinfo2"]）全部命中时合并返回，调用方 find_one 自己取最优。"""
    names = [name] if isinstance(name, str) else list(name) if isinstance(name, list) else None
    if not names or any(n not in _PREFETCH_OF for n in names):
        return None
    threshold = kwargs.get("threshold", 0)
    box = kwargs.get("box")
    for n in names:
        rel, spec_threshold = _PREFETCH_OF[n]
        if threshold != spec_threshold or not _box_matches(task, box, rel):
            return None
    frame = task.executor.frame
    if frame is None:
        return None
    cache = st.get("prefetch_cache")
    if cache is None or cache["frame"] is not frame:
        cache = {"frame": frame, "results": _prefetch_matches(task, find_feature, frame)}
        st["prefetch_cache"] = cache
    if isinstance(name, str):
        return list(cache["results"].get(name) or [])
    merged = []
    for n in names:
        merged += list(cache["results"].get(n) or [])
    return merged


def _icon_positions(results):
    return {n: sorted((b.x, b.y) for b in boxes) for n, boxes in results.items()}


def _same_positions(a, b):
    for name, points in a.items():
        other = b.get(name, [])
        if len(points) != len(other):
            return False
        if any(abs(x1 - x2) > _ROUTE_TOLERANCE or abs(y1 - y2) > _ROUTE_TOLERANCE
               for (x1, y1), (x2, y2) in zip(points, other)):
            return False
    return True


def _settle_route_icons(task, st, find_feature, group, common):
    """路线页识别前原本固定等 1 秒：改为反复截帧识别，至少 0.4 秒且连续两次图标位置一致就继续，最长仍是这 1 秒。
    没识别到任何图标（如 Boss 节点）时等满原时长。返回 (帧, 识别结果)；取不到帧时返回 None 交回原逻辑。"""
    capture = getattr(task.executor, "_speedup_original_next_frame", None)
    if capture is None:
        return None
    start = time.time()
    deadline, st["owed_until"] = st["owed_until"], 0.0
    previous = None
    while True:
        frame = capture()
        if frame is None:
            st["owed_until"] = deadline
            return None
        results = _match_group(find_feature, group, frame, common)
        positions = _icon_positions(results)
        if (previous is not None and any(positions.values()) and _same_positions(positions, previous)
                and time.time() - start >= _ROUTE_MIN_SETTLE):
            break
        previous = positions
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        st["orig_sleep"](min(_ROUTE_POLL, remaining))
    saved = max(0.0, deadline - time.time())
    st["saved_total"] += saved
    if saved > 0:
        task.log_info(f"加速：路线图标已停稳，识别前等待 {time.time() - start:.2f}s（原固定 {deadline - start:.2f}s）")
    return frame, results


def _active_state(task):
    st = getattr(task, "_speedup", None)
    return st if st is not None and st["active"] else None


def _capture(task):
    """截一帧；截不到时返回 None。"""
    capture = getattr(task.executor, "_speedup_original_next_frame", None)
    return capture() if capture is not None else None


def _thumbnail(frame, region, size):
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = region
    area = frame[int(y1 * height):int(y2 * height), int(x1 * width):int(x2 * width)]
    return cv2.cvtColor(cv2.resize(area, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)


def _deck_capture(task):
    """截一帧并取卡牌区域的灰度缩略图；截不到帧时返回 None。"""
    frame = _capture(task)
    return None if frame is None else _thumbnail(frame, _DECK_REGION, _DECK_SIZE)


def _deck_changed(a, b):
    return np.count_nonzero(cv2.absdiff(a, b) > 12) / a.size


def _settle_deck(task, st, before):
    """滚动后原本固定等 0.5 秒：卡牌区域开始移动后，连续两次截帧几乎不变即视为停稳，提前结束等待；
    一直没检测到移动（例如已经到底）或截不到帧时，照原逻辑等满。"""
    deadline = st["owed_until"]
    if before is None or deadline <= time.time():
        return
    moved, previous, still = False, None, 0
    while time.time() < deadline:
        current = _deck_capture(task)
        if current is None:
            return
        if not moved:
            moved = _deck_changed(current, before) > _DECK_MOVED
        elif _deck_changed(current, previous) < _DECK_STILL:
            still += 1
            if still >= 2:
                st["saved_total"] += max(0.0, deadline - time.time())
                st["owed_until"] = 0.0
                return
        else:
            still = 0
        previous = current
        st["orig_sleep"](min(_DECK_POLL, max(0.0, deadline - time.time())))


def _patch_deck_scan():
    """包装 utils 里的牌库翻页滚动和卡牌识别；只在加速模式运行中、选卡流程里生效。"""
    if getattr(utils._scroll_card_page, "_speedup_wrapped", False):
        return
    orig_scroll = utils._scroll_card_page
    orig_recognize = utils._recognize_cards_by_features

    def _scroll_card_page(task, x, y, amount, page, *args, **kwargs):
        st = _active_state(task)
        if st is None or not str(page).startswith("select_card") or task.is_adb():
            return orig_scroll(task, x, y, amount, page, *args, **kwargs)
        st.update(deck_scroll=True, deck_before=None)
        try:
            result = orig_scroll(task, x, y, amount, page, *args, **kwargs)
        finally:
            st["deck_scroll"] = False
        before, st["deck_before"] = st["deck_before"], None
        _settle_deck(task, st, before)
        return result

    def _recognize_cards_by_features(*args, **kwargs):
        task = kwargs["task"] if "task" in kwargs else args[0]
        page = str(kwargs.get("page", ""))
        if _active_state(task) is not None and page.startswith(_NAME_ONLY_PAGES):
            # 删卡/复制卡只需要卡名：图标匹配度够高就不再要求类型和描述（最下面一行的描述在区域外）
            thresholds = dict(kwargs.get("name_only_feature_thresholds") or {})
            for feature_name in kwargs.get("feature_types") or {}:
                thresholds.setdefault(feature_name, _NAME_ONLY_THRESHOLD)
            kwargs["name_only_feature_thresholds"] = thresholds
        return orig_recognize(*args, **kwargs)

    for wrapper in (_scroll_card_page, _recognize_cards_by_features):
        wrapper._speedup_wrapped = True
    utils._scroll_card_page = _scroll_card_page
    utils._recognize_cards_by_features = _recognize_cards_by_features


def _patch_partial_removal():
    """包装 utils.select_card：要求移除多张但目标卡不够时，保留已选中的卡，交给原有的“移除”按钮处理确认删除。
    原逻辑翻到底仍不够就点“跳过”并把待移除计数清零；这里在删卡流程中拦下这一下点击（见 install 里的 click_box），
    结束后把计数恢复成已选中的张数，“移除”按钮处理据此记录删了几张。"""
    if getattr(utils.select_card, "_speedup_wrapped", False):
        return
    orig_select = utils.select_card

    def select_card(task, *args, **kwargs):
        st = _active_state(task)
        action = kwargs.get("action", args[2] if len(args) > 2 else "")
        if st is None or action != "移除":
            return orig_select(task, *args, **kwargs)
        # 上次保留了部分选择，计数却没被“移除”按钮处理清零：说明没能确认，这次照原逻辑跳过，避免反复选卡
        retry = (getattr(task, "_pending_removed_card_count", 0) > 0
                 and time.time() - st["partial_at"] < _PARTIAL_RETRY_SECONDS)
        if retry:
            task.log_info("加速：上次保留的已选卡牌没有移除成功，本次照原逻辑处理")
        st.update(removal_flow=True, allow_skip=retry, kept_pending=None, partial_at=0.0)
        try:
            result = orig_select(task, *args, **kwargs)
        finally:
            st["removal_flow"] = False
        kept, st["kept_pending"] = st["kept_pending"], None
        if kept is not None:
            task._pending_removed_card_count = kept
            st["partial_at"] = time.time()
        return result

    select_card._speedup_wrapped = True
    utils.select_card = select_card



def _patch_battle_play():
    """出击模式战斗出牌：原逻辑按数字键后固定等 0.5~1 秒才回车，回车后再固定等 1~2 秒。
    实测卡牌按下后 0.01~0.04 秒就上滑到位，回车后约 0.2~0.3 秒手牌数就减少了。
    这里把两段等待都改成“游戏一响应就继续”，最长仍是原来的时长；判断不出结果时照原逻辑等满。"""
    utils_sortie = _import("utils_sortie")
    if utils_sortie is None:
        return
    for name in ("handle_battle_page", "_try_all_card_keys"):
        _replace_function(utils_sortie, name, _battle_play_wrapper)


def _import(module_name):
    try:
        return __import__(module_name)
    except ImportError:
        return None


def _replace_function(module, name, make_wrapper):
    """把 module.name 换成 make_wrapper(原函数)，重复调用不会重复包装。
    页面处理函数列表里存的是函数对象本身，别的模块用 from utils import xxx 导入的也是函数对象，
    只换模块属性不会生效，所以两个模式的 PAGE_HANDLERS 和模块命名空间里的原函数也一并换掉。"""
    current = getattr(module, name, None)
    if current is None:
        return None
    if not getattr(current, "_speedup_wrapped", False):
        wrapped = make_wrapper(current)
        wrapped._speedup_wrapped = True
        wrapped.__wrapped__ = current
        setattr(module, name, wrapped)
        current = wrapped
    original = current.__wrapped__
    for module_name in ("utils", "utils_chaos", "utils_sortie"):
        other = sys.modules.get(module_name)
        if other is None:
            continue
        if getattr(other, name, None) is original:
            setattr(other, name, current)
        handlers = getattr(other, "PAGE_HANDLERS", None)
        if isinstance(handlers, list):
            handlers[:] = [current if h is original else h for h in handlers]
    return current


def _battle_play_wrapper(original):
    @functools.wraps(original)  # 保留原函数名：检测间隔按处理函数名判断是否在战斗中
    def wrapped(task, *args, **kwargs):
        st = _active_state(task)
        if st is None:
            return original(task, *args, **kwargs)
        previous = st["battle_play"]
        st.update(battle_play=True, battle_left=False)
        try:
            return original(task, *args, **kwargs)
        finally:
            # battle_left 保留到本轮结束：_finish_run 据此让下一轮尽快来处理弹窗；下一轮开始时复位
            st.update(battle_play=previous, hand_before=None, hand_count=None)

    return wrapped


def _patch_bug_fixes():
    """修正作者代码在国际服（繁中）上的几个问题，与“加速模式”开关无关，一直生效：
    1. 出击模式 BOSS 选择页：标题是「请选择在核心遭遇的BOSS」，原逻辑只认国服的「遇见的首领」，这页没人处理，
       只能等画面静止 10 秒后由卡住兜底随机点屏幕，碰巧点中 BOSS 才能继续（实测每次 11~23 秒）。
    2. 休息区读不到生命值：出击模式当成 100% 去闪光（实测生命值 11% 时也闪光了），卡厄思模式选冥想不选休息。
       读不到信用点当成 0：满血也不闪光。页面刚切过来时这两个数字常常还没显示，两三秒后就能读到。
       改为只在休息区判断时，读不到就重新截图识别几次；仍读不到时生命值按 0% 处理（选休息，不冒险）。
    3. 休息/闪光后等「确认」按钮：框架只在软件界面是繁体时才把 OCR 结果转简体，国际服游戏 + 简体界面时
       拿「确认」去匹配「確認」永远匹配不上，每次白等 2 秒，而且没把“本节点已休息”的状态复位。"""
    utils_sortie = _import("utils_sortie")
    if utils_sortie is not None:
        _replace_function(utils_sortie, "handle_boss_selection", _boss_selection_wrapper)
        _replace_function(utils_sortie, "handle_rest_sortie", _rest_decision_wrapper)
    _replace_function(utils, "handle_rest", _rest_decision_wrapper)
    for module in (utils, utils_sortie):
        if module is None:
            continue
        _replace_function(module, "_get_current_hp_percent", _hp_reader_wrapper)
        _replace_function(module, "_get_current_credit", _credit_reader_wrapper)
        _replace_function(module, "_wait_for_rest_confirm", _rest_confirm_wrapper)


def _boss_selection_wrapper(original):
    @functools.wraps(original)
    def handle_boss_selection(task):
        if original(task):
            return True
        box = utils.find_box_at_point(task, 0.484, 0.928)
        if not (box and _BOSS_TITLE.search(box.name)):
            return False
        # 与原函数相同：随机选一个 BOSS，「确认」由下一轮的 handle_confirm 点击
        bosses = [(b.name, x, y) for x, y in _BOSS_POINTS if (b := utils.find_box_at_point(task, x, y))]
        if not bosses:
            return False
        name, x, y = random.choice(bosses)
        task.log_info(f"首领选择: 随机选择「{name}」")
        battle_log.record(task, "BOSS选择", bosses=[b[0] for b in bosses], chosen=name, reason="随机")
        utils._move_and_click(task, x, y)
        task.sleep(1)
        return True

    return handle_boss_selection


def _rest_decision_wrapper(original):
    @functools.wraps(original)
    def wrapped(task, *args, **kwargs):
        task._speedup_rest_decision = True  # 只在休息区判断时重读生命值/信用点，别处读不到照原逻辑
        try:
            return original(task, *args, **kwargs)
        finally:
            task._speedup_rest_decision = False

    return wrapped


def _reread(task, original):
    """重新截图识别若干次再调用原来的读数函数；读到有效值就返回，否则返回 None。"""
    saved = task.all_texts
    try:
        for _ in range(_REREAD_TRIES):
            time.sleep(_REREAD_INTERVAL)
            frame = _capture(task)
            if frame is None:
                return None
            task.all_texts = utils._simplify_texts(task.ocr(frame=frame))
            value = original(task)
            if value is not False and value != 0:
                return value
    finally:
        task.all_texts = saved
    return None


def _hp_reader_wrapper(original):
    @functools.wraps(original)
    def wrapped(task, *args, **kwargs):
        value = original(task, *args, **kwargs)
        if value is not False or not getattr(task, "_speedup_rest_decision", False):
            return value
        value = _reread(task, original)
        if value is not None:
            return value
        task.log_info("修正：休息区读不到生命值，按 0% 处理（选择休息，不闪光/不冥想）")
        return 0

    return wrapped


def _credit_reader_wrapper(original):
    @functools.wraps(original)
    def wrapped(task, *args, **kwargs):
        value = original(task, *args, **kwargs)
        if value != 0 or not getattr(task, "_speedup_rest_decision", False):
            return value
        value = _reread(task, original)
        return value if value is not None else 0

    return wrapped


def _rest_confirm_wrapper(original):
    @functools.wraps(original)
    def _wait_for_rest_confirm(task, *args, **kwargs):
        # 与原函数相同，只是同时认繁体「確認」
        confirm_boxes = task.wait_ocr(0.170, 0.554, to_x=0.855, to_y=0.769, match=_CONFIRM_TEXT, time_out=2)
        if not confirm_boxes:
            task.log_info("等待休息确认按钮超时")
            return False
        return True

    return _wait_for_rest_confirm


def _before_battle_key(task, st, key):
    """按键前记下参照，并决定这次按键之后怎么补等待。"""
    key = str(key or "").lower()
    st["pay_hook"] = None
    if key.isdigit() or key == "enter":
        st["last_play_key"], st["end_turn_seen"] = time.time(), 0.0  # 出过牌：结束回合要重新确认
    if key.isdigit():
        hand, frame = _still_hand(task)
        if frame is None:
            return
        st["hand_count"] = _hand_count(task, frame)
        st["pay_hook"] = lambda deadline: _wait_card_raised(task, st, hand, deadline)
    elif key == "enter" and st["hand_count"]:
        before = st["hand_count"]
        st["pay_hook"] = lambda deadline: _wait_card_played(task, st, before, deadline)


def _key_of(args, kwargs):
    return str(args[0] if args else kwargs.get("key", "")).lower()


def _end_turn_allowed(task, st):
    """按 E 结束回合前再确认一次：
    - 出过牌后 _END_TURN_AFTER_PLAY 秒内不结束：新抽的牌可能还没到手，网络卡顿时手牌也会晚一点刷新；
    - 按过 E 后 _END_TURN_REPEAT 秒内不再按：结束回合的动画期间按钮还在，连按的 E 可能落到我方下一回合；
    - 要连续两轮（至少相隔 _END_TURN_CONFIRM 秒）都判断该结束回合，才真正按 E；
    - 出牌逻辑确认 AP 已用完时（end_turn_sure），出过牌 _END_TURN_AFTER_PLAY_SURE 秒后直接按，不再确认第二轮。"""
    now = time.time()
    sure, st["end_turn_sure"] = st.get("end_turn_sure", False), False
    if now - st["last_end_turn"] < _END_TURN_REPEAT:
        reason = f"{_END_TURN_REPEAT:g} 秒内刚按过 E"
    elif now - st["last_play_key"] < (_END_TURN_AFTER_PLAY_SURE if sure else _END_TURN_AFTER_PLAY):
        reason = "刚出过牌，等手牌刷新"
    elif sure:
        st.update(end_turn_seen=0.0, last_end_turn=now, end_turn_reason=None)
        return True
    elif not st["end_turn_seen"] or now - st["end_turn_seen"] > _END_TURN_STALE:
        st["end_turn_seen"] = now
        reason = "下一轮再确认一次手牌确实打完了"
    elif now - st["end_turn_seen"] < _END_TURN_CONFIRM:
        reason = "下一轮再确认一次手牌确实打完了"
    else:
        st.update(end_turn_seen=0.0, last_end_turn=now, end_turn_reason=None)
        return True
    if st["end_turn_reason"] != reason:
        st["end_turn_reason"] = reason
        task.log_info(f"加速：先不结束回合（{reason}）")
    return False


def _still_hand(task):
    """按数字键前截一张手牌区“静止”时的参照图，返回 (缩略图, 帧)。
    上一张牌的动画还在手牌区播放时，按键后的变化会被误判成卡牌已上滑而过早回车，结果牌停在上滑状态。
    所以连续两次截图几乎不变才返回；一直在动时最多等 _HAND_STILL_MAX 秒。"""
    previous, deadline = None, time.time() + _HAND_STILL_MAX
    while True:
        frame = _capture(task)
        if frame is None:
            return None, None
        current = _thumbnail(frame, _HAND_REGION, _HAND_SIZE)
        if previous is not None and (_deck_changed(current, previous) < _HAND_STILL or time.time() >= deadline):
            return current, frame
        previous = current
        time.sleep(_HAND_POLL)


def _hand_count(task, frame):
    """读手牌数「N/10」；读不到返回 None。"""
    try:
        for box in task.ocr(*_COUNT_REGION, frame=frame):
            match = _COUNT_PATTERN.search(box.name)
            if match:
                return int(match.group(1))
    except Exception:
        return None
    return None


def _wait_card_raised(task, st, reference, deadline):
    """按数字键后等卡牌上滑到位，到位就可以回车。连续两次截图都比按键前明显变化才算上滑，避免一帧误判。"""
    hits = 0
    while time.time() < deadline:
        frame = _capture(task)
        if frame is None:
            return False
        if _deck_changed(_thumbnail(frame, _HAND_REGION, _HAND_SIZE), reference) > _HAND_MOVED:
            hits += 1
            if hits >= 2:
                st["orig_sleep"](min(_RAISE_SETTLE, max(0.0, deadline - time.time())))
                st["saved_total"] += max(0.0, deadline - time.time())
                return True
        else:
            hits = 0
        st["orig_sleep"](min(_HAND_POLL, max(0.0, deadline - time.time())))
    return True  # 没检测到上滑：已经等满原时长


def _wait_card_played(task, st, before, deadline):
    """回车后等手牌数减少，说明卡牌已经打出去了。
    手牌数连续几次都读不到时，多半是这张牌弹出了选择页面（如“请选择功能”）盖住了手牌区：
    结束等待并标记，这次出牌流程剩下的按键不再发送，由下一轮去处理弹窗。"""
    missing = 0
    while time.time() < deadline:
        frame = _capture(task)
        if frame is None:
            return False
        count = _hand_count(task, frame)
        if count is not None and count < before:
            st["hand_count"] = count
            st["orig_sleep"](min(_PLAY_SETTLE, max(0.0, deadline - time.time())))
            st["saved_total"] += max(0.0, deadline - time.time())
            return True
        missing = missing + 1 if count is None else 0
        if missing >= _LEFT_BATTLE_POLLS:
            st["battle_left"] = True
            st["saved_total"] += max(0.0, deadline - time.time())
            task.log_info("加速：出牌后读不到手牌数，多半弹出了选择页面，停止这次出牌流程，先处理页面")
            return True
        st["orig_sleep"](min(_PLAY_POLL, max(0.0, deadline - time.time())))
    return True  # 手牌数没减少（例如这张牌没打出去）：已经等满原时长


def _patch_split_button_text():
    """修正：OCR 有时把一个按钮的文字切成两个框（实测国际服的「赋予灵光一闪」被切成「赋予灵光-」和「一闪」），
    而各处理函数是拿固定的一个点去找按钮，这个点会落进两框之间的空隙，于是按钮找不到、也没人点，
    选完卡后一直停在选卡页。这里只在原逻辑一个框都没找到时兜底：把该点所在行里紧挨着的文字框合并后再判断一次。
    与“加速模式”开关无关：关掉加速模式也保留这个修正。"""
    original = utils.find_box_at_point
    if not getattr(original, "_speedup_wrapped", False):
        def find_box_at_point(task, rel_x, rel_y):
            return original(task, rel_x, rel_y) or _merged_box_at_point(task, rel_x, rel_y)

        find_box_at_point._speedup_wrapped = True
        find_box_at_point.__wrapped__ = original
        utils.find_box_at_point = find_box_at_point
    # 卡厄思、出击的处理函数是按名字导入 find_box_at_point 的，它们模块里的名字也要换掉
    for module_name in ("utils_chaos", "utils_sortie"):
        module = sys.modules.get(module_name)
        if module is not None and getattr(module, "find_box_at_point", None) is utils.find_box_at_point.__wrapped__:
            module.find_box_at_point = utils.find_box_at_point


def _merged_box_at_point(task, rel_x, rel_y):
    """把 (rel_x, rel_y) 所在行里间隔小于 _MERGE_GAP 的相邻文字框合并成一个框；合不出覆盖该点的框时返回 None。"""
    from ok import Box  # 延迟导入：合并出的框要能被 click_box 直接使用

    x, y = rel_x * (task.width or 1), rel_y * (task.height or 1)
    gap = _MERGE_GAP * (task.width or 1)
    line = sorted((b for b in getattr(task, "all_texts", None) or []
                   if b.name.strip() and b.y <= y <= b.y + b.height), key=lambda b: b.x)
    run = []
    for box in line:
        if run and box.x - max(b.x + b.width for b in run) > gap:
            if _run_covers(run, x):
                break
            run = []
        run.append(box)
    if len(run) < 2 or not _run_covers(run, x):
        return None
    name = _PUNCTUATION.sub("", "".join(b.name.strip() for b in run))  # 去掉切开处多出来的符号
    merged = Box(min(b.x for b in run), min(b.y for b in run),
                 to_x=max(b.x + b.width for b in run), to_y=max(b.y + b.height for b in run),
                 confidence=min(getattr(b, "confidence", 1.0) for b in run), name=name)
    if getattr(task, "_speedup_merged_name", None) != name:
        task._speedup_merged_name = name
        task.log_info(f"加速：OCR 把按钮文字切成了{len(run)}个框，合并为「{name}」后再判断")
    return merged


def _run_covers(run, x):
    return run[0].x <= x <= max(b.x + b.width for b in run)


def _signature(task, texts):
    width, height = task.width or 1, task.height or 1
    return {
        (b.name.strip(), int((b.x + b.width / 2) / width * _GRID), int((b.y + b.height / 2) / height * _GRID))
        for b in texts
        if b.name.strip()
    }


def _similarity(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _click_point(task, args, kwargs):
    """把 click() 的参数换算成像素坐标；Box、相对坐标、像素坐标三种写法都支持。"""
    x = args[0] if args else kwargs.get("x", -1)
    y = args[1] if len(args) > 1 else kwargs.get("y", -1)
    if isinstance(x, list):
        x = x[0] if x else None
    if hasattr(x, "width") and hasattr(x, "x"):
        return x.x + x.width / 2, x.y + x.height / 2
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if 0 < x < 1 or 0 < y < 1:
        return x * task.width, y * task.height
    return (x, y) if x >= 0 and y >= 0 else None


def _no_point(task, args, kwargs):
    return None


def _pool():
    global _match_pool
    if _match_pool is None:
        _match_pool = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4), thread_name_prefix="speedup-match")
    return _match_pool


def _box_key(box):
    if box is None or isinstance(box, str):
        return box
    return box.x, box.y, box.width, box.height


def _enabled(task):
    try:
        return bool(task.config.get(ENABLE_KEY, True))
    except Exception:
        return True


def _float_config(task, key, default, low, high):
    try:
        value = float(task.config.get(key, default))
    except (TypeError, ValueError, AttributeError):
        value = default
    return min(high, max(low, value))


def _keep_stuck_detection_at_one_second():
    """卡死检测按“相邻两次检测的画面变化”判断，检测变密后仍保持约 1 秒采样一次，避免误判卡死。"""
    original = utils.is_frame_stuck
    if getattr(original, "_speedup_wrapped", False):
        return

    def is_frame_stuck(task, stuck_threshold_seconds=30, change_threshold=0.08):
        now = time.time()
        if now - getattr(task, "_speedup_stuck_sample", 0.0) < 0.9 and hasattr(task, "_last_change_time"):
            return now - task._last_change_time >= stuck_threshold_seconds
        task._speedup_stuck_sample = now
        return original(task, stuck_threshold_seconds, change_threshold)

    is_frame_stuck._speedup_wrapped = True
    utils.is_frame_stuck = is_frame_stuck


def _patch_shop_exit():
    """自动卡厄思模式：商店页信用点小于 80 就离开。与“加速模式”开关无关。"""
    _replace_function(utils, "handle_shop", _shop_exit_wrapper)


def _on_shop_page(task):
    """与 handle_shop 相同的页面判定：移除卡牌，或移除位已售罄。"""
    box = utils.find_box_at_point(task, 0.729, 0.261)
    soldout = utils.find_box_at_point(task, 0.727, 0.286)
    return bool((box and "移除卡牌" in box.name) or (soldout and "售" in soldout.name))


def _read_shop_credit(task):
    """读到数字才返回；两个位置都不是数字时返回 None，避免把识别失败当成 0 然后退出。"""
    credit = None
    for pos_x, pos_y in _SHOP_CREDIT_POINTS:
        box = utils.find_box_at_point(task, pos_x, pos_y)
        if box and str(box.name).isdigit():
            credit = max(credit or 0, int(box.name))
    return credit


def _shop_refresh_enabled(task):
    """配置面板上的「刷新商店」。没这项时按关闭处理。"""
    config = getattr(task, "config", None)
    if config is not None and SHOP_REFRESH_KEY in config:
        return bool(config[SHOP_REFRESH_KEY])
    return bool(getattr(task, "default_config", {}).get(SHOP_REFRESH_KEY, False))


def _is_shop_refresh_box(task, box):
    """与原版点「免费」刷新用的是同一块区域，避免误伤休息区的「免费」。"""
    if box is None or "免费" not in getattr(box, "name", ""):
        return False
    width = getattr(task, "width", 0) or 1
    height = getattr(task, "height", 0) or 1
    center_x = (box.x + box.width / 2) / width
    center_y = (box.y + box.height / 2) / height
    return 0.012 <= center_x <= 0.258 and 0.892 <= center_y <= 0.979


def _shop_exit_wrapper(original):
    @functools.wraps(original)
    def handle_shop(task):
        if getattr(task, "name", None) != _CHAOS_MODE or not _on_shop_page(task):
            return original(task)
        credit = _read_shop_credit(task)
        if credit is not None and credit < _SHOP_EXIT_CREDIT:
            status = getattr(task, "node_status", None)
            if isinstance(status, dict):
                status["shop"] = False  # 否则离开后休息页会因为 shop 仍为 True 再次进入
            task.log_info(f"德朗商店: 当前信用点={credit}，小于{_SHOP_EXIT_CREDIT}，退出商店")
            leave = getattr(utils, "handle_leave", None)
            if leave and leave(task):
                return True
            task.log_info("德朗商店: 离开按钮未点到，下一轮再试，期间不再购买")
            return True
        if _shop_refresh_enabled(task):
            return original(task)
        skipped = False
        click_box = task.click_box

        def click_box_no_refresh(box=None, *args, **kwargs):
            nonlocal skipped
            if _is_shop_refresh_box(task, box):
                skipped = True
                task.log_info("德朗商店: 刷新商店已关闭，不点击「免费」")
                return True
            return click_box(box, *args, **kwargs)

        task.click_box = click_box_no_refresh
        try:
            result = original(task)
        finally:
            task.click_box = click_box
        if skipped:
            return False  # 没刷新，交给后面的离开按钮
        return result

    return handle_shop


def _patch_decompose_checkbox():
    """分解存档资料确认框：先勾选再确认。与“加速模式”开关无关。"""
    _replace_function(utils, "handle_center_confirm", _decompose_checkbox_wrapper)


def _text_has(box, needles):
    name = getattr(box, "name", "") or ""
    return any(needle in name for needle in needles)


def _find_text(task, needles):
    return next((box for box in getattr(task, "all_texts", []) or [] if _text_has(box, needles)), None)


def _checkbox_point(task, label):
    x = label.x / task.width - _CHECKBOX_LEFT
    y = (label.y + label.height / 2) / task.height
    return x, y


def _checkbox_checked(task, x, y):
    """勾选框填的是橙色，中间对勾是白的。周围橙色够多才算已勾上。"""
    frame = getattr(task, "frame", None)
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    px, py = int(x * width), int(y * height)
    half = max(4, int(_CHECKBOX_SAMPLE * width))
    x1, y1 = max(0, px - half), max(0, py - half)
    x2, y2 = min(width, px + half), min(height, py + half)
    region = frame[y1:y2, x1:x2, :3]
    if region.size == 0:
        return None
    blue, _, red = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    orange = (red > 170) & (blue < 100) & (red.astype(int) > blue.astype(int) + 60)
    return float(orange.mean()) >= 0.2


def _ensure_decompose_checkbox(task):
    """是分解存档确认框且勾选框没勾上时点一下。不是这个框返回 False。"""
    if _find_text(task, _DECOMPOSE_TITLE) is None:
        return False
    label = _find_text(task, _DECOMPOSE_CHECK)
    if label is None:
        return False
    x, y = _checkbox_point(task, label)
    checked = _checkbox_checked(task, x, y)
    if checked:
        task.log_info("分解存档资料：下次登入前不再显示已勾选")
        return False
    if checked is None:
        task.log_info("分解存档资料：读不到勾选框颜色，不点击，避免把已勾选取消")
        return False
    task.log_info("分解存档资料：勾选下次登入前不再显示")
    task.click_relative(x, y)
    time.sleep(0.4)
    return True


def _decompose_checkbox_wrapper(original):
    @functools.wraps(original)
    def handle_center_confirm(task):
        _ensure_decompose_checkbox(task)
        return original(task)

    return handle_center_confirm


def _patch_storage_capacity():
    """零式系统法典卡片：游戏更新后「存储数据价值 N」换成了「存档资料储存上限 N pt」。
    原版读不到存储数据价值就点重新合成，改版后会一直重新合成下去。与“加速模式”开关无关。"""
    utils_chaos = _import("utils_chaos")
    current = getattr(utils_chaos, "handle_zero_system_initial_page", None)
    # 本仓库的 utils_chaos 已经按存档储存上限判断（并写详细日志），只有官方原版才需要这层修正
    if current is not None and not getattr(current, "handles_storage_capacity", False):
        _replace_function(utils_chaos, "handle_zero_system_initial_page", _storage_capacity_wrapper)


def _zero_system_value_text(task):
    """与 handle_zero_system_initial_page 相同的页面判定；不是这个页面返回 None。"""
    title_text = utils._get_region_text(task, (0.076, 0.011, 0.291, 0.106))
    if utils._get_game_text(task, "零式系统") not in title_text:
        return None
    if "进入" not in utils._get_region_text(task, (0.681, 0.850, 0.988, 0.972)):
        return None
    return utils._get_region_text(task, (0.685, 0.317, 0.980, 0.825))


def _storage_capacity_wrapper(original):
    @functools.wraps(original)
    def handle_zero_system_initial_page(task):
        value_text = _zero_system_value_text(task)
        if value_text is None:
            return original(task)
        storage = re.escape(utils._get_game_text(task, "存储数据"))
        capacity = re.search(rf"{storage}.{{0,4}}上限\s*(\d+)\s*pt", value_text, re.IGNORECASE)
        if capacity is None:
            if re.search(rf"{storage}价值\s*(\d+)", value_text):
                task._storage_read_misses = 0
                return original(task)  # 旧版卡片，照原逻辑按层级判断
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
        task._storage_read_misses = 0
        value = int(capacity.group(1))
        required = utils._get_config_value(task, STORAGE_CAPACITY_KEY, 0)
        try:
            required = int(required)
        except (TypeError, ValueError):
            required = 0
        task.log_info(f"零式系统当前存档储存上限={value}pt，要求大于等于{required}pt")
        if value >= required:
            if battle_log.once(task, "零式系统进入"):
                battle_log.record(task, "零式系统", value=value, unit="pt", required=required, decision="进入")
            return False
        task.log_info("存档储存上限未达要求，点击进入重新合成")
        battle_log.reroll(task, "零式系统", value=value, unit="pt", required=required)
        utils._move_and_click(task, 0.968, 0.153)
        task.sleep(1)
        return True

    return handle_zero_system_initial_page


def _patch_memory_imprint():
    """「选择刻印的记忆」页（游戏更新后零式系统「雪上凝结的约定」出现）：原版没有处理函数，
    三张记忆卡都没选时「确认」是灰的，页面一直停着。插在卡厄思模式「确认」按钮之前。"""
    utils_chaos = _import("utils_chaos")
    handlers = getattr(utils_chaos, "PAGE_HANDLERS", None)
    if not isinstance(handlers, list) or any(h.__name__ == "handle_memory_imprint" for h in handlers):
        return
    index = next((i for i, h in enumerate(handlers) if h.__name__ == "handle_confirm"), len(handlers))
    handlers.insert(index, handle_memory_imprint)


def match_memory(texts, priority):
    """texts 为三张记忆卡的文字。按优先级顺序找第一个有卡包含的关键词，返回 (第几张 0~2, 理由)，都没有返回 None。"""
    cleaned = [_PUNCTUATION.sub("", t) for t in texts]
    for word in priority:
        key = _PUNCTUATION.sub("", str(word))
        if not key:
            continue
        for index, text in enumerate(cleaned):
            if key in text:
                return index, f"记忆卡牌优先级「{word}」"
    return None


def choose_memory(texts, priority, default):
    """返回 (第几张 0~2, 理由)。按优先级匹配；都没有时按默认选择（「1」~「3」或「随机」）。"""
    matched = match_memory(texts, priority)
    if matched is not None:
        return matched
    if default == "随机":
        return random.randrange(len(texts)), "没有匹配的记忆卡，随机选"
    try:
        index = min(max(int(default), 1), len(texts)) - 1
    except (TypeError, ValueError):
        index = 0
    return index, f"没有匹配的记忆卡，按默认选第{index + 1}张"


def handle_memory_imprint(task):
    title = utils._get_region_text(task, _MEMORY_TITLE)
    if "刻印的记" not in title and "刻印的記" not in title:
        return False
    x1, y1, x2, y2 = _MEMORY_CONFIRM
    confirm = next((b for b in task.all_texts
                    if (utils._clean_match(b.name, "确认") or "確認" in b.name)
                    and x1 <= (b.x + b.width / 2) / task.width <= x2
                    and y1 <= (b.y + b.height / 2) / task.height <= y2), None)
    if confirm is not None and utils.is_button_active(task, confirm):
        return False  # 已经选好：交给 handle_confirm 点确认
    texts = [utils._get_region_text(task, region) for region in _MEMORY_CARDS]
    priority = utils._get_config_value(task, MEMORY_PRIORITY_KEY, [])
    if isinstance(priority, str):
        priority = [priority]
    default = utils._get_config_value(task, MEMORY_DEFAULT_KEY, "1")
    if priority and any(texts) and match_memory(texts, priority) is None:
        for slot, region in enumerate(_MEMORY_CARDS, 1):
            refresh = utils._get_region_text(task, (region[0], _MEMORY_REFRESH_Y[0], region[2], _MEMORY_REFRESH_Y[1]))
            count = re.search(r"(\d+)\s*/\s*\d+", refresh)
            if count and int(count.group(1)) > 0:
                task.log_info(f"选择刻印的记忆：三张都不含优先级关键词，重新搜索第{slot}张（剩 {count.group(1)} 次），"
                              f"三张=「{'」「'.join(texts)}」")
                battle_log.record(task, "记忆卡刷新", options=texts, slot=slot, remaining=int(count.group(1)))
                utils._move_and_click(task, (region[0] + region[2]) / 2, sum(_MEMORY_REFRESH_Y) / 2)
                task.sleep(1)
                return True
    index, reason = choose_memory(texts, priority, default)
    task.log_info(f"选择刻印的记忆：选第{index + 1}张（{reason}），三张=「{'」「'.join(texts)}」")
    if not any(texts):
        battle_log.anomaly(task, "记忆卡读不到文字", "三张记忆卡区域都没有文字，按默认选择")
    battle_log.record(task, "记忆卡选择", options=texts, chosen=index + 1, reason=reason)
    region = _MEMORY_CARDS[index]
    utils._move_and_click(task, (region[0] + region[2]) / 2, _MEMORY_CARD_Y)
    task.sleep(1)
    return True
