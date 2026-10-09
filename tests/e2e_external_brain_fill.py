"""外接大脑「真回填」端到端测试。

与 `e2e_external_brain.py` 的区别：
    * `e2e_external_brain.py` 只验证「答案文件 -> 内存里的 Answer」，全程无浏览器。
    * 本文件验证**用户真正要的那条链路**：
          抓题落盘 -> 外部 AI 给答案文件 -> CLI 导入 -> 打开页面 -> 真的填进去

为什么要单独写：`--from-dir` + `--answers` + `--to-url` 这条组合曾经是**坏的**——
`--to-url` 给了却不生效（代码没开浏览器，一路走到「离线模式」直接 return 0，
答案一个字都没填）。没有这个测试就发现不了，因为它退出码仍然是 0。

运行：python tests/e2e_external_brain_fill.py
退出码 0 = 全通过；非 0 = 有失败项。

注意：需要能启动 Chromium（沙箱环境下要放宽权限，否则报 WinError 5）。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xxtutor.browser import BrowserSession          # noqa: E402
from xxtutor.cli import save_questions              # noqa: E402
from xxtutor.config import load_config              # noqa: E402
from xxtutor.page import ChaoxingPage               # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "answer_page.html"
WORK = ROOT / ".state" / "e2e-brain-fill"
PASS: list = []
FAIL: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))


# 故意和 e2e_offline.py 里的 EXPECTED 写得不一样：
# 只要页面上出现的是这一组，就证明「填进去的确实是外部 AI 的答案」，
# 而不是求解器从缓存/页面/模型里自己凑出来的。
AI_ANSWERS = {
    "q1001": "A",                                  # 单选 -> 选项文字 def
    "q1002": ["A", "C"],                           # 多选 -> GET, PUT
    "q1003": False,                                # 判断题 -> 错误
    "q1004": ["8080", "8443"],                     # 两空填空
    "q1005": "AI 外接大脑写的简答（端到端测试）",     # 简答
    "q1006": {"1": "C", "2": "A", "3": "D"},       # 配伍 -> 低血压 / 失眠 / 心悸
}


def build_base_cfg():
    cfg = load_config(None, root=ROOT, strict=False)
    cfg.browser.headless = True
    cfg.answer.min_delay_ms = 0
    cfg.answer.max_delay_ms = 0
    cfg.output.dump_dom = False
    return cfg


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    run_dir = WORK / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    profile = WORK / "profile"
    out_dir = WORK / "out"

    uri = FIXTURE.resolve().as_uri()
    print(f"仿真答题页：{uri}\n")

    # ---------- 1) 抓题落盘（模拟第一次运行 answer 抓完题就走） ----------
    print("=== 1) 抓题落盘 ===")
    cfg = build_base_cfg()
    cfg.browser.user_data_dir = str(profile)
    with BrowserSession(cfg) as s:
        page = ChaoxingPage(s, cfg)
        s.goto(uri)
        qs = page.extract_questions()
    check("抓到 6 道题", len(qs) == 6, f"实际 {len(qs)}")
    qpath = save_questions(qs, run_dir)
    check("questions.json 已落盘", qpath.exists())
    check("questions.json 是裸数组（AI 最容易读的形状）",
          isinstance(json.loads(qpath.read_text(encoding="utf-8")), list))

    # ---------- 2) 外部 AI 给的答案文件 ----------
    print("\n=== 2) 写外部 AI 答案文件 ===")
    ai_file = WORK / "ai-answers.json"
    ai_file.write_text(json.dumps(AI_ANSWERS, ensure_ascii=False, indent=2), encoding="utf-8")
    check("ai-answers.json 已写好", ai_file.exists())

    # ---------- 3) 临时配置（指向临时档案与临时输出目录） ----------
    cfg_file = WORK / "config.json"
    cfg_file.write_text(json.dumps({
        "browser": {"headless": True, "user_data_dir": str(profile)},
        "answer": {"min_delay_ms": 0, "max_delay_ms": 0, "submit": False},
        "output": {"dir": str(out_dir), "dump_dom": False},
        "store": {"path": str(WORK / "answers.db")},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 4) 跑真正的 CLI：--from-dir + --answers + --to-url ----------
    print("\n=== 3) CLI：answer --from-dir --answers --to-url ===")
    from xxtutor.cli import main as cli_main

    rc = cli_main([
        "--config", str(cfg_file),
        "answer",
        "--from-dir", str(run_dir),
        "--answers", str(ai_file),
        "--to-url", uri,
        "--no-llm",
    ])
    check("CLI 退出码为 0", rc == 0, f"实际 {rc}")

    # ---------- 5) 断言真的回填了 ----------
    print("\n=== 4) 回填结果 ===")
    reports = sorted(out_dir.glob("*/fill-report.json"))
    check("生成了 fill-report.json（= 真的开了页面填答案）",
          len(reports) == 1, f"找到 {len(reports)} 个：{[str(p) for p in reports]}")
    filled = failed = 0
    if reports:
        rep = json.loads(reports[0].read_text(encoding="utf-8"))
        filled, failed = int(rep.get("filled") or 0), int(rep.get("failed") or 0)
        check("6 题全部填答成功", filled == 6 and failed == 0,
              f"filled={filled} failed={failed}")
        # apply_answers 的 details 会带每题成败与原因
        bad = [d for d in (rep.get("details") or []) if not d.get("ok", True)]
        check("没有任何一题回填失败", not bad, str(bad[:3]))

    runs = sorted([p for p in out_dir.glob("*") if p.is_dir()])
    ajson = runs[-1] / "answers.json" if runs else None
    check("answers.json 已落盘", bool(ajson and ajson.exists()))
    if ajson and ajson.exists():
        data = json.loads(ajson.read_text(encoding="utf-8"))
        items = data.get("items") or []
        got = {it.get("qid"): it for it in items}
        check("答案来源标记为 file（外部答案文件）",
              all((it.get("source") or "") == "file" for it in items),
              str({it.get("qid"): it.get("source") for it in items}))
        # 注意：answers.json 的条目用 `answer` 作答案键（不是 `value`）
        check("#q1001 用的是外部答案 A（不是求解器自己凑的）",
              _norm(_ans_of(got.get("q1001"))) == "A",
              str(got.get("q1001")))
        check("#q1002 多选是 A/C",
              _norm(_ans_of(got.get("q1002"))) == "A,C",
              str(got.get("q1002")))
        check("#q1004 两空填空保持列表 8080/8443（不能被拍平成 80808443）",
              _norm(_ans_of(got.get("q1004"))) == "8080,8443",
              str(got.get("q1004")))
        check("#q1003 判断题 false 落成选项字母 B（=错误）",
              _norm(_ans_of(got.get("q1003"))) == "B",
              str(got.get("q1003")))
        check("#q1006 配伍题保留 3 组配对",
              _pair_count(_ans_of(got.get("q1006"))) == 3,
              str(got.get("q1006")))

    # ---------- 6) 复核：离线守卫不会误伤 ----------
    # 反例——不给 --to-url 且不给 -c 时必须报错（而不是静默成功）
    print("\n=== 5) 反例：--from-dir 不给回填目标 ===")
    rc2 = cli_main([
        "--config", str(cfg_file),
        "answer",
        "--from-dir", str(run_dir),
        "--answers", str(ai_file),
        "--no-llm",
    ])
    check("缺少回填目标时报错退出（非 0）", rc2 != 0, f"实际退出码 {rc2}")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过 ✔")
    return 0


def _ans_of(item) -> object:
    """answers.json 的答案键是 `answer`；兼容旧写法 `value`。"""
    if not isinstance(item, dict):
        return None
    if item.get("answer") is not None:
        return item.get("answer")
    return item.get("value")


def _pair_count(v) -> int:
    """配伍题答案的配对数：dict / "1:A,2:B" 两种形态都算。"""
    if isinstance(v, dict):
        return len(v)
    if isinstance(v, str):
        return sum(1 for part in v.split(",") if ":" in part)
    return 0


def _norm(v) -> str:
    """把答案值压成可比字符串：['A','C'] 与 'A,C' 视为相同。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x).strip().upper() for x in v)
    return str(v).strip().upper()


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    print(f"耗时 {time.time() - t0:.1f}s")
    raise SystemExit(rc)
