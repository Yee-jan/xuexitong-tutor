"""在学习通答题页里执行的 DOM 抽取脚本。

这是「抓题」的核心：不依赖任何第三方库，纯浏览器端 JS。
设计上做了三重兜底，降低学习通改版导致的失效概率：

    1. 题型优先读 `data-type` 属性，读不到再从题干文字里认「单选/多选/判断/填空/简答」；
    2. 题目容器优先 `.TiMu`，退化到「自己像题、子元素不像题」的通用判定；
    3. 选项优先从 `input[radio|checkbox]` 反查所属 li 的文本，
       退化到 `ul > li`，再退化到 `ul.Zy_ulTop > li` 这类学习通专有 class。

返回结构与 Python 侧 `Question` 一一对应，方便直接构造。
"""

from __future__ import annotations

# 注意：这段脚本用 page.evaluate 传入，必须是「单个表达式」，不要写成具名函数声明。
EXTRACT_JS = r"""
() => {
  const strip = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();

  const TYPE_TEXT = [
    [/配伍|匹配|连线/, 'match'],
    [/B\s*1?\s*型/i, 'match'],
    [/多选/, 'multiple'], [/X\s*型/i, 'multiple'],
    [/单选/, 'single'], [/A\s*[123]\s*型/i, 'single'],
    [/判断/, 'judge'],
    [/填空/, 'fill'], [/简答/, 'short'], [/论述/, 'short'], [/问答/, 'short'], [/名词解释/, 'short'],
  ];
  const typeFromText = (t) => {
    for (const [re, v] of TYPE_TEXT) { if (re.test(t || '')) return v; }
    return '';
  };
  // 学习通在题干开头会写题型标签（括号里、或「1. (A1型) ...」这种）。
  // 但用户题干正文里完全可能提到「B 型题」「X 型」这类词，直接对整段题干做正则
  // 会把普通题误判成配伍题（实测：题干「B 型组题目：...」被认成 match）。
  // 所以只做「标签位置 + 独立词」判定：标签必须由引号/括号/顿号等 bookend 包住，
  // 或者正好是一个独立的 type-词 再跟 型。
  // 允许字母/数字/「型」之间夹空格或全角空格 —— 真实复习页里写的是
  // 「15. (B 型题)」「二. B 型题（共1题）」，字母与「型」之间正好有一个空格，
  // 早期正则 [ABX]\s*[123]?\s*型 对 "B 型" 能过，但对 "B 1 型"/"B1 型" 就漏了。
  const CASE_RE = /(?:^|[\s(（\[【、,，:：|/])((?:[ABX]\s*[123]?|[A-Z])\s*型)(?=$|[\s)）\]】、,，:：|/])/i;
  const TYPE_LABEL_RE = /(?:^|[\s(（\[【、,，:：|/])((?:[ABX]\s*[123]?|[A-Z])|配伍|匹配|连线|多选|单选|判断|填空|简答)(?=$|[\s)）\]】、,，:：|/])/i;
  const QTYPE_WORD_RE = /(配伍|匹配|连线|多选|单选|判断|填空|简答|论述|问答|名词解释)/;
  const typeFromHint = (t) => {
    const s = strip(t || '');
    if (!s) return '';
    const head = s.slice(0, 60);
    const mc = head.match(CASE_RE);
    if (mc) {
      const tok = mc[1].replace(/\s+/g, '').toUpperCase();      if (['A1', 'A2', 'A3'].includes(tok)) return 'single';
      if (['X1', 'X2', 'X3'].includes(tok)) return 'multiple';
      if (['B', 'B1', 'B2', 'B3'].includes(tok)) return 'match';
      // 「A 型」「X 型」这种不带数字的：A 型=单选，X 型=多选
      if (tok === 'A' || tok === 'A型') return 'single';
      if (tok === 'X' || tok === 'X型') return 'multiple';
      return '';
    }
    const mw = head.match(QTYPE_WORD_RE);
    if (mw) {
      const t0 = typeFromText(mw[1]);
      if (t0) return t0;
    }
    const ml = head.match(TYPE_LABEL_RE);
    if (ml) {
      const t0 = typeFromText(ml[1]);
      if (t0) return t0;
    }
    return '';
  };

  const normalizeType = (raw) => {
    const s = String(raw == null ? '' : raw).trim();
    if (['0', '1', '2', '3', '4'].includes(s)) {
      return { '0': 'single', '1': 'multiple', '2': 'fill', '3': 'judge', '4': 'short' }[s];
    }
    // 学习通部分题库把配伍题（B 型题）编码为 6 / 7
    if (['6', '7'].includes(s)) return 'match';
    const m = typeFromText(s);
    if (m) return m;
    const low = s.toLowerCase();
    if (['single', 'multiple', 'judge', 'fill', 'short', 'match'].includes(low)) return low;
    return '';
  };
  // 题型标记可能不在题目容器上，而在它的祖先节点上；向上找 4 层
  const typeFromAncestors = (el) => {
    let cur = el;
    for (let depth = 0; cur && depth < 4; depth += 1, cur = cur.parentElement) {
      for (const attr of ['data-type', 'type', 'data-questiontype', 'data-question-type']) {
        const v = normalizeType(cur.getAttribute && cur.getAttribute(attr));
        if (v) return v;
      }
    }
    return '';
  };

  const LETTER_RE = /^[(（\[【]?\s*([A-Ha-h])\s*[)）\]】.、,，:：]?\s*/;
  const letterAt = (i) => String.fromCharCode(65 + i);
  const isNoiseText = (t) =>
    !t || t.length > 200 || /查看答案|正确答案|答案解析|我的答案/.test(t);

  // ---- 配伍题（B 型题）小题抽取 ---------------------------------------- //
  // 「1. xxx」「2、xxx」「1xxx」（序号在 <i class="serial"> 里、innerText 拼出来是
  // 「1入睡困难」）三种写法都要认；分隔符可有可无，但缺分隔符时序号后面必须紧跟
  // 中文字符，否则题干里的数字（如「收缩压 120mmHg」）会被误当成小题序号。
  const ITEM_PREFIX_RE = /^\s*[(（\[【]?\s*(\d{1,2})\s*(?:[)）\]】.、,，:：]\s*|(?=[\u4e00-\u9fa5]))/;
  const ITEM_NODE_SEL =
    'li, .small-question, .sub-question, .ZiTiMu, .zi-ti, .question-small, p, .mark_item';

  // 小题序号不能来自杂项节点的「顺带文字」：真实页面里 div.Zy_TItle 的 innerText
  // 拼出来是「6. 配伍题（B 型题）」，ITEM_PREFIX_RE 会把它当成小题 6，于是
  // 小题列表变成 ['6. 配伍题（B 型题）', '6. 配伍题（B 型题） 1. 入睡困难', …]。
  // 所以只允许两种来源：显式小题列表容器里的 li，或文本本身就只有「编号 + 文字」。
  const SHORT_ITEM_MAX = 80;
  const cleanItemText = (t) => {
    const s = strip(t || '');
    if (!s) return '';
    const m = s.match(ITEM_PREFIX_RE);
    if (!m) return '';
    return strip(s.slice(m[0].length).replace(/^\s*[)）\]】.、,，:：]\s*/, ''));
  };

  const extractMatchItems = (el) => {
    // 备选（A-E）所在的容器不要当成小题来源，先收集它们，后面整枝剔掉
    const bankNodes = new Set();
    el.querySelectorAll('input[type=radio], input[type=checkbox]').forEach((inp) => {
      const box = inp.closest('li') || inp.closest('label') || inp.parentElement;
      if (box) bankNodes.add(box);
    });
    const LETTER_ROW_RE = /^\s*[（(\[【]?\s*[A-Ha-h]\s*[)）\]】.、,，:：]/;
    const NUM_ROW_RE = /^\s*[（(\[【]?\s*\d{1,2}\s*[)）\]】.、,，:：]/;
    // 备选行所在的容器（ul.mark_letter / .B-info：整枝都是备选）。
    el.querySelectorAll('ul.mark_letter, .B-info').forEach((n) => {
      bankNodes.add(n);
      n.querySelectorAll('li').forEach((li) => bankNodes.add(li));
    });
    // ⚠️ `.match-items` / `.small-questions` / `.zi-ti-list` 装的是**小题**，绝不能整枝
    // 标记为备选（实测 `tests\fixtures\answer_page.html` 的 #q1006：三个小题 li 被误标成
    // 备选行 → 主循环一条都没产出 → 退到「纯文本切分」兜底，`match_items` 变成
    // `['6. 配伍题（B 型题）', '6. 配伍题（B 型题） 1. 入睡困难', …]` 这种累积串）。
    // 只标记里面「确实像备选行」的 li（带 input，或带 A. 字母前缀），且不含「1.」数字前缀。
    el.querySelectorAll('.match-items, .small-questions, .zi-ti-list').forEach((n) => {
      n.querySelectorAll('li').forEach((li) => {
        if (li.querySelector('input[type=radio], input[type=checkbox]')) { bankNodes.add(li); return; }
        const t = strip(li.innerText || '');
        if (LETTER_ROW_RE.test(t) && !NUM_ROW_RE.test(t)) bankNodes.add(li);
      });
    });

    const seen = new Set();
    const items = [];
    // 真实复习页（mooc-ans work/view）的 B 型题把小题写在 .order_answer 里，
    // 文本形如「(1) 胆总管结石多表现为」—— 优先按这个结构取，最准。
    const orderNodes = Array.from(el.querySelectorAll('.order_answer'));
    if (orderNodes.length) {
      orderNodes.forEach((n) => {
        let t = strip(n.innerText || '');
        const m = t.match(ITEM_PREFIX_RE);
        if (m) t = t.slice(m[0].length).trim();
        t = strip(t.replace(/^\s*[)）\]】.、,，:：]\s*/, ''));
        if (t && !seen.has(t) && t.length <= 200) { seen.add(t); items.push(t); }
      });
      if (items.length) return items;
    }
    // 新版答题页的 B 型题（实测 2026-10 `mooc-ans/work/doWork`）结构：
    //   <div class="bTypeWrap">            <- 共享备选，不是小题来源
    //     <p class="B-info">(1)-(2) 题共用备选答案：</p>
    //     <div class="stem_answer">…A. 蹒跚步态 …</div>
    //   </div>
    //   <div class="B-answer-ct">          <- 每个小题一个
    //     <p class="B-tit">(1) </p><p>帕金森病患者可能出现的步态是</p>
    //     <div class="B-answerCon"><span data=A>A</span>…</div>
    //   </div>
    // 必须走这个分支：通用循环里 ITEM_NODE_SEL 含 `p`，而 <p class="B-tit"> 的
    // innerText 会把后面兄弟节点的文字一起拼进来，产出
    // 「(B 型题) （1～2共用备选答案） (1)-(」这种累积垃圾串（实测 4 道 B 型题全中）。
    const bCts = Array.from(el.querySelectorAll('.B-answer-ct'));
    if (bCts.length) {
      bCts.forEach((n) => {
        const tit = n.querySelector('.B-tit');
        let t = '';
        if (tit && tit.nextElementSibling) {
          t = strip(tit.nextElementSibling.innerText || '');
        }
        if (!t) {
          t = strip(n.innerText || '')
            .replace(/\s*[A-H]\s*[A-H]\s*[A-H]\s*[A-H](\s*[A-H])?\s*$/, '');
        }
        t = strip(t.replace(ITEM_PREFIX_RE, ''));
        t = strip(t.replace(/^\s*[)）\]】.、,，:：]\s*/, ''));
        if (t && !seen.has(t) && t.length <= SHORT_ITEM_MAX) { seen.add(t); items.push(t); }
      });
      if (items.length) return items;
    }
    el.querySelectorAll(ITEM_NODE_SEL).forEach((node) => {
      if (bankNodes.has(node)) return;
      const inList = !!node.closest('.match-items, .small-questions, .zi-ti-list');
      if (node.querySelector('input[type=radio], input[type=checkbox], textarea')) return;
      if (node.querySelector(ITEM_NODE_SEL)) return;      // 只取最内层，避免重复
      const raw = strip(node.innerText || '');
      if (!raw || raw.length > 200 || isNoiseText(raw)) return;
      // 经典配伍页的小题都在 ul/li 里，且一定带「1.」编号；无编号的 li 基本是备选行，不要。
      // 没有 ul 结构的页面由下面「纯文本切分」兜底，所以这里不会漏题。
      const t = cleanItemText(raw);
      if (!t || seen.has(t)) return;
      // 非小题列表容器里，只有「文本本身就只有编号 + 小题文字」才认，
      // 避免把题干块（「6. 配伍题（B 型题）」）当小题。
      if (!inList && raw.length > SHORT_ITEM_MAX) return;
      seen.add(t);
      items.push(t);
    });

    // 兜底：容器里没有 li 结构时，从纯文本按「1. xxx 2. yyy」切
    if (items.length < 2) {
      const raw = strip(el.innerText || '');
      const parts = [];
      const re = /(\d{1,2})\s*[)）\]】.、,，:：]\s*/g;
      let mm, last = null;
      while ((mm = re.exec(raw)) !== null) {
        if (last !== null) {
          const seg = raw.slice(last.end, mm.index).trim();
          if (seg && seg.length <= 200) parts.push(seg);
        }
        last = mm;
      }
      if (last !== null) {
        const seg = raw.slice(last.end).trim();
        if (seg && seg.length <= 200) parts.push(seg);
      }
      const clean = parts.map((t) => strip(t)).filter(Boolean)
        // 备选整行（「A.失眠」）不是小题
        .filter((t) => !/^\s*[A-Ha-h][.、)）]/.test(t))
        // 含 2 个以上「A. / A、」备选行的段落是「题干 + 一串备选」的拼接，不是小题
        .filter((t) => (t.match(/[A-Ha-h][.、)）]\S/g) || []).length < 2)
        .map((t) => {
          // 若段落尾部粘了备选，切掉（取最后一个「A.」之前的文字）
          const lm = t.match(/^(.*?)\s*[A-Ha-h][.、)）]\S/);
          return lm ? strip(lm[1]) : t;
        })
        .filter((t) => t && t.length <= SHORT_ITEM_MAX)
        // 只有「本来带 1./2. 编号」的段落才算小题
        .filter((t) => NUM_ROW_RE.test(t))
        .map((t) => t.replace(NUM_ROW_RE, '').trim())
        .filter(Boolean);
      if (clean.length >= 2) {
        const uniq = Array.from(new Set(clean));
        if (uniq.length >= 2) return uniq.slice(0, 12);
      }
    }
    return items;
  };

  // ---- ARIA 组件式答题页 ------------------------------------------------ //
  // 新版 mooc-ans `/work/doWork`（实测 2026-10 检体诊断学作业页，73 题）**没有任何
  // 原生 form 控件**：选项是带 role 的 div，答案写进隐藏域。
  //   <div class="answerBg workTextWrap" onclick="addChoice(this);" role="radio"
  //        qid="215177587" qtype="0">
  //     <span data="E" class="choice215177587 num_option fl">A</span>
  //     <div class="answer_p"><p>破伤风——角弓反张位</p></div>
  //   </div>
  //   <input type="hidden" id="answertype215177587" value="0">  <!-- 0单选 1多选 20配伍 -->
  //   <input type="hidden" id="answer215177587" value="">        <!-- 答案落这里 -->
  // 实测统计：answerable(input/textarea)=0、role=radio 67、role=checkbox 2、
  // hidden answer=73 —— 只认原生的 looksLikeItem 会把 73 道题**全部**丢掉（实际抓到 0 题）。
  const ARIA_OPTION_SEL = '.answerBg, [role=radio], [role=checkbox]';
  // 注意：`.answerBg` 只出现在题干区；B 型题的选项在 `.bTypeWrap .stem_answer`
  // 与 `.B-answerCon` 里，两者都不带 role，所以下面用 `.bTypeWrap` 单独接。
  const ARIA_ENTRY_SEL = '.answerBg, [role=radio], [role=checkbox], .B-answerCon, .bTypeWrap';
  // 读一个选项/一行 li 的**权威字母**。三种页面各有各的权威来源，顺序很重要：
  //   1. `[data]` 属性 —— 新版 ARIA 页（`.num_option[data=E]`）。页面会打乱屏显
  //      字母（data=E 却显示「A」），所以它必须排在屏显之前；
  //   2. 原生 input 的 `value` —— 老版页面（`input[value=A]`，配伍题多个小题
  //      共用同一组字母，靠它才对得上）；
  //   3. 行首文字「A.」「(B)」—— 静态夹具 / 复习页；
  //   4. 位置编号 —— 什么都没有时的最后兜底。
  // 少了 1/2 任意一条，字母就会与页面真正的 data/value 错位，回填立刻点错选项。
  const readLetterAt = (scope, i) => {
    const dEl = scope.querySelector('[data]');
    let letter = dEl ? (dEl.getAttribute('data') || '').trim().toUpperCase() : '';
    if (/^[A-H]$/.test(letter)) return letter;
    const inp = scope.querySelector('input[type=radio], input[type=checkbox]');
    letter = inp ? (inp.getAttribute('value') || inp.value || '').trim().toUpperCase() : '';
    if (/^[A-H]$/.test(letter)) return letter;
    const t = strip(scope.innerText || '');
    const mt = t.match(LETTER_RE);
    if (mt && /^[A-H]$/i.test(mt[1])) return mt[1].toUpperCase();
    return letterAt(i);
  };
  // 从选项框里读字母（`span.num_option[data=A]` 是权威来源）+ 正文（`.answer_p`）。
  const readChoiceBoxes = (el) => {
    const boxes = Array.from(el.querySelectorAll(ARIA_OPTION_SEL));
    const out = [];
    boxes.forEach((box, i) => {
      // ⚠️ 字母走统一的 readLetterAt（`data` 属性优先，而不是屏显 innerText）：
      // 多选题里页面会打乱屏显字母（实测：data=E 的选项屏显是「A」，
      // 因为 A 是页面标准答案）。先读 innerText 会把字母整体读错，
      // 回填时点错选项 —— 曾经就是这样错的。
      const letter = readLetterAt(box, i);
      let t = strip((box.querySelector('.answer_p') || box).innerText || '');
      const mt = t.match(LETTER_RE);
      if (mt) t = t.slice(mt[0].length).trim();
      if (!t) {
        const al = (box.getAttribute('aria-label') || '').replace(/选择$/, '').trim();
        t = al.replace(/^[A-Ha-h]\s*/, '').trim();
      }
      if (isNoiseText(t)) t = '';
      out.push({ letter: letter.toUpperCase(), text: t || ('选项' + letter.toUpperCase()) });
    });
    return out;
  };
  // 新版 B 型题：共享备选在 `.bTypeWrap .stem_answer`（每行「A. 蹒跚步态」），
  // 每个小题在 `.B-answer-ct`，组内可点的是 `.B-answerCon > span[data=A]`。
  const readBTypeOptions = (el) => {
    const out = [];
    el.querySelectorAll('.bTypeWrap .stem_answer > .clearfix, .stem_answer .clearfix').forEach((row) => {
      let t = strip(row.innerText || '');
      const mt = t.match(LETTER_RE);
      if (!mt) return;
      const letter = mt[1].toUpperCase();
      t = t.slice(mt[0].length).trim();
      if (!t || isNoiseText(t)) return;
      if (out.some((o) => o.letter === letter)) return;
      out.push({ letter, text: t });
    });
    if (out.length >= 2) return out.sort((a, b) => a.letter.localeCompare(b.letter));
    // 退化：从组内可点 span[data] 反查（拿不到正文，只能给字母）
    const letters = [];
    el.querySelectorAll('.B-answerCon span[data]').forEach((sp) => {
      const d = (sp.getAttribute('data') || '').trim().toUpperCase();
      if (/^[A-H]$/.test(d) && !letters.includes(d)) letters.push(d);
    });
    return letters.sort().map((d) => ({ letter: d, text: '选项' + d }));
  };

  // ---- 题目容器定位 ---------------------------------------------------- //
  // 注意：这里是「三路候选合并」，不是「一路不行再换一路」。
  // 真实页面经常混排：老版 .TiMu 嵌 iframe、新版组件又用 .question-item，
  // 早先写成 if(!items.length) 的话，只要页面上存在哪怕一个 .TiMu，
  // 其它形态的题就会被整体漏掉（实测夹具里 6 题只抓到 2 题）。
  const looksLikeItem = (el) => {
    if (!(el instanceof HTMLElement)) return false;
    if (!strip(el.innerText || '')) return false;
    // 「能答题」的最小条件：里面得有可作答的输入控件（radio/checkbox/
    // textarea/文本框）。只有标题文字的 .mark_item（章节测验的 h2 头）
    // 会被这一条挡掉 —— 否则它会被当成第 N 道题混进题目列表。
    const answerable = el.querySelectorAll(
      'input[type=radio], input[type=checkbox], textarea, input[type=text]');
    if (answerable.length) {
      const hasStem = el.querySelector('.Zy_TItle, .mark_name, .question-name, .stem, .TiMu .title');
      if (hasStem) return true;
      return answerable.length > 0;
    }
    // ARIA 组件式答题页（新版 mooc-ans doWork）：没有原生控件，可作答信号是
    // role=radio/checkbox 的选项块、.answerBg、B 型题的 .B-answerCon/.bTypeWrap。
    // 必须先过「有题干标记」这一道，否则只装题型标题的 .mark_item 外壳也会混进来。
    if (el.querySelector(ARIA_ENTRY_SEL)) {
      if (el.querySelector('.mark_name, .qtContent')) return true;
    }
    // 复习/详情页（真实 mooc-ans `work/view`，已提交作业的答卷页）没有输入控件，
    // 题目是 div.questionLi，题干在 h3.mark_name（内含 .qtContent），
    // 选项是 ul.mark_letter > li。只认「有题干标记 + 有选项」的块，
    // 纯标题块（.type_tit 分组头）依旧被挡掉。
    if (!el.querySelector('.mark_name, .qtContent')) return false;
    return el.querySelectorAll('ul.mark_letter > li, ul.qtDetail > li').length >= 2;
  };

  const CONTAINER_SEL = '.TiMu, .question-item, .mark_item, .exam-question, ' +
    'div.questionLi, div[id^=question], div[id^=q_], li[id^=q_]';

  const isNested = (el, all) => all.some((o) => o !== el && el.contains(o));

  let items = Array.from(document.querySelectorAll(CONTAINER_SEL));

  // 用「通用推断」补一路：连容器 class 都认不出来时，直接找「像一道题的最内层块」
  const guessed = Array.from(document.querySelectorAll('div, li, section, tr')).filter((el) => {
    const n = el.querySelectorAll('input[type=radio], input[type=checkbox]').length;
    if (n < 2) return false;
    if (!strip(el.innerText || '')) return false;
    const same = Array.from(el.querySelectorAll('div, li, section, tr')).filter((c) =>
      c.querySelectorAll('input[type=radio], input[type=checkbox]').length === n);
    return same.length === 0;
  });
  guessed.forEach((el) => { if (!items.includes(el)) items.push(el); });

  // 复习页分支：真实 mooc-ans `work/view` 里题目是 div.questionLi，
  // 外层 .mark_item 只是「题型分组」的壳（含 <h2 class="type_tit">一. 单选题</h2>）。
  // 若把外壳也当题目，一道 14 小题的单选题组会被压成 1 道怪题，所以这里剔除外壳。
  //
  // 但 B 型题（配伍题）例外：它的结构是
  //   .questionLi#questionXXX (外层，题干 + 三组共享备选)
  //     └ .mark_item
  //         └ .questionLi  <- 内层，只装「(1)-(3) 题共用备选答案」那一小块
  // 内层 questionLi 会被 isNested 判为嵌套而丢掉整个外层题组（实测第 15 题
  // `#question214133358` 就这样消失）。所以规则要写成：
  //   「有 questionLi 祖先的 questionLi」丢内层，其余外壳一律丢。
  const hasQuiLiInside = (el) => !!el.querySelector('div.questionLi, .questionLi');
  const hasQuiLiAncestor = (el) => {
    let p = el.parentElement;
    while (p) {
      if (p.classList && p.classList.contains('questionLi')) return true;
      p = p.parentElement;
    }
    return false;
  };
  // 只保留「最外层 questionLi」。B 型题的外层 questionLi 里还嵌着一个
  // 内层 questionLi（装「(1)-(3) 题共用备选答案」那一块），内层丢掉、外层留下，
  // 这样一道 B 型题组才是**一道题**（含共享备选 + 多个小题），而不是被拆散或整组消失。
  const questionLis = items.filter(
    (el) => el.classList.contains('questionLi') && !hasQuiLiAncestor(el));
  if (questionLis.length) items = questionLis;
  else {
    // 没有 questionLi 的页面（新版答题页 / 老版 .TiMu）：退化为「丢掉题型分组外壳」。
    items = items.filter((el) => !hasQuiLiInside(el));
  }

  // 套在一起的容器只保留最外层，避免同一个题被解析两次。
  // 注意 here 的判定：自己不像题的一律丢掉 —— 早先写成
  // `if (!like && wrap) return false`，于是「既不像题也不是 wrapper」的
  // 纯标题块（如只有 <h2>章节测验</h2> 的 .mark_item）会被当成一道题。
  items = items.filter((el) => {
    if (isNested(el, items)) return false;
    if (!looksLikeItem(el)) return false;
    return true;
  });

  const seen = new Set();
  items = items.filter((el) => {
    if (seen.has(el)) return false;
    seen.add(el);
    return true;
  });
  // ---- 逐题解析 -------------------------------------------------------- //
  const out = [];
  // 回填端要能精确取回「同一个元素」：这里给每题记一个 (选择器, 下标) 定位键。
  // 不能只记下标 —— 抽取端做过去重/嵌套过滤，和页面上「原始选择器命中的第 N 个」
  // 并不是同一个元素（实测混排页面里按 index 取会整体错位）。
  const qIndexIn = (selector, el) => {
    const all = Array.from(document.querySelectorAll(selector));
    return all.indexOf(el);
  };
  items.forEach((el, idx) => {
    // 题干：从「标题写法的元素」里取最短的那个，避免把整题文本当题干
    const stemEls = Array.from(el.querySelectorAll(
      '.qtContent, .Zy_TItle .clearfix, .Zy_TItle, .mark_name, .question-name, .stem, .title, .fl.clearfix'));
    let stem = '';
    stemEls.map((e) => strip(e.innerText)).filter(Boolean).sort((a, b) => a.length - b.length)
      .some((t) => { stem = t; return true; });
    if (!stem) stem = strip(el.innerText).slice(0, 600);

    // 复习页（已提交答卷）会把标准答案直接印在页面上：`.rightAnswerContent`
    // （单选/多选/简答）或 `.B_daan.rightAnswerContent`（B 型题）。
    // 抓下来有两个用处：① 人工核对是否与 AI 一致；② `bank harvest` 直接入库，
    // 以后再遇到同一题就不用问模型 —— 这是最省 token 的答案来源。
    let reviewAnswer = '';
    let reviewMine = '';
    try {
      const ra = el.querySelector('.rightAnswerContent');
      if (ra) reviewAnswer = strip(ra.innerText || '');
      const ma = el.querySelector('.stuAnswerContent');
      if (ma) reviewMine = strip(ma.innerText || '');
    } catch (e) { /* 忽略 */ }

    // 题型
    const typeHint = strip(el.innerText).slice(0, 120);
    // `typename`：新版答题页把「单选题 / 多选题 / B 型题」写在这个属性上
    // （实测 73 题全都有，而页面上一个 data-type 都没有）。
    const typenameHint = el.getAttribute('typename')
      || el.getAttribute('data-typename') || el.getAttribute('type-name') || '';
    let qtype = normalizeType(el.getAttribute('data-type'))
      || normalizeType(el.getAttribute('type'))
      || normalizeType(el.getAttribute('data-questiontype'))
      || typeFromText(typenameHint)
      || typeFromAncestors(el.parentElement)
      || typeFromHint(typeHint);

    // 选项
    let opts = [];
    // 「这一题页面上能不能作答」。原生控件走 inputs；新版答题页没有原生控件，
    // 只有 role=radio/checkbox + onclick=addChoice 的 `.answerBg` 块（点了之后
    // 页面脚本把字母写进隐藏域 #answer<qid>），所以额外记一个 has_choice_ui，
    // 否则 CLI 会把这种答题页误判成「已提交的复习页」而跳过回填。
    let hasChoiceUi = false;
    let inputs = Array.from(el.querySelectorAll('input[type=radio], input[type=checkbox]'));
    inputs = inputs.filter((i) => i.type === 'radio' || i.type === 'checkbox');
    const isMatchQ = (t) => t === 'match' || typeFromHint(typeHint) === 'match'
      || !!typenameHint.replace(/\s+/g, '').match(/B\s*1?\s*型/i);
    if (inputs.length >= 2) {
      opts = inputs.map((inp, i) => {
        let box = inp.closest('li') || inp.closest('label') || inp.closest('.option-item')
          || inp.closest('div');
        if (box && box !== el && box.contains(inp) && box.querySelectorAll('input').length > 1) {
          box = inp.parentElement || box;
        }
        let t = strip((box || inp.parentElement || inp).innerText || '');
        const mt = t.match(LETTER_RE);
        if (mt) t = t.slice(mt[0].length).trim();
        if (isNoiseText(t)) t = '';
        // 字母从原生 input 的 value / 行首文字读（配伍题每个小题共用同一组字母，
        // 位置编号会把第 2 组编成 E、F、G、H…，回填时字母就对不上了）。
        const letter = readLetterAt(box || inp.parentElement || inp, i);
        return { letter, text: t || ('选项' + letter) };
      });
      if (!qtype) qtype = inputs[0].type === 'checkbox' ? 'multiple' : 'single';
    } else if (isMatchQ(qtype) && el.querySelector('.bTypeWrap, .B-answerCon')) {
      // 新版 B 型题：共享备选在 .bTypeWrap .stem_answer 的每一行
      opts = readBTypeOptions(el);
      if (el.querySelector('.B-answerCon')) hasChoiceUi = true;
      if (!qtype) qtype = 'match';
    } else {
      // ARIA 组件式选项（新版答题页）：优先读 .answerBg/[role=radio|checkbox]
      const aria = readChoiceBoxes(el);
      if (aria.length >= 2 && aria.filter((o) => o.text).length >= 2) opts = aria;
      if (aria.length) hasChoiceUi = true;
    }
    if (!opts.length) {
      const uls = Array.from(el.querySelectorAll('ul.Zy_ulTop, ul.mark_letter, ul.clearfix, ul'));
      for (const ul of uls) {
        const lis = Array.from(ul.children).filter((c) => c.tagName === 'LI');
        if (lis.length < 2) continue;
        const texts = lis.map((li, i) => {
          let t = strip(li.innerText || '');
          const mt = t.match(LETTER_RE);
          // ⚠️ 字母同样走统一的 readLetterAt（原生 input 的 value / `data` 属性
          // 优先，位置编号只是兜底）：回填是按字母找选项的，这里错了就点错。
          const letter = readLetterAt(li, i);
          if (mt) t = t.slice(mt[0].length).trim();
          if (isNoiseText(t)) {
            const a = li.querySelector('a, .option-text, label, span');
            t = a ? strip(a.innerText || '') : '';
          }
          return { letter, text: t || ('选项' + letter) };
        });
        if (texts.filter((x) => x.text).length >= 2) { opts = texts; break; }
      }
      if (!qtype && opts.length >= 2) qtype = 'single';
    }

    // 硬信号兜底：复习页的配伍题一定会有 div.order_answer 装小题
    // （「(1) 胆总管结石多表现为」这种）。题型文字可能被改写成
    // 「组题目」「B型题」的任意变体，正则总有失手，这里用结构特征再兜一层。
    if (qtype !== 'match' && el.querySelectorAll('.order_answer').length >= 2) {
      qtype = 'match';
    }

    // 填空 / 简答判定
    if (!qtype) {
      const tas = Array.from(el.querySelectorAll('textarea, input[type=text]'));
      if (tas.length >= 2) qtype = 'fill';
      else if (tas.length === 1) qtype = 'short';
      else if (opts.length >= 2) qtype = 'single';
    }
    if (!qtype) qtype = typeFromHint(typeHint) || 'other';

    // ---- 配伍题（B 型题）识别 ------------------------------------------ //
    // 页面形态：一个题组容器里既有「1.失眠 2.发热 3.头痛」这种小题文本，
    // 又有一组共享备选（A-E 选项）。也可能被学习通按小题拆成多个容器，
    // 每个容器都挂着同一组备选，此时靠 data-type=6/7 或题干里的「配伍」字样判定。
    const isMatchType = qtype === 'match' || typeFromHint(typeHint) === 'match';
    let matchItems = [];
    if (isMatchType) {
      matchItems = extractMatchItems(el);
      if (matchItems.length && opts.length >= 2) qtype = 'match';
      else if (isMatchType && !matchItems.length && opts.length >= 2) {
        // 单个小题的配伍题：题干本身就是小题，选项是备选
        matchItems = [stem];
        qtype = 'match';
      }
    }

    // 填空题可能带多个空
    const textFields = el.querySelectorAll('textarea, input[type=text]').length;
    if (qtype === 'fill' && !textFields && opts.length === 0) {
      // 没有输入框也没有选项，可能是纯展示，仍按填空处理
    }

    let qid = el.getAttribute('data-questionid') || el.id || String(idx + 1);
    if (/^\d+$/.test(qid) && document.querySelectorAll('.TiMu').length) {
      qid = 'q' + qid;
    }

    // 新版 ARIA 答题页把「这一题当前选中了什么」放在隐藏域
    // <input type="hidden" id="answer<数字>">（提交时服务端读的就是它）。
    // 注意不能拿上面的 qid 去拼：qid 是 question215177587，隐藏域却是
    // #answer215177587 —— 中间那个 "question" 前缀会把选择器拼错，
    // 于是点击成功后读不到值，看起来像「点了没生效」。
    // 页面上最权威的编号是选项块自己的 qid 属性 / data 属性。
    let answerKey = '';
    {
      const carrier = el.querySelector('.answerBg[qid], [role=radio][qid], [data]');
      const raw = (carrier && (carrier.getAttribute('qid') || carrier.getAttribute('data'))) || '';
      if (/^\d+$/.test(raw)) {
        answerKey = raw;
      } else {
        const m = String(el.id || '').match(/^(?:question)?(\d+)$/);
        answerKey = m ? m[1] : (/^\d+$/.test(el.id || '') ? el.id : '');
      }
    }

    const cleaner = strip(stem);
    // 定位键：在「匹配到本元素」的候选里挑命中数最少（最具体）的那个，
    // 回填端按 `sel` + `sel_index` 取第几个，保证拿回同一个元素。
    let selHint = '', selIdx = -1, selCount = Infinity;
    for (const cand of ['.TiMu', '.question-item', '.mark_item', '.exam-question',
                        'tr.exam-question', 'div[id^=question]', 'li[id^=q_]',
                        'div.questionLi']) {
      if (!el.matches || !el.matches(cand)) continue;
      const n = document.querySelectorAll(cand).length;
      if (n < selCount) { selCount = n; selHint = cand; selIdx = qIndexIn(cand, el); }
    }
    if (!selHint) { selHint = CONTAINER_SEL; selIdx = qIndexIn(CONTAINER_SEL, el); }
    out.push({
      qid: qid,
      raw_id: el.id || '',
      answer_key: answerKey,
      qtype: qtype,
      stem: cleaner,
      options: opts,
      match_items: matchItems,
      blanks: qtype === 'fill' ? Math.max(1, textFields) : 0,
      has_text_input: textFields > 0,
      has_input: inputs.length,
      has_choice_ui: hasChoiceUi ? 1 : 0,
      review_answer: reviewAnswer,
      review_mine: reviewMine,
      sel: selHint,
      sel_index: selIdx,
      text: strip(el.innerText).slice(0, 1500),
    });
  });

  return {
    version: '1.0',
    url: location.href,
    title: document.title,
    count: out.length,
    questions: out,
  };
}
"""

