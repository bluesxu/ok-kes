from ok import TriggerTask, og

import battle_log
import config_layout
import speedup
import utils_battle
import utils_sortie
from config_io import (
    make_export_callback,
    make_import_callback,
    make_save_local_config_callback,
    make_switch_local_config_callback,
    migrate_game_language_config_file,
)
from config_sync import check_upload_if_needed, show_hot_configs_dialog
from utils import (
    reset_all_status,
    _migrate_route_boss_to_elite,
    _simplify_texts,
)


class SortieMode(TriggerTask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "自动出击模式"
        self.description = "1. 自动战斗依赖按键识别，请在游戏设置中打开快捷键显示，提升出牌准确率。\n2. 国际服玩家请将本模式配置中的\"游戏语言\"设置为繁体中文。"
        self.instructions = """<a href="https://github.com/ok-oldking/ok-py">ok-py</a>"""
        self.trigger_interval = 1
        self.all_texts = []
        self.default_config["_enabled"] = False
        self.default_config["配置操作"] = ""
        self.default_config["游戏语言"] = "简体中文"
        self.default_config["出战主战员优先级"] = ["海德玛丽", "九", "力", "绯"]
        self.default_config["主战员优先级"] = ["米卡", "尼娅", "蒂菲拉", "麦格纳", "卡修斯"]
        self.default_config["领取奖励"] = False
        self.default_config["出牌优先级"] = ["剑雨", "水之源", "一缕光芒", "万众英雄","极光剑", "展开极光","解放极光"]
        self.default_config["获得卡牌优先级"] = ["展开极光", "剑雨", "一缕光芒","缕光芒","凝聚极光"]
        self.default_config["移除卡牌列表"] = ["剑幕"]
        self.default_config["复制卡牌列表"] = ["剑雨", "展开极光", "一缕光芒","缕光芒"]
        self.default_config["闪光卡牌列表"] = ["剑雨", "展开极光", "一缕光芒","缕光芒"]
        self.default_config["拉黑卡牌"] = []
        self.default_config["闪光优先级"] = []
        self.default_config["装备1号位优先级"] = ["蚀化臂铠"]
        self.default_config["装备2号位优先级"] = ["拷问工具箱"]
        self.default_config["装备3号位优先级"] = ["异象石碑"]
        self.default_config["只打第一层"] = True
        self.default_config["进入商店"] = False
        self.default_config["优先移除基础牌"] = True
        self.default_config["几轮后停止(0为不停止)"] = 0
        self.default_config["卡牌奖励优先级"] = ["梦之边境", "装备包"]
        self.default_config["任务优先级"] = ["选取随机3条命运","信用点增加", "移除"]
        self.default_config["拉黑任务"] = ["咒术卡牌", "压力"]
        self.default_config["拉黑主战员"] = ["黛安娜", "阿黛尔海特"]
        self.default_config["生命值大于多少优先闪光(百分比)"] = "60"
        self.default_config["路线优先级"] = ["休息", "事件", "小怪", "精英"]
        self.default_config["第几层boss前自动暂停"] = "不暂停"
        self.node_status = {"shop": False, "flash_or_rest": False, "reach_final_boss": False, "final_boss_battle": False, "pass_final_boss_count": 0, 
                            "total_rounds": 0, "success_rounds": 0, "node_count": 0, "enter_new_node": False, "node_type": "",
                            "is_escaped": False, "save_target_member": False,
                            "removed_card_count": 0, "neutral_card_count": 0}
        self.member_status = {
            "equipment": {
                "names": ["", "", ""],
                "descriptions": ["", "", ""],
                "qualities": ["", "", ""],
            },
            "deck": {},
            "recruit_roles": {},
        }

        self._last_upload_time = 0
        self.config_type = {
            '游戏语言': {'type': 'drop_down', 'options': ['简体中文', '繁体中文']},
            '配置操作': {
                'type': 'button',
                'buttons': [
                    {'text': '导入配置码', 'callback': make_import_callback(self)},
                    {'text': '导出配置码', 'callback': make_export_callback(self)},
                    {'text': '热门配置', 'callback': self._show_hot_configs},
                    {'text': '保存配置', 'callback': make_save_local_config_callback(self, 'sortie')},
                    {'text': '切换配置', 'callback': make_switch_local_config_callback(self, 'sortie')},
                ],
            },
            '第几层boss前自动暂停': {'type': 'drop_down', 'options': ['不暂停', '1', '2', '3']},
        }
        self.config_description['游戏语言'] = "国际服请设置为繁体中文"
        self.config_description['闪光卡牌列表'] = (
            "休息区闪光时选哪张牌，填牌名；本局这些牌都闪过（选牌页里不再出现）后，休息区改为休息"
        )
        self.config_description['闪光优先级'] = (
            "闪光出来的 3 个版本选哪个：「牌名:关键词」只对这张牌生效，不带牌名的对所有牌生效；"
            "关键词写描述里的几个字，顺序与原文一致。都没命中时保留原类型、选数值最大的"
        )
        # 出牌策略与详细战斗日志的配置项
        utils_battle.install(self)
        battle_log.install(self)
        # 实验性加速模式：新增配置项并接管等待逻辑，关闭时行为与原来完全一致
        speedup.install(self)
        # 所有配置项都加完后按分组重排、隐藏平时不改的项、补全说明
        config_layout.apply(self, config_layout.SORTIE_ORDER)

    def load_config(self):
        migrate_game_language_config_file(self)
        super().load_config()

    def enable(self):
        """开启出击模式时自动禁用卡厄思模式，重置状态并迁移配置。"""
        from ChaosMode import ChaosMode
        chaos = og.executor.get_task_by_class(ChaosMode)
        if chaos and chaos.enabled:
            chaos.disable()
        reset_all_status(self)
        _migrate_route_boss_to_elite(self)
        battle_log.maybe_cleanup(self, force=True)
        super().enable()

    def _check_upload_if_needed(self):
        check_upload_if_needed(self, "sortie")

    def _show_hot_configs(self):
        show_hot_configs_dialog(self, "sortie")

    def run(self):
        self.all_texts = _simplify_texts(self.ocr())
        for handle_page in utils_sortie.PAGE_HANDLERS:
            if handle_page(self):
                return
        # 帧末尾检查是否需要上传配置
        self._check_upload_if_needed()
