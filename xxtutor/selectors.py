"""学习通页面选择器集合。

学习通前端改版很频繁（超星、泛雅、新版 i.chaoxing.com 三套皮肤并存），
所以每个语义都给出**按优先级排列的候选选择器**，运行时逐个尝试。
如果全部失效，用 `python -m xxtutor dump-dom` 存下 DOM 再回来这里补选择器，
业务代码无需改动。
"""

from __future__ import annotations

from typing import List

# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
LOGIN_LOGGED_IN: List[str] = [
    "#userInfo", ".user-info", ".personal-info", ".header-user",
    ".zl_login", ".main-content .user",
    "a[href*='passport2.chaoxing.com/logout']",
    "a[href*='passport2/logout']",
    # 新版 i.chaoxing.com 的登录后特征：头像/昵称/退出入口
    ".avatar", ".user-avatar", ".head-img", "#headImg",
    ".user-name", ".nickname", "#nickName", ".userInfo",
    "a[href*='logout']", "[class*='logout']",
    ".Mycourse", "li.course", ".course-list",
]

#: URL 里出现这些片段，基本可以判定已经离开登录页、进入登录后的空间
LOGGED_IN_URL_HINTS: List[str] = [
    "i.chaoxing.com", "i.mooc.chaoxing.com", "/space/",
    "/mycourse", "mooc1.chaoxing.com/visit",
]

LOGIN_PHONE_TAB: List[str] = [
    "#phoneLogin", ".login-tab li:has-text('手机号')",
    "a:has-text('手机号登录')", "li:has-text('手机号')",
]
LOGIN_USERNAME_INPUT: List[str] = [
    "#phone", "input[name='phone']", "input#uname", "input[name='uname']",
    "input[placeholder*='手机']", "input[placeholder*='账号']",
]
LOGIN_PASSWORD_INPUT: List[str] = [
    "#pwd", "input[name='pwd']", "input[type='password']",
    "input[placeholder*='密码']",
]
LOGIN_SUBMIT: List[str] = [
    "#loginBtn", "a#loginBtn", "button:has-text('登录')",
    "input[type='submit']", ".login-btn", "a:has-text('登 录')",
]
LOGIN_QRCODE: List[str] = ["#qrcode", ".qrcode-img", "img.qrcode", "canvas#qrcodeImg"]

# --------------------------------------------------------------------------- #
# 个人空间 / 课程列表
# --------------------------------------------------------------------------- #
HOME_COURSE_CARDS: List[str] = [
    # 实测（2026-09，真实账号）：个人空间把课程放在 iframe
    # `#frame_content` -> https://mooc1-1.chaoxing.com/visit/interaction?s=<sid>
    # 卡片是 `ul#courseList > li.course`，另有 li.teacher-folder / li.new-folder 等非课程项。
    "#courseList > li.course",
    "#courseList li.course",
    "li.course",
    ".course-list li.course",
    ".course-info",
    ".course-list li",
    ".courseCard",
    ".course-card",
    "div.Mycourse",
]
# 「我教的课 / 我学的课」切换（coursetype 0/1）。学生必须切到「我学的课」才有课。
COURSE_TYPE_TABS: List[str] = [
    ".course-tab .tab-item",
    ".tab-item",
]
HOME_COURSE_LINK: List[str] = [
    "a[href*='stucoursemiddle']",
    "a[href*='studentcourse']",
    "a[href*='course/phone/course']",
    "a[href*='/course/']",
]
HOME_COURSE_NAME: List[str] = [".course-name", "h3 .course-name", "h3 a", ".title", ".name"]
# 课程列表 iframe（个人空间把课程塞在 frame 里）
COURSE_FRAME: List[str] = ["#frame_content", "iframe[name='frame_content']", "iframe"]

# --------------------------------------------------------------------------- #
# 课程内页
# --------------------------------------------------------------------------- #
COURSE_TABS_HOMEWORK: List[str] = [
    # 实测（2026-09 真实 mooc2-ans 课程页）：导航是
    # <li dataname="zy" pageheader="8"><a href="javascript:void(0);" title="作业"
    #    data-url="https://mooc1.chaoxing.com/mooc2/work/list">…作业…</a></li>
    # 地址在 data-url 上，href 是 javascript:void(0)，按 href 找必然失败。
    "a[data-url*='/work/']",
    "li[dataname='zy'] a",
    "a[data-url*='work/list']",
    "a[title='作业']",
    "a[href*='work/list']",
    "a[href*='job/list']",
    "[data-url*='work']",
]
COURSE_TABS_EXAM: List[str] = [
    "a[data-url*='exam']",
    "li[dataname='ks'] a",
    "a[title='考试']",
    "a:has-text('考试')",
    "li:has-text('考试')",
    "a[href*='exam']",
]

# 作业列表项（老师发布的作业）
HOMEWORK_ITEMS: List[str] = [
    "div.work-list li",
    "ul.work-list > li",
    ".ulDiv > ul > li",
    ".workList li",
    "div:has(> a:has-text('未交'))",
    "li:has-text('未交')",
    "li:has-text('待完成')",
]
HOMEWORK_TITLE: List[str] = [".title", "h3", ".work-name", "span.title", "a"]
HOMEWORK_STATUS: List[str] = [".status", ".work-status", ".state", "span:has-text('未交')"]
HOMEWORK_ENTRY_BTN: List[str] = [
    "a:has-text('做作业')",
    "a:has-text('开始答题')",
    "a:has-text('进入作业')",
    "a:has-text('未交')",
    "a:has-text('继续答题')",
    "a.btn",
]

