"""
卡厄思模式加速模式（实验性，默认关闭）。

原逻辑每次点击后按固定时长等待（click_box 自带 1 秒，处理函数再 sleep 0.5~2 秒），
_move_and_click 点击前还固定悬停 0.5 秒，而界面通常不到 1 秒就切换完成。

开启后：
1. 延迟支付等待：处理函数里的 sleep 先记账不真睡，之后它若还要看画面或再操作，
   先把欠的时间补足，保持原有语义；处理函数结束时还欠着的等待交给第 2 步。
2. 文字闸门：只做了一次点击的帧，下一轮 OCR 后先判断页面是否已响应——
   被点的文字还在原处就继续等；页面文字变了，再多识别一次确认不再变化才交给处理函数。
   无论如何不会晚于原逻辑的等待时长。游戏背景一直有动画，所以只看文字，不看像素。
3. 点击前悬停 0.5 秒缩短为可配置值；非战斗时检测间隔 1 秒降为可配置值。
4. 并行模板匹配：路线节点、小地图、牌库和选卡页的卡牌类型都是在同一帧、同一区域里逐个匹配多种图标，
   请求组内第一个图标时整组并行算完并缓存，后续直接取结果；每个图标的计算与原来完全相同。
5. 路线页识别前原本固定等 1 秒让图标入场动画放完：改为至少等 0.4 秒、且连续两次识别到的图标位置一致就继续，
   最长仍是 1 秒；没识别到任何图标（Boss 节点）时等满 1 秒。
6. 牌库翻页原本每次滚 3 格只移动半行，改为连发两次滚动（约一整行），滚完后的等待不变。
7. （独立开关）整库扫描确认本局牌库已没有“移除卡牌列表”里的卡后，本局再遇到删卡只看最底页（仍会删咒术卡）
   后照原逻辑跳过，事件里含“移除”的任务优先级排到最后；换一局或重新开启任务后重新检查。
只作用于卡厄思模式，关闭任务配置里的“加速模式”即完全使用原逻辑。

注意：本文件不能定义顶层类，框架会把 ok_tasks 下含类的 .py 当作任务加载。
"""
import inspect
import os
import time
from concurrent.futures import ThreadPoolExecutor

import config_io
import utils
import utils_chaos

ENABLE_KEY = "加速模式"
HOVER_KEY = "点击前悬停等待(秒)"
INTERVAL_KEY = "非战斗检测间隔(秒)"
REMOVAL_KEY = "目标卡删完后不再整库翻找"

_GRID = 10              # 文字中心按 10x10 网格量化后比较页面
_SAME_PAGE = 0.6        # 与点击前文字布局相似度 >= 0.6 视为页面还没响应
_STABLE = 0.75          # 相邻两次识别相似度 >= 0.75 视为文字已稳定
_BUTTON_TOLERANCE = 0.02
_GATE_INTERVAL = 0.05   # 闸门等待期间尽快再识别
_BATTLE_INTERVAL = 1.0  # 战斗中保持原逻辑的 1 秒，避免多占 CPU
_STATS_EVERY = 30
_EXPECTED_RUN_SOURCE = (
    "self.all_texts = _simplify_texts(self.ocr())",
    "utils_chaos.PAGE_HANDLERS",
    "self._check_upload_if_needed()",
)
# 处理函数会在同一帧、同一区域、同一阈值下依次匹配的图标组
_PARALLEL_GROUPS = (
    ("safezone", "enemy", "elite", "event", "settlement", "shop", "kalei", "seal", "hard"),  # 路线页节点与标记
    ("position_in_map", "settlement_in_map", "enemy_in_map", "safezoom_in_map", "elite_in_map",
     "event_in_map"),  # 小地图节点（阈值 0.85）
    ("kalei_in_map", "shop_in_map", "seal_in_map", "hard_in_map"),  # 小地图特殊标记（阈值 0.65）
    ("attack_in_deck", "skill_in_deck", "enhance_in_deck", "hex_in_deck", "hex_in_deck_tw"),  # 牌库卡牌类型
    ("attack", "skill", "enhance", "hex", "abnormal"),  # 选卡页卡牌类型
)
_GROUP_OF = {name: group for group in _PARALLEL_GROUPS for name in group}
_PREFETCH_KWARGS = {"feature_name", "box", "threshold"}
_match_pool = None
_ROUTE_TOLERANCE = 3        # 路线图标两次识别位置相差不超过 3 像素视为已停止入场动画
_ROUTE_MIN_SETTLE = 0.4     # 至少等 0.4 秒，防止图标依次出现时把“已出现的一部分”当成全部
_ROUTE_POLL = 0.05
_DECK_SCROLLS = 2           # 牌库每次翻页发送的滚动次数（原来 1 次 = 3 格 ≈ 半行）
_JUMP_SCROLLS = 10          # 已确认没有目标卡时，一次滚到最底页
_SKIP_WORDS = ("跳过", "取消")  # 删卡流程没找到卡时点的按钮


