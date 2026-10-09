# -*- coding: utf-8 -*-
"""用 GitHub REST API 推送本地 HEAD —— 给连不上 github.com:443 的网络用。

为什么要它
----------
`git push` 走的是 github.com:443 上的 smart-HTTP。有些网络（实测中国大陆部分
线路）会把这台机器的 443 阻断，表现为：

    fatal: unable to access 'https://github.com/...': Recv failure: Connection was reset
    fatal: unable to access 'https://github.com/...': Failed to connect to github.com:443
                                                   after 21079 ms: Could not connect to server

但同一个 GitHub 的 `api.github.com` 与 `codeload.github.com` 却完全通畅。于是
本脚本绕开 git 协议，改用纯 REST 把**当前 HEAD 提交**原样搬上去：

    POST /repos/{owner}/{repo}/git/blobs   每个文件一个 blob（base64）
    POST /repos/{owner}/{repo}/git/trees   组装整棵树
    POST /repos/{owner}/{repo}/git/commits 建一个 commit（父提交 = 远程当前 ref）
    PATCH /repos/{owner}/{repo}/git/refs/heads/{branch}

按 git 对象（`git cat-file blob`）取内容，而不是读工作区字节 —— 带
`.gitattributes` 时工作区可能是 CRLF 而入库的是 LF，读工作区会导致远程 blob SHA
与本地对不上。脚本会自检 `tree SHA` 与 blob SHA，不一致直接报错退出。

用法
----
    python tools/publish_via_api.py --repo Yee-jan/xuexitong-tutor --token-file C:\\path\\token
    python tools/publish_via_api.py --repo owner/name -m "fix: xxx"      # token 走 GH_TOKEN
    python tools/publish_via_api.py --repo owner/name --dry-run          # 只看要推什么

参数
----
    --repo OWNER/NAME   必填
    --token-file PATH   读 token 的文件（strip 空白）；不给则用环境变量 GH_TOKEN
    -m/--message TEXT   commit message；不给则用 git 的 `HEAD` message
    --branch NAME       默认取 git 当前分支
    --dry-run           只打印将要上传的文件清单
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = "https://api.github.com"


def git(*args: str, cwd: Path, binary: bool = False):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def read_token(args) -> str:
    if args.token_file:
        raw = Path(args.token_file).read_text("utf-8", "replace")
    else:
        raw = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    # PowerShell 管道和记事本都可能塞进换行/空格/不可见字符
    return "".join(ch for ch in raw if not ch.isspace())


def api(method: str, url: str, tok: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "publish-via-api")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} {method} {url}\n{e.read().decode('utf-8', 'replace')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="用 GitHub API 推送本地 HEAD（绕开被阻断的 github.com:443）")
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--token-file", help="含 token 的文件路径")
    ap.add_argument("-m", "--message", help="commit message（默认用 git 的 HEAD message）")
    ap.add_argument("--branch", help="目标分支（默认当前分支）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cwd", default=".", help="git 仓库目录，默认当前目录")
    args = ap.parse_args()

    cwd = Path(args.cwd).resolve()
    if "/" not in args.repo:
        raise SystemExit("--repo 要写成 OWNER/NAME")
    owner, name = args.repo.split("/", 1)
    api_base = f"{API_ROOT}/repos/{owner}/{name}"

    dirty = git("status", "--porcelain", cwd=cwd).strip()
    if dirty:
        print("⚠ 工作区有未提交改动 —— 推的是 HEAD，这些改动不会被上传：")
        for line in dirty.splitlines()[:10]:
            print(f"    {line}")

    branch = args.branch or git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).strip()
    head = git("rev-parse", "HEAD", cwd=cwd).strip()
    tree_sha = git("rev-parse", "HEAD^{tree}", cwd=cwd).strip()
    message = args.message or git("log", "-1", "--format=%B", cwd=cwd).rstrip()

    entries: list[tuple[str, str, str]] = []
    for line in git("ls-tree", "-r", "-z", "HEAD", cwd=cwd).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, otype, sha = meta.split()
        if otype == "blob":
            entries.append((mode, path, sha))

    print(f"仓库      {args.repo}    分支 {branch}")
    print(f"本地 HEAD {head[:8]}   tree {tree_sha}")
    print(f"待推文件  {len(entries)} 个")
    if args.dry_run:
        for _m, p, s in entries:
            print(f"    {s[:8]}  {p}")
        print("\n（--dry-run，什么都没上传）")
        return 0

    tok = read_token(args)
    if not tok:
        raise SystemExit("没拿到 token：给 --token-file 或设环境变量 GH_TOKEN")

    # 空仓库不能建 blob（409 Git Repository is empty）：先用 Contents API 放个占位
    # 文件把仓库激活。随后建的提交不带 parents（根提交），再把 ref 指过去，
    # 占位提交就变成不可达对象。
    try:
        api("GET", f"{api_base}/git/refs/heads/{branch}", tok)
    except SystemExit:
        print("远程还没有该分支，先激活空仓库 …")
        api("PUT", f"{api_base}/contents/.bootstrap", tok,
            {"message": "bootstrap",
             "content": base64.b64encode(b"init\n").decode("ascii")})
        head = None

    parent = None
    if head is not None:
        try:
            parent = api("GET", f"{api_base}/git/refs/heads/{branch}", tok)["object"]["sha"]
        except SystemExit:
            parent = None

    tree_items = []
    for i, (mode, path, sha) in enumerate(entries, 1):
        content = base64.b64encode(git("cat-file", "blob", sha, cwd=cwd, binary=True)).decode("ascii")
        r = api("POST", f"{api_base}/git/blobs", tok,
                {"content": content, "encoding": "base64"})
        if r["sha"] != sha:
            raise SystemExit(f"!! {path} 的 blob SHA 对不上：{r['sha']} != {sha}")
        tree_items.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        print(f"  [{i:2d}/{len(entries)}] {sha[:8]}  {path}")

    new_tree = api("POST", f"{api_base}/git/trees", tok, {"tree": tree_items})["sha"]
    if new_tree != tree_sha:
        raise SystemExit(f"!! tree SHA 对不上：{new_tree} != {tree_sha}")

    author = {"name": git("config", "user.name", cwd=cwd).strip() or owner,
              "email": git("config", "user.email", cwd=cwd).strip()
                       or f"{owner}@users.noreply.github.com"}
    payload = {"message": message, "tree": new_tree, "author": author, "committer": author}
    if parent:
        payload["parents"] = [parent]
    commit = api("POST", f"{api_base}/git/commits", tok, payload)["sha"]
    print(f"\ncommit {commit}")
    print(f"tree   {new_tree}" + ("  ✔ 与本地 HEAD 完全等价" if new_tree == tree_sha else ""))

    if parent:
        api("PATCH", f"{api_base}/git/refs/heads/{branch}", tok,
            {"sha": commit, "force": False})
    else:
        try:
            api("POST", f"{api_base}/git/refs", tok,
                {"ref": f"refs/heads/{branch}", "sha": commit})
        except SystemExit:
            api("PATCH", f"{api_base}/git/refs/heads/{branch}", tok,
                {"sha": commit, "force": True})

    remote = api("GET", f"{api_base}/git/trees/{commit}?recursive=1", tok)
    remote_blobs = {t["path"]: t["sha"] for t in remote["tree"] if t["type"] == "blob"}
    bad = [p for _m, p, s in entries if remote_blobs.get(p) != s]
    print(f"远程 {len(remote_blobs)} 个 blob" + ("" if not bad else f"，★ 不一致 {bad}"))
    print(f"\nhttps://github.com/{args.repo}/tree/{branch}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
