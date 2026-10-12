"""生成发布说明：python packaging/release_notes.py [上一次发布的提交]，输出到标准输出。

每个合并的 PR 是 main 上的一个提交，标题就是更新内容。最后一行记下本次提交，下次从这里接着列。
"""
import re
import subprocess
import sys


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def main() -> None:
    prev = sys.argv[1] if len(sys.argv) > 1 else ""
    head = git("rev-parse", "HEAD").strip()
    ok = prev and subprocess.run(["git", "merge-base", "--is-ancestor", prev, head], capture_output=True).returncode == 0
    rng = [f"{prev}..{head}"] if ok else ["-n", "10", head]
    titles = [re.sub(r"\s*\(#\d+\)$", "", t).strip() for t in git("log", "--first-parent", "--format=%s", *rng).splitlines()]
    titles = [t for t in titles if t] or ["维护更新"]
    print("\n".join(f"- {t}" for t in titles))
    print(f"\n<!-- commit: {head} -->")


if __name__ == "__main__":
    main()