# 兜底：把整页所有可见文本抓出来，用于选择器彻底失效时人工定位
DEBUG_PAGE_JS = r"""
() => {
  const strip = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const classes = {};
  document.querySelectorAll('div, li, ul, span').forEach((el) => {
    const c = el.className;
    if (typeof c === 'string' && c) {
      c.split(/\s+/).filter(Boolean).forEach((k) => { classes[k] = (classes[k] || 0) + 1; });
    }
  });
  return {
    url: location.href,
    title: document.title,
    iframes: Array.from(document.querySelectorAll('iframe')).map((f) => ({
      id: f.id, name: f.name, src: f.src,
    })),
    classHistogram: Object.entries(classes).sort((a, b) => b[1] - a[1]).slice(0, 80),
    bodyText: strip(document.body ? document.body.innerText : '').slice(0, 4000),
    htmlLength: document.documentElement.outerHTML.length,
  };
}
"""

# 把答案写进输入框并触发学习通前端依赖的事件
FILL_TEXT_JS = r"""
(args) => {
  const el = args.el;
  if (!el) return false;
  const value = args.value == null ? '' : String(args.value);
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  el.focus();
  if (setter && setter.set) setter.set.call(el, value); else el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('keyup', { bubbles: true }));
  el.dispatchEvent(new Event('blur', { bubbles: true }));
  return el.value === value;
}
"""

# 富文本编辑器（简答题常见 .edui-body-container）兜底写入
FILL_EDITOR_JS = r"""
(args) => {
  const el = args.el;
  if (!el) return false;
  el.innerHTML = '';
  const lines = String(args.value == null ? '' : args.value).split('\n');
  lines.forEach((ln, i) => {
    const p = document.createElement('p');
    p.textContent = ln || '\u00a0';
    el.appendChild(p);
  });
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
"""
