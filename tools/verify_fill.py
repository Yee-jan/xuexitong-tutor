# -*- coding: utf-8 -*-
"""真实页面回填验证 + 逐题核对（默认不保存、不提交）。

用法：
  python .state\\_verify_fill.py [answers.json] [--reload]

流程与 `answer` 命令的回填段一致，把最后一步 save_progress() 换成逐题核对：
  - 每题「期望字母」 vs 「隐藏域实际值」（服务端保存时读的就是它）；
  - 多选比字母集合，B 型题比 JSON 每一项；
  - 加 --reload 会重新打开作业页再核对一次（能验出「页面没登记、保存会丢」）。
"""
import json
import sys
from collections import Counter
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

from xxtutor.answerfile import import_answers            # noqa: E402
from xxtutor.browser import BrowserSession               # noqa: E402
from xxtutor.config import load_config                   # noqa: E402
from xxtutor.page import ChaoxingPage                    # noqa: E402
from xxtutor.types import QType                          # noqa: E402

READHIDDEN = r"""
() => {
  const out = {};
  document.querySelectorAll('input[type=hidden][id^=answer]').forEach((i) => {
    if (/^answer\d+$/.test(i.id)) out[i.id.replace(/^answer/, '')] = i.value;
  });
  return out;
}
"""


def retry(fn, label, sess, n=6, delay=4000):
    last = None
    for i in range(n):
        try:
            r = fn()
            if r:
                return r
            last = 'empty'
        except Exception as e:  # noqa: BLE001
            last = f'{type(e).__name__}: {e}'
        print(f'  [{label}] 第 {i + 1} 次失败：{last}')
        try:
            sess.page.wait_for_timeout(delay)
        except Exception:
            pass
    raise SystemExit(f'{label} 连续失败：{last}')


def open_work(page, sess, cfg):
    """打开作业页并返回 (questions, frame)。"""
    page.ensure_login(interactive=False)
    course = retry(lambda: page.find_course('检体诊断学'), 'course', sess)
    page.open_course(course)

    def get_hw():
        page.open_homework_tab()
        return page.find_homework('基本检查方法', page.list_homework())

    hw = retry(get_hw, 'homework', sess)
    page.open_homework(hw)
    questions = retry(lambda: page.extract_questions(), 'extract', sess)
    return questions, page._find_answer_frame(questions)


def audit(page, frame, qs, got, tag=''):
    """把「期望答案」与「隐藏域实际值」逐题核对。"""
    hidden = frame.evaluate(READHIDDEN)
    bad = []
    multi = btype = single = 0
    for q in qs:
        a = got.get(q.qid)
        if a is None:
            continue
        key = str(q.answer_key or '')
        val = str(hidden.get(key, '') or '')
        if q.qtype == QType.MATCH:
            btype += 1
            want = {str(k): str(v).upper() for k, v in (a.pairs or {}).items()}
            try:
                parsed = {str(x.get('name')): str(x.get('content', '')).upper()
                          for x in json.loads(val)}
            except Exception:
                parsed = {}
            if not want or any(parsed.get(k) != v for k, v in want.items()):
                bad.append((q.number, 'match', want, val[:110]))
        elif q.qtype == QType.MULTIPLE:
            multi += 1
            want = {c for c in str(a.value).upper() if c.isalpha()}
            have = {c for c in val.upper() if c.isalpha()}
            if want != have:
                bad.append((q.number, 'multi', sorted(want), val))
        else:
            single += 1
            want = str(a.value).strip().upper()
            if want and want not in val.upper():
                bad.append((q.number, 'single', want, val))

    print(f'\n===== 核对{tag} =====')
    print(f'  单选题 {single} | 多选题 {multi} | B 型题 {btype}'
          f' | 隐藏域共 {len(hidden)} 个')
    print(f'  不一致 {len(bad)} 题')
    for num, kind, want, val in bad[:20]:
        print(f'    Q{num} [{kind}] 期望 {want} → 实际 {val!r}')
    return bad


def main(argv):
    reload_check = '--reload' in argv
    argv = [a for a in argv if not a.startswith('--')]
    src = argv[1] if len(argv) > 1 else str(
        WS / 'out' / '20261008-231200-answer' / 'answers.json')

    cfg = load_config(str(WS / 'config.json'))
    sess = BrowserSession(cfg).start()
    try:
        page = ChaoxingPage(sess, cfg)
        qs, frame = open_work(page, sess, cfg)
        print(f'\n抓到 {len(qs)} 道题')

        got, skipped = import_answers(src, qs)
        print(f'答案文件命中 {len(got)}/{len(qs)} 题，未对上 {len(skipped)} 个键')
        answers = [got[q.qid] for q in qs if q.qid in got]

        stats = page.apply_answers(qs, answers, dry_run=False)
        print('\n填答统计：', {k: v for k, v in stats.items() if k != 'details'})
        for d in [d for d in stats['details'] if d['status'] != 'ok'][:15]:
            print('  未成功：', json.dumps(d, ensure_ascii=False)[:220])

        bad1 = audit(page, frame, qs, got)

        print('\n回填方式分布：')
        for k, v in Counter(str(d.get('how', ''))[:16]
                            for d in stats['details']).items():
            print(f'  {k or "(无)":<22} {v}')

        if reload_check:
            print('\n===== 重载作业页后复核（证明页面真的登记了）=====')
            sess.page.wait_for_timeout(1200)
            qs2, frame2 = open_work(page, sess, cfg)
            print(f'重载后抓到 {len(qs2)} 道题')
            got2, _ = import_answers(src, qs2)
            audit(page, frame2, qs2, got2, tag='（重载后）')

        print('\n（本次未点保存、未点提交）')
        print(f'结论：{"全部一致" if not bad1 else f"{len(bad1)} 题不一致"}')
        return 0 if not bad1 else 2
    finally:
        sess.close()


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