def install(task):
    """给卡厄思模式任务实例装上加速逻辑，重复调用无副作用。"""
    if getattr(task, "_speedup", None) is not None:
        return
    task.default_config[ENABLE_KEY] = False
    task.default_config[HOVER_KEY] = 0.1
    task.default_config[INTERVAL_KEY] = 0.3
    task.default_config[REMOVAL_KEY] = True
    task.config_description[ENABLE_KEY] = "实验性：点击后页面一响应就继续，不再固定等待；关闭时完全使用原逻辑"
    task.config_description[HOVER_KEY] = "仅在开启加速模式时生效：鼠标移到目标后等待多久再点击，原逻辑固定 0.5 秒"
    task.config_description[INTERVAL_KEY] = "仅在开启加速模式时生效：非战斗时两次识别的最短间隔，原逻辑 1 秒；战斗中保持 1 秒"
    task.config_description[REMOVAL_KEY] = (
        "需开启加速模式，且关闭“优先移除基础牌”和“刷空档”：整库确认本局已没有“移除卡牌列表”里的卡后，"
        "本局再删卡只看最底页（仍删咒术卡）就跳过，事件里含“移除”的任务优先级排到最后"
    )
    # 加速选项只影响本机，不写进导出的配置码和上传的统计
    config_io.UI_ONLY_CONFIG_KEYS.update({ENABLE_KEY, HOVER_KEY, INTERVAL_KEY, REMOVAL_KEY})

    orig = {
        name: getattr(task, name)
        for name in ("sleep", "click", "move", "scroll", "mouse_down", "mouse_up", "send_key", "run", "find_feature")
    }
    st = {
        "orig_sleep": orig["sleep"], "active": False, "gate_ok": _run_is_compatible(task),
        "in_run": False, "in_action": False, "owed_until": 0.0, "actions": 0,
        "moved": False, "held": False,
        "action_sig": None, "action_box": None, "action_time": 0.0,
        "gate": None, "prev_sig": None, "hit": None, "battle": False,
        "planned": 0.0, "actual": 0.0, "count": 0, "saved_total": 0.0,
        "match_cache": None,
        # 删卡：removal_exhausted 保存确认“没有目标卡”时那一局的 member_status 对象
        "removal_exhausted": None, "collect_cards": False, "seen_cards": [], "skip_clicked": False,
        "jump_scroll": False,
    }
    task._speedup = st
    if not st["gate_ok"]:
        task.log_info("加速模式：ChaosMode.run() 结构与 speedup.py 预期不一致，文字闸门已停用，其余优化照常")

    def sleep(timeout):
        if not st["active"] or timeout is None or timeout <= 0:
            return orig["sleep"](timeout)
        if st["held"] or task.executor.paused:
            _pay_owed(st)
            return orig["sleep"](timeout)
        if st["moved"]:
            st["moved"] = False
            _pay_owed(st)
            hover = _float_config(task, HOVER_KEY, 0.1, 0.0, 1.0)
            st["saved_total"] += max(0.0, timeout - hover)
            return orig["sleep"](min(timeout, hover))
        # 连续多次 sleep 依次累加，与原逻辑串行等待一致
        st["owed_until"] = max(st["owed_until"], time.time()) + timeout
        task.executor.reset_scene(check_enabled=False)
        return True

    def action(name, point_of):
        def wrapped(*args, **kwargs):
            if not st["active"] or st["in_action"]:
                return orig[name](*args, **kwargs)
            st["in_action"] = True
            try:
                _pay_owed(st)
                if name == "mouse_down":
                    st["held"] = True
                    st["moved"] = False
                else:
                    if name == "mouse_up":
                        st["held"] = False
                    if (name == "click" and st["collect_cards"]
                            and any(word in str(kwargs.get("name") or "") for word in _SKIP_WORDS)):
                        st["skip_clicked"] = True
                    _note_action(task, st, point_of(task, args, kwargs))
                return orig[name](*args, **kwargs)
            finally:
                st["in_action"] = False
        return wrapped

    def move(*args, **kwargs):
        if st["active"]:
            _pay_owed(st)
            st["moved"] = True
        return orig["move"](*args, **kwargs)

    def find_feature(*args, **kwargs):
        name = kwargs.get("feature_name")
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
                  moved=False, held=False, hit=None, action_sig=None, action_box=None, match_cache=None)
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
    task.scroll = action("scroll", _no_point)
    task.mouse_down = action("mouse_down", _no_point)
    task.mouse_up = action("mouse_up", _no_point)
    task.send_key = action("send_key", _no_point)
    task.move = move
    task.find_feature = find_feature
    task.run = run
    _keep_stuck_detection_at_one_second()
    _patch_card_helpers()


def _run_is_compatible(task):
    """加速模式会接管 run()，只在其源码仍是“OCR -> 依次尝试页面处理函数 -> 上传检查”时启用闸门；
    修改 ChaosMode.run() 后需同步 _gated_run。"""
    try:
        source = inspect.getsource(type(task).run)
    except (OSError, TypeError):
        return False
    return all(snippet in source for snippet in _EXPECTED_RUN_SOURCE)


