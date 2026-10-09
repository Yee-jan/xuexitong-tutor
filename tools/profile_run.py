# -*- coding: utf-8 -*-
"""分阶段计时 Profiler：找出 `answer` 每一步真正花掉的时间。

用法：
  python tools\\profile_run.py [answers.json]

只读（不点保存、不点提交、不改页面提交开关），把流程拆成
  浏览器启动 / 登录检查 / 开答题页 / 抓题 / 答案库查询 / 回填 / 逐题核对
并额外统计：
  - 固定 wait_for_timeout 的累计耗时
  - Playwright 每次 evaluate / locator 调用的往返耗时（按题聚合）
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

from xxtutor.answerfile import import_answers            # noqa: E402
from xxtutor.browser import BrowserSession               # noqa: E402
from xxtutor.config import load_config                   # noqa: E402
from xxtutor.page import ChaoxingPage                    # noqa: E402

STEPS = []


class step:
    """with step('抓题'): ...  —— 累计同名步骤的总耗时与调用次数。"""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        STEPS.append((self.name, time.perf_counter() - self.t0))
        return False


def report(steps):
    agg = {}
    order = []
    for name, dt in steps:
        if name not in agg:
            agg[name] = [0.0, 0]
            order.append(name)
        agg[name][0] += dt
        agg[name][1] += 1
    total = sum(dt for _, dt in steps)
    print('\n' + '=' * 62)
    print(f'{"阶段":<28}{"总耗时":>10}{"次数":>7}{"平均":>10}')
    print('-' * 62)
    for name in order:
        dt, n = agg[name]
        print(f'{name:<28}{dt:>9.2f}s{n:>7}{dt / max(n, 1) * 1000:>9.0f}ms')
    print('-' * 62)
    print(f'{"以上合计":<28}{total:>9.2f}s')
    print('=' * 62)
    return total


def main(argv):
    src = argv[1] if len(argv) > 1 and not argv[1].startswith('--') else None
    if src is None:
        cands = sorted((WS / 'out').glob('*/answers.json'),
                       key=lambda p: p.stat().st_mtime)
        src = str(cands[-1])
    print(f'用答案文件：{src}')

    cfg = load_config(str(WS / 'config.json'))
    print(f'config: channel={cfg.browser.channel} headless={cfg.browser.headless} '
          f'delay={cfg.answer.min_delay_ms}-{cfg.answer.max_delay_ms}ms')

    t_all = time.perf_counter()
    aq = ChaoxingPage.apply_answers

    def timed_apply(self, questions, answers, *, dry_run=False):
        inner = []
        real_fill = self._fill_one

        def timed_fill(frame, q, a):
            t0 = time.perf_counter()
            r = real_fill(frame, q, a)
            inner.append((q.number, time.perf_counter() - t0, r[0]))
            return r

        self._fill_one = timed_fill
        t0 = time.perf_counter()
        stats = aq(self, questions, answers, dry_run=dry_run)
        dt = time.perf_counter() - t0
        STEPS.append(('回填 apply_answers 总计', dt))
        self._fill_one = real_fill
        slow = sorted(inner, key=lambda x: -x[1])[:5]
        print('\n单题回填最慢的 5 题：')
        for num, sec, ok in slow:
            print(f'  Q{num:<4} {sec * 1000:>7.0f}ms  {"ok" if ok else "FAIL"}')
        if inner:
            print(f'  每题平均 {sum(x[1] for x in inner) / len(inner) * 1000:.0f}ms')
        return stats

    ChaoxingPage.apply_answers = timed_apply

    sess = None
    try:
        with step('浏览器启动'):
            sess = BrowserSession(cfg).start()
        page = ChaoxingPage(sess, cfg)

        with step('登录检查'):
            page.ensure_login(interactive=False)

        run_json = Path(src).parent / 'run.json'
        work_url = ''
        if run_json.exists():
            work_url = (json.loads(run_json.read_text(encoding='utf-8'))
                        .get('work_url') or '')
        reopened = False
        if work_url:
            # 旧 URL 上的 enc/standardEnc 是一次性令牌，服务端多半只回一张
            # 「作答状态异常！」的提示页。所以先花 ~1 秒问一句「这页能用吗」，
            # 能用才继续；不能用立刻退回课程/作业查找（跟 cli.py 同一套逻辑）。
            with step('重开答题页(校验)'):
                sess.goto(work_url)
                st = page.answer_page_state(timeout_s=5.0)
            if st['ready']:
                with step('确认答题页控件'):
                    page.enter_answer_page_if_needed(timeout_s=3.0)
                reopened = True
                print(f'  重开成功：questions={st["questions"]} start={st["start"]}')
            else:
                print(f'  !! 重开失败（{st.get("trouble") or "页面上没有题目"}），'
                      f'退回课程/作业查找')
        else:
            print('!! 没找到 work_url，退回课程/作业查找路径')

        if not reopened:
            with step('找课程'):
                course = page.find_course('检体诊断学')
            with step('开课程'):
                page.open_course(course)
            with step('找工作列表'):
                page.open_homework_tab()
                hw = page.find_homework('基本检查方法', page.list_homework())
            with step('开作业'):
                page.open_homework(hw)

        with step('抓题 extract_questions'):
            qs = page.extract_questions()
        print(f'\n抓到 {len(qs)} 道题')

        with step('读答案文件 import_answers'):
            got, skipped = import_answers(src, qs)
        print(f'答案文件命中 {len(got)}/{len(qs)} 题，未对上 {len(skipped)} 个键')

        answers = [got[q.qid] for q in qs if q.qid in got]
        with step('找答题 frame'):
            frame = page._find_answer_frame(qs)
        qs = list(qs)
        stats = timed_apply(page, qs, answers, dry_run=False)
        print('\n填答统计：', {k: v for k, v in stats.items() if k != 'details'})
        print('回填方式分布：')
        for k, v in Counter(str(d.get('how', ''))[:22]
                            for d in stats['details']).most_common():
            print(f'  {k or "(无)":<24} {v}')
    finally:
        if sess is not None:
            with step('关闭浏览器'):
                sess.close()

    report(STEPS)
    print(f'\n（未点保存、未点提交）  总墙钟 {time.perf_counter() - t_all:.1f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
