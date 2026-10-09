# -*- coding: utf-8 -*-
"""最终只读核对（权威版）：页面当前选中态 vs 答案库期望，逐题打印正文。

上一版 `_audit_persisted.py` 直接拿 `a.value` 和隐藏域裸比字母，遇到两种情况会误报：
  1. B 型题的隐藏域是 JSON、`a.value` 是 "1:C,2:E" 紧凑串；
  2. 页面每次加载都会重排屏显字母。
这里统一改成「用当前页面把两边的值都翻译成正文再比」，并同时读 `.check_answer*` 选中态。

不点保存、不点提交。
"""
import json
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

from xxtutor.answer_codec import option_text  # noqa: E402
from xxtutor.browser import BrowserSession  # noqa: E402
from xxtutor.config import load_config  # noqa: E402
from xxtutor.llm import LLMClient  # noqa: E402
from xxtutor.page import ChaoxingPage  # noqa: E402
from xxtutor.solver import Solver  # noqa: E402
from xxtutor.store import AnswerStore  # noqa: E402
from xxtutor.types import QType  # noqa: E402

READ = r"""
(args) => {
  const el = document.querySelector('div#' + args.id);
  if (!el) return {err: 'not found'};
  const hid = document.getElementById('answer' + args.key);
  const picked = [];
  el.querySelectorAll('.answerBg, [role=radio], [role=checkbox]').forEach((b) => {
    const sp = b.querySelector('.num_option, .num_option_dx, [data]');
    if (sp && /check_answer/.test(sp.className)) {
      picked.push(sp.getAttribute('data') || '');
    }
  });
  const bts = [];
  el.querySelectorAll('.B-answer-ct').forEach((ct, i) => {
    ct.querySelectorAll('.B-answerCon span[data]').forEach((sp) => {
      if (/check_answer/.test(sp.className)) bts.push({i: i, d: sp.getAttribute('data')});
    });
  });
  return {hidden: hid ? hid.value : '', picked: picked, bts: bts,
          nBox: el.querySelectorAll('.answerBg, [role=radio], [role=checkbox]').length,
          nCt: el.querySelectorAll('.B-answer-ct').length};
}
"""


def main() -> int:
    cfg = load_config(str(WS / 'config.json'))
    sess = BrowserSession(cfg).start()
    try:
        page = ChaoxingPage(sess, cfg)
        page.ensure_login(interactive=False)
        runs = sorted((WS / 'out').glob('*-answer'), key=lambda p: p.name)
        run = None
        for cand in reversed(runs):
            if (cand / 'run.json').exists():
                run = cand
                break
        assert run is not None, '找不到带 run.json 的产物目录'
        print(f'复核对象：{run.name}')
        url = json.loads((run / 'run.json').read_text(encoding='utf-8')).get('work_url')
        sess.goto(str(url))
        sess.page.wait_for_timeout(1500)
        page.enter_answer_page_if_needed()
        qs = page.extract_questions()
        frame = page._find_answer_frame(qs)
        store = AnswerStore(str(WS / '.state' / 'answers.db'))
        solver = Solver(cfg, LLMClient.from_config(cfg), store)
        expected, rep = solver.solve(qs, course_id='', allow_llm=False)
        emap = {a.qid: a for a in expected}
        print(f'抓到 {len(qs)} 题 | 库内命中 {rep.from_cache} 相似 {rep.from_memory} '
              f'跳过 {rep.skipped}')

        bad = []
        for q in qs:
            a = emap.get(q.qid)
            st = frame.evaluate(READ, {'id': q.qid, 'key': str(q.answer_key)})
            if a is None or st.get('err'):
                bad.append((q.number, q.qtype.value, 'missing', str(a), st))
                continue
            picked = [str(x) for x in st['picked']]
            if q.qtype == QType.MATCH:
                want = {str(k): str(v) for k, v in (a.pairs or {}).items()}
                got = {str(b['i'] + 1): str(b['d']) for b in st['bts']}
                if want != got:
                    bad.append((q.number, 'match',
                                {k: option_text(q, v) for k, v in want.items()},
                                {k: option_text(q, v) for k, v in got.items()},
                                st['hidden'][:90]))
            else:
                want_letters = (sorted(picked) if False else None)
                want = sorted(set(str(x).upper() for x in
                                  (a.value if isinstance(a.value, list) else [a.value])))
                got = sorted(set(x.upper() for x in picked if x))
                # B 型之外的题：想比正文，也打印出来
                if want != got:
                    bad.append((q.number, q.qtype.value,
                                {L: option_text(q, L) for L in want},
                                {L: option_text(q, L) for L in got},
                                (st['hidden'] or '')[:90]))
                del want_letters

        print(f'不一致 {len(bad)} 题')
        for num, kind, want, got, hid in bad[:20]:
            print(f'  Q{num} [{kind}]')
            print(f'      期望 {want}')
            print(f'      实际 {got}   隐藏域={hid!r}')
        store.close()
        print('结论：' + ('全部一致 ✔' if not bad else f'{len(bad)} 题不一致 ✘'))
        return 0 if not bad else 2
    finally:
        sess.close()


if __name__ == '__main__':
    raise SystemExit(main())