def _gated_run(task, st):
    """与 ChaosMode.run() 相同，只在交给页面处理函数前多一道文字闸门。"""
    texts = utils._simplify_texts(task.ocr())
    if _gate_blocks(task, st, texts):
        return
    task.all_texts = texts
    for handle_page in utils_chaos.PAGE_HANDLERS:
        if handle_page(task):
            st["hit"] = handle_page.__name__
            return
    task._check_upload_if_needed()


def _finish_run(task, st):
    now = time.time()
    if st["owed_until"] > now:
        if st["gate_ok"] and st["actions"] == 1 and st["action_sig"] is not None:
            st["gate"] = {
                "sig": st["action_sig"], "box": st["action_box"],
                "start": st["action_time"], "until": st["owed_until"], "changed": False,
            }
            st["owed_until"] = 0.0
        else:
            _pay_owed(st)  # 多步操作或没有点击：保持原逻辑的等待时长
    if st["hit"] == "handle_battle_auto_check":
        st["battle"] = True
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
            f"加速统计：最近{st['count']}次点击后的等待，原逻辑{st['planned']:.1f}秒，实际{st['actual']:.1f}秒"
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
    st["owed_until"] = 0.0
    if remaining > 0:
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


def _removal_shortcut_enabled(task):
    """只有删卡流程本来就只删目标卡时才启用：开着“优先移除基础牌”或“刷空档”时删卡会删别的卡。"""
    try:
        config = task.config
        return (bool(config.get(REMOVAL_KEY, True))
                and config.get("优先移除基础牌", True) is False
                and config.get("刷空档", False) is not True)
    except Exception:
        return False


def _removal_exhausted(task, st):
    marker = st["removal_exhausted"]
    if marker is not None and marker is not getattr(task, "member_status", None):
        st["removal_exhausted"] = marker = None  # 新的一局或重新开启任务：重新检查
    return marker is not None


def _patch_card_helpers():
    """包装 utils 里的翻页滚动、选卡、牌库识别和列表配置读取；只在加速模式运行中生效。"""
    if getattr(utils._scroll_card_page, "_speedup_wrapped", False):
        return
    orig_scroll = utils._scroll_card_page
    orig_select = utils.select_card
    orig_recognize = utils.recognize_cards_in_deck
    orig_card_list = utils._get_card_list

    def _scroll_card_page(task, x, y, amount, page, *args, **kwargs):
        st = _active_state(task)
        if st is not None and str(page).startswith("select_card") and not task.is_adb():
            # 原逻辑每次只发 1 次 3 格滚动（约半行）；先补发几次，再交给原逻辑发最后一次并等待
            extra = (_JUMP_SCROLLS if st["jump_scroll"] and amount < 0 else _DECK_SCROLLS) - 1
            task.move_relative(x, y)
            task.sleep(0.05)
            for _ in range(extra):
                task.scroll_relative(x, y, amount)
        return orig_scroll(task, x, y, amount, page, *args, **kwargs)

    def select_card(task, card_names, count=1, action="", *args, **kwargs):
        st = _active_state(task)
        if st is None or action != "移除" or not _removal_shortcut_enabled(task):
            return orig_select(task, card_names, count, action, *args, **kwargs)
        if _removal_exhausted(task, st):
            task.log_info("加速：本局已确认牌库里没有要移除的目标卡，直接看最底页（咒术卡）")
            st["jump_scroll"] = True
            try:
                return orig_select(task, [], count, action, *args, **kwargs)
            finally:
                st["jump_scroll"] = False
        st.update(collect_cards=True, seen_cards=[], skip_clicked=False)
        try:
            result = orig_select(task, card_names, count, action, *args, **kwargs)
        finally:
            st["collect_cards"] = False
        targets = [t.strip() for t in card_names if isinstance(t, str) and t.strip()]
        names = [n.strip() for n in st["seen_cards"] if n and n.strip()]
        # 与 select_card 的命中规则一致：目标名包含卡名或卡名包含目标名
        if st["skip_clicked"] and targets and names and not any(t in n or n in t for t in targets for n in names):
            st["removal_exhausted"] = getattr(task, "member_status", None)
            task.log_info("加速：整库扫描没有要移除的目标卡，本局后续删卡只看最底页")
        return result

    def recognize_cards_in_deck(task, *args, **kwargs):
        cards = orig_recognize(task, *args, **kwargs)
        st = getattr(task, "_speedup", None)
        if st is not None and st["collect_cards"]:
            st["seen_cards"].extend(str(card.get("name", "")) for card in cards or [])
        return cards

    def _get_card_list(task, key, *args, **kwargs):
        value = orig_card_list(task, key, *args, **kwargs)
        st = _active_state(task)
        if (key == "任务优先级" and st is not None and _removal_shortcut_enabled(task)
                and _removal_exhausted(task, st)):
            value = [v for v in value if "移除" not in str(v)] + [v for v in value if "移除" in str(v)]
        return value

    for wrapper in (_scroll_card_page, select_card, recognize_cards_in_deck, _get_card_list):
        wrapper._speedup_wrapped = True
    utils._scroll_card_page = _scroll_card_page
    utils.select_card = select_card
    utils.recognize_cards_in_deck = recognize_cards_in_deck
    utils._get_card_list = _get_card_list


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
        return bool(task.config.get(ENABLE_KEY, False))
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