# --------------------------------------------------------------------------- #
# 答题页
# --------------------------------------------------------------------------- #
# 整个答题页可能被塞进 iframe（老版 /work/doWork 不是，新版 /exam-ans/ 常在 iframe 内）
ANSWER_FRAME_HINTS: List[tuple] = [
    ("iframe#iframe", "作业/考试的答题 iframe"),
    ("iframe#frame_content", "泛雅内容 iframe"),
    ("iframe[id^='frame']", "泛雅 iframe"),
    ("iframe[name='iframe']", "老版 iframe"),
    ("iframe.tk-iframe", "题库/答题 iframe"),
]

# 题目容器（按优先级）
Q_ITEM: List[str] = [
    ".TiMu",
    "div.TiMu",
    ".question-item",
    ".mark_item",
    "div[id^='question']",
    ".ans-cc .TiMu",
    "[data-type][data-questionid]",
    ".zy_TItle",
]
Q_ITEM_FALLBACK: List[str] = ["li[id^='q_']", ".qtContent", ".subject", ".exam-question"]

# 抽取端（extract_js.py）把下面这些容器**合并**成一个候选集再按 index 编号，
# 所以回填端也必须用同一个合并选择器按同样的 index 取元素，两边顺序才对得上。
# 早先回填端只按 Q_ITEM 逐个选择器 nth(index)，页面上一旦混排两种形态，
# 第 5、6 题就取不到容器（实测报「容器找不到了」）。
Q_ITEM_ALL: str = (
    ".TiMu, .question-item, .mark_item, .exam-question, "
    "div[id^='question'], div[id^='q_'], li[id^='q_']"
)

# 题干
Q_STEM: List[str] = [
    ".Zy_TItle .clearfix",
    ".Zy_TItle",
    ".fl.clearfix",
    ".mark_name",
    ".question-name",
    ".stem",
    ".title",
]
Q_NUMBER: List[str] = [".Zy_TItle .fl", ".mark_num", ".num", ".fontLabel"]

# 题型标识（数字：0 单选 1 多选 2 填空 3 判断 4 简答；也可能直接写在中文里）
Q_TYPE_ATTR: str = "data-type"
Q_TYPE_TEXT: List[str] = ["span:has-text('单选题')", "span:has-text('多选题')",
                          "span:has-text('判断题')", "span:has-text('填空题')",
                          "span:has-text('简答题')", "span:has-text('论述题')"]

# 选项
Q_OPTIONS: List[str] = [
    "ul.Zy_ulTop > li",
    "ul.mark_letter > li",
    "li.clearfix",
    ".answerList li",
    ".option-item",
    "ul li:has(input[type='radio'])",
    "ul li:has(input[type='checkbox'])",
    "label.option",
]
Q_OPTION_INPUT: List[str] = ["input[type='radio']", "input[type='checkbox']", "input"]
# 新版答题页（mooc-ans /work/doWork，实测 2026-10）没有原生控件：选项是带 role 的
# div，点它走 onclick="addChoice(this)"，答案最终写进隐藏域 input#answer<qid>。
ARIA_OPTION_SEL: str = ".answerBg, [role=radio], [role=checkbox]"
ARIA_BTYPE_SEL: str = ".B-answer-ct"
ARIA_BTYPE_OPTION_SEL: str = ".B-answerCon span[data]"
ARIA_HIDDEN_ANSWER_TP: str = "input#answer{qid}"
Q_OPTION_LETTER: List[str] = ["i.fl", "span.fl", ".letter", "i", "span"]
Q_OPTION_TEXT: List[str] = ["a.fl", "a", ".option-text", "span", "label"]

# 填空 / 简答输入框
Q_TEXTAREA: List[str] = [
    "textarea", "input[type='text']", "div[contenteditable='true']",
    ".edui-body-container", "textarea.textarea",
]

# --------------------------------------------------------------------------- #
# 提交
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 暂存 / 提交
# --------------------------------------------------------------------------- #
SAVE_BTN: List[str] = [
    "a:has-text('暂时保存')",
    "a:has-text('保存')",
    "button:has-text('保存')",
    "#tempsave",
    "#saveBtn",
    "a[onclick*='save']",
    "input[value*='保存']",
]
SAVE_DONE_HINTS: List[str] = [
    "text=保存成功", "text=暂存成功", ".save-success", ".layui-layer-content:has-text('成功')",
]
SUBMIT_BTN: List[str] = [
    "a:has-text('提交')",
    "button:has-text('提交')",
    "#submitBtn",
    "#confirmBtn",
    "a[onclick*='submit']",
    "input[value*='提交']",
    ".btn-submit",
]
SUBMIT_CONFIRM_BTN: List[str] = [
    "a:has-text('确定')",
    "button:has-text('确定')",
    ".layui-layer-btn0",
    "#popok",
    "a:has-text('确认')",
    ".dialog-btn-ok",
]
SUBMIT_DONE_HINTS: List[str] = [
    "text=已提交", "text=提交成功", "text=已完成", ".submitted", ".finish-tip",
]

# 已经答过的标记（避免重复作答）
Q_ANSWERED_HINTS: List[str] = [
    "span:has-text('已作答')", ".answered", ".check_answer",
]

# --------------------------------------------------------------------------- #
# URL 规则
# --------------------------------------------------------------------------- #
URL_WORK_LIST = "/work/list"
URL_DO_WORK = "/work/doWork"
URL_EXAM_LIST = "/exam/test"
URL_EXAM_ANS = "/exam-ans/exam/test"
URL_KW_HOMEWORK = ("work", "homework", "job", "exam")
