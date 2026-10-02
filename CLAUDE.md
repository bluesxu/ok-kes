# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概况

ok-kes：基于 [ok-script](https://github.com/ok-oldking/ok-script)（PyPI 包 `ok-script-kes`）的《卡厄思梦境》自动化工具，只用截图 + OCR + 模板匹配 + 模拟点击/按键，Windows / Python 3.12。
代码、注释、日志、配置键、提交信息都用中文。本仓库是 `baoxin1100/ok-kes` 的 fork，`origin` 指向 `bluesxu/ok-kes`，`upstream` 为原作者仓库。

## 常用命令

2026-10 起本仓库从官方版独立出来，直接用仓库 `.venv` 运行 `main.py`（配置、日志都在仓库的 `configs/`、`battle_logs/`、`logs/` 下，均被 git 忽略），不再装进官方安装目录；`speedup_patch/` 只作备用，不往原作者的热门配置库上传（`config_sync.AUTO_UPLOAD`）。
`ok` 框架来自 `requirements.txt` 里的 `ok-script-kes`，不在仓库中（`.gitignore` 忽略了 `ok/`）。优先用仓库 `.venv`；没有时可借用已安装的 ok-kes：
解释器 `D:\Program Files\ok-kes\data\apps\ok-kes\python\python.exe`，并把 `PYTHONPATH` 设为 `D:\Program Files\ok-kes\data\apps\ok-kes\working`（里面有 `ok/`）。

```powershell
pip install -r requirements.txt
python main.py                      # 启动 GUI；main_debug.py 为调试模式
.\run_tests.ps1                     # 逐个跑 tests\*.py（CI 同样如此）
python -m unittest tests/TestSpeedup.py
python -m unittest tests.TestSpeedup.TestSpeedup.test_card_play_waits_for_raise_and_hand_count   # 单个用例
```

`speedup_patch/tests` 下是另一套独立测试，不能放进 `tests/`（它自带 `ok.py`、`utils.py` 等替身模块，会遮住真实模块）：

```powershell
cd speedup_patch\tests; python test_speedup.py       # 替身环境下的离线测试，脚本式输出「N/N 项通过」
python real_bugfix_check.py                           # 用官方安装目录里的原版 utils 验证补丁修正（需本机装有 ok-kes）
```

没有 lint 配置。打包见 `BUILD.md`；正式发布由推送 `v*` 标签触发 `.github/workflows/build.yml`（跑测试 → 按 `deploy.txt` 同步到更新库 → pyappify 打包 → Release）。

## 架构

**任务加载**：`src/config.py` 的 `config` 字典是 ok-script 的应用配置（窗口、截图方式、OCR 后端、模板标注 `ok_tasks/assets/coco_annotations.json` 等）。`custom_tasks: True` 让框架扫描 `ok_tasks/` 下的 `.py`，把其中定义的类当任务加载，且 `ok_tasks/` 在导入路径上，所以模块之间直接 `import utils`、`import utils_chaos`。**因此 `ok_tasks/` 下的工具模块不能定义顶层类**，否则会被当成任务。运行中修改 `ok_tasks/` 文件会触发热重载。`src/tasks/` 是框架模板里的示例任务（`TestMain.py` 测的就是它）。

**模式 = TriggerTask + 页面处理函数列表**：`ChaosMode`（自动卡厄思）、`SortieMode`（自动出击）、`StoryMode` 都是 `TriggerTask`，`run()` 每帧做一次全屏 OCR，存到 `task.all_texts`（经 `_simplify_texts` 把繁体/日文字形转成简体），然后按顺序调用对应模块的 `PAGE_HANDLERS`（`utils_chaos.py` / `utils_sortie.py` / `utils_story.py`），第一个返回 True 的处理函数结束本帧。
- 每个处理函数形如 `handle_xxx(task) -> bool`：先用固定相对坐标或文字判断“是不是这个页面”，不是就返回 False。**列表顺序就是优先级**，列表里的行尾注释说明了为什么要排在前面。
- `utils.py`（约 4400 行）是两种模式共用的处理函数和工具：`find_box_at_point`（按相对坐标找 OCR 框）、`_move_and_click`（先悬停再点击）、`_clean_match`、生命值/信用点读取、选卡/牌库识别等。`utils_chaos` / `utils_sortie` 用 `from utils import ...` 按名字导入这些函数。
- 跨帧状态存在任务实例上（`task.node_status`、`task.member_status` 等），由 `utils.reset_all_status` 复位。
- 坐标一律是相对屏幕的 0~1 值（基准 2560×1440，16:9）。国服简体、国际服繁体都要支持：匹配文字时考虑两种写法，或依赖 `_simplify_texts` 统一成简体。注意 `wait_ocr` 等框架方法返回的文字不经过 `_simplify_texts`。

**配置**：配置键就是中文显示名，会持久化到 `configs/*.json`。`config_io.py` 负责配置码导入/导出、本地多套配置、旧配置迁移（在 `load_config` 里调用），`UI_ONLY_CONFIG_KEYS` 里的键不进配置码也不上传；`config_sync.py` 负责匿名上传配置/胜率和“热门配置”。界面顺序、隐藏项、子选项和缺省说明统一在 `config_layout.py`（两个模式 `__init__` 末尾调用 `apply`），新增配置项要排进 `CHAOS_ORDER` / `SORTIE_ORDER`，否则 `TestConfigLayout` 会失败。`config_description` 等界面文字要同步 `i18n/<locale>/LC_MESSAGES/ok.po`，并重新编译 `ok.mo`（`msgid` 必须与代码字符串完全一致）。

**给某个角色写配置（查构筑资料 → 出配置码）**：产出是配置码（配置 JSON 的 base64，格式同 `config_io._export_config_to_text`），不改仓库文件。
- 资料来源，按可信度排：
  1. 玩家上传的真实配置：用 `config_sync.py` 里的 `SUPABASE_URL` / `SUPABASE_ANON_KEY` 分页拉 `configs` 表（`mode` 为 `sortie`/`chaos`），解码 `config_b64` 后按 `first_member` 或卡名筛选。这里的卡名、闪光描述是游戏里的原文，神闪词条原文（如「赋予敌人脆弱2」）也只在这里好找。
  2. gamekee 角色页（`gamekee.com/czn/<id>`）：有国服卡面和每张牌 ①~⑤ 号灵光一闪的全文。正文在页面引用的 `api-cdn.gamekee.com/.../content/<id>.json` 里；curl 和 jina 会被 EdgeOne 拦（567），要在浏览器里打开角色页，再用页面内的 `fetch` 去取。Bwiki（`wiki.biligame.com/czn`）的 `api.php` 偶尔能用，页面本身常被拦。cznbuilds.com 有装备、配队推荐。
  3. B站攻略视频：通常没有字幕，用 `bili audio <BV> --no-split` 下载后跑 `agent-reach transcribe`。转写出来的卡名、装备名是同音字（曾把「定位雷射」听成「追踪雷射」），只能用来取打法思路，名字必须拿 1、2 核对。
- 写配置时的匹配规则：配置读取和 OCR 文字都会转成简体，国际服繁体名转完通常和国服一样，写一套简体即可。卡牌列表按「包含」匹配。「闪光优先级」写成 `牌名:关键词`，关键词按字依次出现比对（`_flash_rules` / `_flash_rule_matches`）。神闪 = 普通版本的描述末尾再加一行神词条，所以想要某个神闪就写 `牌名:普闪关键词+神词条关键词`，排在普闪规则前面。
- 导入配置码只覆盖码里写到的键，没写的键沿用用户原来的值（往往是别的角色的配置）。所以要把该模式会读到的键都写上（`grep _get_config_value` 查全）。出击最容易漏的是「卡牌奖励优先级」：出击模式的牌靠奖励页发放，而且可以一直刷新，只填核心牌（如只填「定位雷射」），其余都会被刷掉。

**出击模式出牌 `ok_tasks/utils_battle.py`**：`utils_sortie.handle_battle_page` 只保留 Ego 释放和「结束回合」按钮检测，出牌交给 `utils_battle.play_turn`（设计见 `CONTEXT.md` 术语表和 `docs/adr/0001`）。
- 手牌按张数排成固定扇形，`hand_slots` 由「N/10」推出每张牌的位置，**按键 = 位置序号**，不依赖 OCR 读牌上方的按键数字；牌名、类型、费用再按位置归属。
- 敌人以洋红色血条为锚点（`enemy_bars`），血量/护盾/行动倒计时/意图图标都相对血条定位；Boss 的倒计时 ∞ 会被 OCR 读成 8，用字形宽高比区分。
- 费用、倒计时这类压在彩色背景上的数字用 `_read_digit` 对裁剪区域试几种预处理；单张牌费用要两种预处理读数一致才采信，读不到的靠「AP不足」提示兜底。
- `choose_play` / `choose_target` 是纯函数，`tests/TestBattle.py` 用 `tests/images/battle` 的真实截图 + 真实 OCR 测识别，改坐标或阈值后要跑它。
- `battle_log.py` 写结构化记录（`battle_logs/*.jsonl`）和异常截图，并按保留天数/总大小清理。两个模式共用：出击模式在 `SortieMode` 里装开关，卡厄思模式由 `speedup.install` 装。事件名和字段见 `CONTEXT.md`「详细日志」，查日志时按那张表过滤；新增决定时在点击处记一行，同一种决定两个模式用同一个事件名。

**现场包 `ok_tasks/recorder.py` + `scripts/scene.py`**：排查自动化出的问题时先看这里，比 jsonl 和单张截图全。
- `speedup.install` 末尾给两个模式装上记录器（包在加速补丁接管的 `run` 和点击/按键/识别方法外层），内存里留最近 30 秒的每一帧；出问题时写出前后各 30 秒到 `battle_logs/现场/`。开关跟「详细战斗日志」+「异常截图」走，触发条件和去重规则见 `CONTEXT.md`「现场包」。加速关闭时记不到接手的处理函数。
- 目录内容：`meta.json`（模式、轮次、触发列表、配置）、`timeline.jsonl`（每帧一行：`hit` 接手的处理函数、`gated` 闸门等待、`texts` 全屏文字、`calls` 识别调用 → 结果、`actions` 动作、`events` 这一帧写的 jsonl 事件、`images` 画面编号）、`frames/` 1280 宽画面、`full/` 有动作那几帧的原尺寸画面、`state.pkl` 第一帧的跨帧状态，看完后写 `诊断.md`。
- 排查流程（用户说「看一下现场」时）：
  1. `python scripts/scene.py list` 列出还没有 `诊断.md` 的现场包；
  2. `scene.py timeline <包>` 读精简时间线（页面切换、动作、决定、异常，重复帧已合并），先只看文字；
  3. 需要看画面时 `scene.py draw <包> <帧序号>`，读 `annotated/` 下画了 OCR 框编号和动作位置（A1、A2…）的图，只挑关键的 2~3 帧；
  4. 改完代码 `scene.py replay <包>`，用记录的识别结果重跑页面处理函数，逐帧和当时的决定对比（`-v` 看重放日志）；「记录里没有的识别调用」是改代码后新增的识别：OCR 会在当时的画面上实际跑一遍，模板匹配一律当作没找到；
  5. 在包里写 `诊断.md`：结论、改了什么、还要观察什么。
- `scene.py html <包>` 生成 `index.html` 回放页，给用户在浏览器里逐帧翻看。
- 新增要写现场包的异常：把异常名加进 `recorder.TRIGGER_KINDS`。新的识别方法要能重放，就加进 `PERCEPTION_METHODS`，并在 `scene.ReplayTask` 里补上同名方法。

**加速模式 `ok_tasks/speedup.py`**：`ChaosMode` / `SortieMode` 在 `__init__` 末尾调用 `speedup.install(self)`，它通过猴子补丁接管任务的 `sleep`/`click*`/`run` 等方法，以及 `utils*` 里的部分处理函数：
- 延迟支付处理函数里的 sleep；点击后用“文字闸门”判断页面已响应就继续，最长不超过原时长。
- 并行模板匹配、路线页/牌库滚动“停稳即识别”、出击出牌按手牌变化继续。
- 替换处理函数时要同时替换各模块里按名字导入的引用和 `PAGE_HANDLERS` 列表里的函数对象，统一走 `_replace_function`。
- `_EXPECTED_RUN_SOURCE` / `_handlers_module` 通过读 `run()` 源码来确认结构没变。**修改 `ChaosMode.run` / `SortieMode.run` 时要同步 `speedup._gated_run`**，`TestSpeedup` 里的 `test_real_*_run_matches_gated_run` 会检查这一点。
- 其中几项与速度无关的修正（按钮文字被 OCR 切成两个框、国际服 BOSS 页/休息区、分解存档确认框、零式系统法典卡片改为按存档储存上限 pt 判断）在源码里也已修好。补丁里保留同样的逻辑，是为了用 `speedup_patch/install_speedup.py` 装进未修改的官方版时同样生效；两边同时存在不冲突。

**`speedup_patch/`**：`install_speedup.py` 把本仓库的改动装进官方安装目录（默认 `D:\Program Files\ok-kes\data\apps\ok-kes\working`），需要先关闭 ok-kes，官方版自动更新后要重新安装。安装目录里的 `utils.py`、`utils_sortie.py`、`utils_chaos.py`、`ChaosMode.py`、`SortieMode.py` 和翻译文件与 v1.4.3 原版哈希（`BASE_SHA`）一致时整份替换，并复制 `speedup.py`、`utils_battle.py`、`battle_log.py`、`config_layout.py`；对不上（官方已更新）时只复制 `speedup.py`、`battle_log.py`，往原版 `ChaosMode`/`SortieMode` 插入加速补丁调用。另给 `src/config.py` 追加 OpenVINO f32 补丁。**改了这些被整份替换的文件后，仓库要先合并对应的上游版本，再更新 `BASE_SHA`。**

## 仓库内的代理技能

`.agents/skills/` 里有面向 Codex 的技能说明，涉及相关工作时可以参考：
- `ok-script-tasks`、`ok-script-codegen`：ok-script 任务 API 和写法，比如优先用 `wait_*`；`sleep`/`next_frame` 会清掉当前帧。
- `ok-script-i18n`：同步并编译 `.po`/`.mo`，辅助脚本是 `scripts/task_i18n_helper.py`。
- `deploy`：打版本标签发布。提交信息的语言要跟最近一条非合并提交一致；不要改动或删除已有标签。
