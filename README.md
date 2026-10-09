# 学习通作业自动答题框架（xxtutor）

抓题 → AI 作答 → 自动回填 → **只保存、不提交**（提交由你自己在浏览器里点）。

> ⚠️ **使用边界**：本项目不内置任何题库或标准答案，也**从不自动提交**作业。
> 请先读文末的[免责声明与使用边界](#9-免责声明与使用边界)。

面向章节测验 / 作业页，支持 **单选（A1/A2/A3 型）**、**多选（X 型）**、**配伍题（B 型）**、
填空、判断、简答，并内置**本地答案库**：同题/相似题直接复用，这是省 token 的主要手段。

---

## 平时怎么用（最短路径）

**第一次用**：装好 Python 3.9+ 后，双击项目里的 `setup.bat`（装 playwright + 下载
Chromium 内核，只需一次，约 150MB）。

**之后**：双击 `run.bat`，或者在任何 `cmd` / PowerShell 窗口里进到项目目录：

```bat
cd /d "解压出来的那个目录"
run.bat                 :: 不带参数就打印所有命令
```

日常就三条：

```bat
run.bat login                         :: 登录态失效了才需要，成功一次能管很久
run.bat courses                       :: 看看有哪些课
run.bat answer -c 高等数学 -w 第三章     :: 抓题 → 出答案 → 保存（不提交）
```

不想按课程名找，就直接给答题页地址（最稳）：

```bat
run.bat answer --url "https://mooc1.chaoxing.com/work/doWork?..."
```

结果在 `out\<时间戳>-answer\` 里，其中 `answers.md` 是给人看的（题目 + 选项 + 答案 + 来源 + 置信度），
`answers.json` 是给程序/题库用的。**最后一步的「提交」需要你自己在浏览器里点**——
框架只填答案并点「暂时保存」，不会替你交卷。

其他有用的：

```bat
run.bat bank                          :: 看本地题库攒了多少题、省了多少请求
run.bat doctor                        :: 环境自检（含模型连通性）
run.bat homework -c 高等数学           :: 先看看这门课有哪些作业
run.bat answer -c 高等数学 -w 第三章 --dry-run   :: 只出答案，一个字都不往页面写
```

> `run.bat` 只是帮你切目录 + 设好 UTF-8 编码，等价于
> `python -X utf8 -u -m xxtutor ...`。日志里中文乱码时用它准没错。
> 它会先检查依赖装好了没，没装会提醒你先跑 `setup.bat`。

---

## 0. 一次性准备

最省事：双击 `setup.bat`（自动装依赖 + 下载 Chromium 内核，约 150MB）。

手动装也行：

```powershell
python -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
python -m playwright install chromium
```

> 国内下载内核慢的话，先设镜像：
> `set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`

配置：把 `config.example.json` 复制成 `config.json`，填上自己的 key。**不填也能跑**——
所有命令都会退回用 `config.example.json` 的默认值，只是需要模型的题会标成未作答
（页面标准答案和本地答案库这两条零 token 通路照常工作）。

```powershell
copy config.example.json config.json
```

```jsonc
"llm": {
  "preset": "deepseek",                       // 也可 openai / dashscope / moonshot / zhipu / ollama
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "sk-..."                         // 填这里；清空它就会改用环境变量 XX_API_KEY
}
```

> 不想把 key 写进文件，就留空 `api_key` 并设环境变量 `XX_API_KEY`——
> **完全不想花钱、也不想填任何 key 的话走「外接大脑」路线（见 4.5 节）**，
> 把题目丢给任意 AI（豆包网页版、本机的 DSH 会话、本地模型都行）再贴回来即可。
> `config.json` 已在 `.gitignore` 里，key 不会被提交。只想让它从环境变量读，就把 `api_key` 清空并设置 `XX_API_KEY`。
> `python -m xxtutor doctor` 只会报告 key 的来源和长度，**不会回显 key 的任何片段**。
> 配置文件写成带 BOM 的 UTF-8（记事本另存为的默认行为）也能正常读。

环境变量优先级最高：`XX_API_KEY` / `XX_BASE_URL` / `XX_MODEL`。
用 Ollama 本地模型就不花一分钱：`"preset": "ollama"`。

先自检：

```powershell
python -m xxtutor doctor            # 看环境、配置、答案库
python -m xxtutor doctor --check-llm   # 顺便测一次模型接口连通（消耗几十 token）
```

---

## 1. 登录（扫码，推荐）

```powershell
python -m xxtutor qrlogin --wait 600
```

二维码会存到 `.state\qr\login-qr.png`，**用学习通 App 扫它**。登录态写进 `.state\browser`，
之后所有命令都复用这个档案，不用重复登录。

> 想在有头浏览器里手动登录也行：`python -m xxtutor login`
> 想在别的机器上看这张二维码，直接把 `.state\qr\login-qr.png` 打开即可。

---

## 2. 找课程

```powershell
python -m xxtutor courses
```

输出形如：

```
[1] 高等数学                courseId=12345678   classId=87654321
[2] 大学英语                courseId=23456789   classId=98765432
```

课程名和 courseId 都能用于下一步。

---

## 3. 找作业

```powershell
python -m xxtutor homework -c 高等数学
```

会列出该课程下的作业/测验名称与链接。拿到作业名，或者不想按名字匹配时直接用 URL。

---

## 4. 开跑（抓题 → AI 作答 → 回填 → 保存）

按课程 + 作业名（模糊匹配）：

```powershell
python -m xxtutor answer -c 高等数学 -w 第三章
```

直接给 URL（最稳，跳过课程/作业导航）：

```powershell
python -m xxtutor answer --url "https://mooc1.chaoxing.com/work/doWork?..." -c 高等数学
```

> ⚠️ **URL 一定要用引号包起来**。答题页 URL 里通常带 `&`（`courseId=..&classId=..&enc=..`），
> 在 `cmd` 里不引号会被当成命令分隔符，URL 被截断（在 PowerShell 里同样建议加引号）。
> 直接从浏览器地址栏复制整段即可，框架会自动取 `courseId/workId/enc` 这些参数。

### 常用开关

| 参数 | 作用 |
| --- | --- |
| `--dry-run` | 只算答案不点页面，先看看 AI 答得对不对 |
| `-i` / `--interactive` | 逐题人工核对，确认后才回填（**第一次跑某门课强烈建议加**） |
| `--from-file out/xxx/questions.json` | 离线模式：不连浏览器，直接对已抓下来的题作答 |
| `--solve-only` | 只求解、不碰页面（没配 key 也能用来验证流程） |
| `--no-confirm` | 提交时不弹确认（配合 `submit: true` 用） |
| `--headless` / `--headed` | 覆盖配置里的有头/无头 |

**没配 API Key 也能跑**：脚本不会退出，只会用「页面标准答案 + 本地答案库」作答，
真正需要模型的题标成未作答。想让它问模型，就设 `XX_API_KEY` 或填 `config.json` 的 `llm.api_key`。

**如果打开的是「已提交」的作业**，会落到复习页（`mooc-ans/mooc2/work/view`）。
那一页没有可作答的控件，脚本会认出这一点并跳过回填，只把页面上印着的标准答案采下来入库，打印：

```
这是「已提交答卷」的复习页：页面上没有可作答的控件，跳过回填。
页面印着的标准答案已随抓题一起入库（15 题，零 token）。
```

> ⚠️ **别把「未提交的新版 doWork 页」误认成复习页**：新版 doWork 页的选项不是原生
> `input[type=radio]`，而是 `<div class="answerBg" role="radio" onclick="addChoice(this)">`。
> 若判据只看原生控件，会把 73 道题全判成「无可作答控件」而跳过回填。
> 判据要认 `has_choice_ui`（抽取端发现 ARIA 选项盒时置 1）。
> 详见 `troubleshooting.md` 的「ARIA 页面千万别自己往隐藏域写字符串」。

**默认不会提交**。跑完它会打印：

```
提交开关未打开（默认就是这样）。
答案已填入页面并保存，请你在浏览器里核对后手动点提交。
```

有头模式下会停住等你按回车，方便你直接在页面里检查。

### 产物

每次运行在 `out/<时间戳>-<标签>/` 下生成：

- `questions.json` —— 抓到的题目（含题型、选项、配伍小题）
- `answers.json` / `answers.md` —— AI 给出的答案与置信度，可直接阅读
- `page.html` / `page.png` —— 当时的页面快照，选择器失效时用它排查
- `answers.template.json` / `answers.template.md` —— 给「外接大脑」用的空白答卷（见下一节）

---

## 4.5 外接大脑：不用 API Key，把任意 AI 当答题引擎

`answer` 不发模型请求也能出答案：把题目文件丢给**任意 AI**（豆包 / Kimi / ChatGPT 网页版、
DSH 里的模型、本地 Ollama……），把 AI 回的答案存成文件再喂回来。这条路上 `llm.py` 完全不参与，
**不需要 API Key、不产生 token 费用**，换哪个模型都零成本。

### 三步走

```powershell
# 第 1 步：抓题（会同时写出空白答卷模板），token 花费 0
python -m xxtutor answer -c 高等数学 -w 第一章 --dry-run --no-llm

# 第 2 步：把 out\<那次运行>\questions.json 拖进任意 AI 对话框，附一句：
#   「逐题作答，只输出 JSON：{"qid": 答案}。单选给 "A"，多选给 ["A","C"]，
#     判断给 true/false，B 型配伍给 {"1":"D","2":"A"}。不要解释。」
#   懒得写 qid 也可以直接按题号：{"1":"A","2":["B","D"],"3":{"1":"D","2":"A"}}
#   把 AI 的输出存成 answers-ai.json（或直接编辑 answers.template.json）

# 第 3 步：带上答案回填（仍然要连着浏览器，因为要往页面里填）
python -m xxtutor answer -c 高等数学 -w 第一章 --answers out\<那次运行>\answers-ai.json
```

也可以完全跳过第 2 步的 AI：**自己填** `answers.template.json`（哪个题不会就留空），
或者用 `-i` 在终端里逐题核对。想要一次填完所有备忘，`answers.template.md` 更适合阅读/粘贴。

### 答案文件认哪些写法

| 写法 | 例子 |
| --- | --- |
| 纯 qid 映射 | `{"q1": "A", "q3": ["A","C"]}` |
| 按题号 | `{"1": "A", "3": ["A","C"]}` |
| 按题干 | `{"心脏的正常起搏点是？": "A"}` |
| 按选项正文 | `{"q1": "窦房结"}` |
| 本框架的 `{"items": [...]}` | `{"items": [{"qid": "q1", "answer": "A"}]}`（键名也认 `questions` / `qs` / `list`） |
| `answers.md` 可读版 | `### 1. 心脏的正常起搏点是？` + `**答案**：A`（AI 直接回 markdown 也能吃） |

编码 BOM、全角/半角、大小写、`1:B,2:A` 这种紧凑配伍串都会自动归一化；对不上的键会打印成
`skipped` 警告而**不会静默丢弃**。多个 `--answers` 可以叠，后面的覆盖前面的。

### 相关参数

| 参数 | 作用 |
| --- | --- |
| `--answers FILE` | 导入外部答案文件（可重复） |
| `--answers-from RUN_DIR` | 读某次已完成运行目录里的 `answers.json` |
| `--from-dir DIR` | 复用上次抓的题目，不重新抓；该目录记有答题页 URL 时会**自动重开那一页**直接回填 |
| `--reopen RUN_DIR` | 提速：直接用某次运行留下的答题页 URL 打开作业，跳过「找课程 → 开课程 → 找作业」（也可直接给 http URL） |
| `--to-url URL` | 回填目标页。配合 `--from-dir` 时**唯一**不用重走「进课程→开作业」的路子 |
| `--no-fill` | 完全不打开浏览器（配合 `--from-dir` 纯离线攒题库） |
| `--no-llm` | 只用「页面答案 + 本地题库 + 外部答案」，一次模型请求都不发 |
| `--dry-run` | 只抓题出答案，不往页面填 |

**优先级**：`--answers` 给的答案 > 页面标准答案 > 本地答案库 > 模型。

### 复用已抓题目直接回填（跳过重新进课程）

作业页 URL 你自己手里有的时候，这是最快的一条路——不重走「进课程 → 开作业」：

```powershell
python -m xxtutor answer --from-dir out\20261001-101500-answer `
    --answers answers-ai.json --to-url "https://mooc1-1.chaoxing.com/mooc-ans/mooc2/work/doWork?..."
```

**URL 不用自己找**：每次一进答题页就会把 URL 写进该次运行的 `run.json`、回填后还会写进
`fill-report.json`，所以下面这条就够（实测 138.5s → 55.1s → 24s）：

```powershell
python -m xxtutor answer --from-dir out\20261001-101500-answer
```

`--reopen <目录或URL>` 可显式指定要重开哪一页；`read_work_url()` 依次看
`fill-report.json` / `run.json` / `answers.json`，再退到 `answers.md`、`page.html` 里正则找
`/mooc-ans/mooc2/work/` 链接。

⚠ **别把 `--reopen` 当主力提速手段**：doWork URL 里的 `enc` / `standardEnc` 是**一次性
令牌**，换个会话重放会拿到一张 1142 字节的「作答状态异常！」页。改 URL（`answerId=0`、
去掉令牌）只会变成 `403` 或「章节任务点未达标」。所以现在的实现是
**快速失败 + 自动退回**：`answer_page_state()` 一次问完「有没有答题控件 / 有没有异常提示」，
判不可用就打印 `⚠ --reopen 打开的页面不可用（作答状态异常），退回按课程/作业名查找`
并继续走 `-c 课程名 -w 作业名` 的正常查找（实测这一步从 5.86s 降到 0.44s，而不是白等 9.5s）。

`--from-dir` 不带 `--to-url`/`-c` 也不给 `--reopen`、又没加 `--no-fill` 时会**直接报错退出**，
而不是静默地不填就走人（曾经就是静默的，答案一个字都没进页面）。

### 只想攒题库、不想填页面

`--from-file` / `--from-dir` 时不打开浏览器，跑完会打印「离线模式：没有打开浏览器，
因此不做回填」并把题目、模板、答案留在 `out\<运行目录>\`。适合批量刷答案库：

```powershell
python -m xxtutor answer --from-dir out\20261001-101500-answer --answers answers-ai.json
```

---

## 4.6 随包工具（`tools\`，全是只读或可回滚的）

| 脚本 | 干什么 |
| --- | --- |
| `python tools\profile_run.py [answers.json]` | **分阶段计时**：浏览器启动/登录/导航/抓题/回填各花多少秒。嫌慢先跑它，别猜 |
| `python tools\probe_work_url.py [运行目录]` | 把 `work_url` 拆成 6 种变体逐个打开，看哪种能重开（`enc` 一次性令牌的取证工具） |
| `python tools\verify_fill.py [--reload]` | **真填一遍**并逐题核对「期望字母 vs 页面隐藏域」，**不保存不提交**；`--reload` 会重开页面再核一次 |
| `python tools\audit_persisted.py` | 纯只读复核（自动挑最新带 `run.json` 的产物目录），把服务端已保存的值与答案库逐题比 |
| `python tools\check_semantics.py` | 跨运行核对「答案指向的选项正文」有没有变（字母坐标会漂，正文不会） |
| `python tools\migrate_store_coords.py [--apply]` | 把答案库里的历史裸字母迁移成选项正文（`--apply` 前自动备份 `answers.db.bak-<时间戳>`） |
| `python tools\score_vs_official.py [我方] [官方]` | **作业提交之后**用：把复习页印的标准答案与我们填的答案逐题比对，给出正确率与错题明细（不传参就取 `out\` 下最新两个产物） |

⚠ 这些脚本都用 `Path(__file__).resolve().parent.parent` 定位工作区根，所以放到
`scripts\tools\` 里也能跑。`profile_run.py` 默认挑 `out\*\answers.json` 里最新的那个，
**会误取 `--solve-only` 产物**（那种只有 2 个答案），对比时要显式传运行目录。

---

## 5. 提交

**由你手动完成**：在打开的浏览器页面里核对答案 → 点「提交」。
想让它自动提交，就把 `config.json` 的 `answer.submit` 改成 `true`（这时候 `answer.confirm_before_submit` 决定是否再问一次）。

---

## 6. 省 token 的机制（为什么便宜）

1. **页面标准答案（零 token，最优先）**：已经提交过的作业，学习通会把正确答案印在复习页
   （`mooc-ans/mooc2/work/view`）的 `.rightAnswerContent` 上。抓题时顺手带下来，直接当作答案
   写回题库，一次模型调用都不发。优先级：人工预置 > 页面答案 > 本地答案库 > 模型。
   开关：`solver.use_page_answers`（默认 true，设为 false 就完全忽略页面答案）、
   `solver.save_page_answers`（默认 true，把页面答案写进题库供以后复用）。
2. **本地答案库优先**：`.state/answers.db` 里同题/相似题直接命中，一次模型调用都不发。
   相似用题干 3-gram Jaccard，阈值 `store.similarity_threshold`（默认 0.85：同题≈1.0，加噪音≈0.68）。
   选项不同会被「选项签名」挡下，避免张冠李戴。
3. **批量作答**：`llm.batch_size`（默认 6）道题塞进一次请求，system prompt 只发一次。
4. **题目瘦身**：只发题干 + 选项正文，不发 HTML；配伍题只发一组备选 + 小题列表。
5. **熔断**：单次作业超过 `llm.max_tokens_per_assignment`（默认 60000）直接停下报错。
6. **人工修正会写回题库**：`-i` 模式里改过的答案以 `manual` 来源入库，下次直接复用。
7. **外接大脑零成本**：用 `--answers` 从外部 AI 导入答案时，框架一次模型请求都不发
   （`外部答案 N（覆盖 M）` 会出现在汇总行里）。这条路对方是免费 AI 也不影响本框架。
   详见 [4.5 外接大脑](#45-外接大脑不用-api-key把任意-ai-当答题引擎)。

查看题库统计 / 导出：

```powershell
python -m xxtutor bank                    # 打印统计（题数、复用次数、token、题型分布）
python -m xxtutor bank --export out/bank.json
```

---

## 7. 出问题怎么办

| 症状 | 处理 |
| --- | --- |
| 报「没抓到任何题目」 | 加 `--headed` 看是不是还没进答题页；页面快照在 `out/*/page.html` |
| 报「第 N 题的容器找不到了」 | 学习通改版了。先跑 `python -m xxtutor dump-dom --url <答题页>` 存下真实 DOM，再对照 `xxtutor/extract_js.py` 的容器规则 |
| 二维码扫了没反应 | 二维码有时效，`--refresh 45` 会自动刷新；`.state\qr\login-qr.png` 是最新一张 |
| 打开的是「已提交」作业 | 会落到复习页，只能看答案不能作答。脚本仍会把页面印着的标准答案抓下来入库（零 token） |
| 配伍题（B 型题）小题抓错 | 用 `tests\e2e_real_review.py` 跑一遍真实复习页夹具，能复现就对照 `xxtutor/extract_js.py` 的 `extractMatchItems` |
| 模型返回解析失败 | 调小 `llm.batch_size`（一次少答几道），或换个更强的模型 |
| 想从头再来 | 删 `.state\browser`（重登）或 `.state\answers.db`（清空题库） |

自测（不碰真实站点）：

```powershell
python selftest.py                   # 85 项离线单测（题型/解析/题库/求解/CLI）
python tests\e2e_offline.py          # 真实 Chromium 跑仿真答题页（含配伍题），31 项
python tests\e2e_variants.py         # 多形态页面（新版/表格/配伍）回归，28 项
python tests\e2e_real_review.py      # 真实「已提交答卷复习页」结构回归，16 项
python tests\e2e_external_brain.py   # 外接大脑（答案文件导入/合并/离线守卫），38 项
python tests\e2e_external_brain_fill.py  # 外接大脑真回填（CLI -> 打开页面 -> 填进去），16 项
python tests\e2e_aria_page.py        # 新版 ARIA 答题页（role=radio + 隐藏域）回归，34 项
python tests\e2e_store_coords.py     # 答案库「存选项正文、不存字母坐标」回归，20 项
python tests\e2e_course_tabs.py      # 课程页「我学的课」标签切换时序（处理器晚绑定）回归，16 项
```

合计 284 项。其中 `tests\e2e_aria_page.py` 用 `tests/fixtures/answer_page_aria.html`
锁住新版 `mooc-ans /work/doWork` 页最容易翻车的几件事：**选项字母必须读 `data` 属性
而不是屏显文字**（夹具故意让屏显与 `data` 错位）、多选题要累积成 `"ABE"` 而不是只剩
最后一个字母、B 型题隐藏域要是 `[{"name":1,"content":"C"},…]` 这种 JSON 数组、重复
回填幂等（不能把已选项 toggle 掉）、**页面残留的多余选中项必须被取消**（只勾不取消会
让隐藏域一直多出字母，真机 Q68/Q69 就是这样从 `AC` 变成 `ABCE`）、点「暂时保存」绝不
触发提交。

其中 `tests\e2e_store_coords.py` 不需要浏览器：它用同一道题的两套字母坐标（`fp` 完全相同）
验证答案库里存的是**选项正文**，出库时再按当前题面换算回字母 —— 真机上新版 doWork 页
每次加载都会重排屏显字母，库里若存裸字母，第二轮起答案就会指向别的选项，而日志还是
「缓存命中 73/73、复用率 100%」。

其中 `tests\e2e_course_tabs.py` 用 `tests/fixtures/course_page_tabs.html` 锁住课程页的
时序坑：**「我学的课」标签元素比它自己的点击处理器先出现**（夹具里处理器晚 1.2 秒绑定）。
真站实测 1.5s 点一下「看起来成功」但页面毫无反应（`current` 留在「我教的课」、卡片一张
不出），2.6s 再点一次才真的切过去。所以 `_switch_to_student_courses` 必须「点完确认生效
（标签拿到 `current` 或卡片已出现），没生效就再点」—— 只点一次的实现在这份夹具上必然失败。

其中 `tests\e2e_real_review.py` 用的快照 `tests/fixtures/real_work_review.html`
**结构取自真实的「已提交答卷复习页」，但题面文本已全部替换成合成占位串**（题干、选项、
配伍小题、备选答案都换成了 `合成占位001…` 这类字符串），仓库里不含任何真实试题与答案。
它锁住三件事：14 道单选 + 1 道 B 型配伍题都能抓到、配伍题的小题不会被题干/备选污染、
页面印着的标准答案 15/15 都能解析出来（这三条在真实页面上都踩过坑）。

**强制验证"模型真的会答"**（绕过页面答案和答案库两条零 token 捷径）：

```powershell
$env:XX_API_KEY = "sk-..."      # 这个测试配置故意不带 key，从环境变量读
python -m xxtutor --config .state\llm-test-config.json solve `
  -f tests\fixtures\questions_no_answers.json --course-id llmcheck --json
```

`.state/llm-test-config.json` 里 `use_page_answers=false`、`similarity_threshold=1.01`、`store.path` 指向临时库，
所以每题都必然发给模型，并且**不会污染真实答案库**。实测 3 题 1 次请求 488 token，
答案与页面标准答案一致（D / E / E）。

---

## 8. 子命令一览

```
doctor      环境/配置/答案库自检
login       有头浏览器手动登录
qrlogin     无头抓二维码扫码登录
courses     列出课程
homework    列出某课程的作业
answer      主流程：抓题 -> AI -> 回填 -> 保存（默认不提交）
solve       只求解：从 questions.json 出答案，不碰浏览器
bank        答案库统计 / --export 导出 JSON
dump-dom    存某页 HTML+截图，排查选择器
```

---

## 9. 免责声明与使用边界

**这不是超星/学习通官方项目**，与超星集团及其关联公司无任何关系。

**它做什么、不做什么**

- 它只做四件事：把页面上的题目**读出来**、把答案**算出来**、把答案**填回页面**、点**「暂时保存」**。
- 它**从不自动提交**。默认配置 `answer.submit = false`，而 `--submit` 需要你在命令行显式写出；
  即使写了，也会先弹确认。提交按钮必须由你自己在浏览器里点。
- 它**不内置任何题库或标准答案**。答案只有三个来源：① 你自己提供的答案文件（`--answers`）；
  ② 你自己的模型接口；③ 学习通**已经开放给你账号**的复习页标准答案（已提交作业才会显示）。

**你需要自己承担的部分**

- **学术规范**：请遵守你所在学校的学术诚信规定和超星用户协议。用本工具代替自己完成作业
  可能构成违纪，**后果由使用者自负**。作者不支持、也不鼓励任何形式的作弊。
- **平台风控**：自动化访问可能触发验证码、限流乃至账号封禁。学习通的页面结构与风控策略
  随时可能变化，本项目**不保证任何时刻可用**。
- **答案正确性**：模型给出的答案是**参考**，不是权威结论。实测在《检体诊断学》一份 73 题的
  作业上，与官方标准答案一致率约 **90%**。请务必自己核对后再决定是否提交。
- **凭据安全**：`config.json` 里的 API Key、`.state/` 下的登录态（浏览器用户目录）都在你本机，
  `.gitignore` 已排除它们。**不要**把它们提交到任何仓库。

**适用场景**：个人学习自查、浏览器自动化技术研究、以及无障碍辅助（例如行动不便者难以
长时间操作网页）。请在这些边界内使用。

---

## 10. 许可

MIT License，见 [LICENSE](LICENSE)。
