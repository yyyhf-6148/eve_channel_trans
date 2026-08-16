# -*- coding: utf-8 -*-
"""初始化本地 git 仓库、提交源码并配置远程仓库。"""
import subprocess
import sys

GIT = r"C:\Program Files\Git\cmd\git.exe"
REMOTE = "https://github.com/yyyhf-6148/eve_channel_trans.git"


def run(args, check=True):
    r = subprocess.run([GIT] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print("[out]", out)
    if err:
        print("[err]", err)
    if check and r.returncode != 0:
        print("[FAIL]", args)
        sys.exit(r.returncode)
    return r


# 1. 初始化仓库（若尚未初始化）
r = run(["rev-parse", "--is-inside-work-tree"], check=False)
if r.returncode != 0:
    run(["init", "-b", "main"])

# 2. 暂存并提交（本地指定提交者，不修改全局 git 配置）
run(["add", "-A"])
r = run(["-c", "user.name=yyyhf-6148",
         "-c", "user.email=yyyhf-6148@users.noreply.github.com",
         "commit", "-m", "EVE channel translation overlay tool"], check=False)
if r.returncode != 0:
    print("[info] 无新增提交（可能已提交过）")

# 3. 配置远程仓库
run(["remote", "remove", "origin"], check=False)
run(["remote", "add", "origin", REMOTE])

print("OK: 本地仓库已就绪，远程已配置。")
