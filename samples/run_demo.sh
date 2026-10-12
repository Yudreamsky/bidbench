#!/usr/bin/env bash
# 用合成数据演示整条流水线：逐批收图 → 模拟识别 → 计算台账和事实 → 报告。
set -euo pipefail
OUT=${1:-demo-project}
SAMPLES=samples/out
PY=python3
python3 -c "" >/dev/null 2>&1 || PY=python  # Windows 的虚拟环境里没有 python3
"$PY" samples/make_sample.py "$SAMPLES" >/dev/null
rm -rf "$OUT"
bidbench init "$OUT" --name "示例综合体项目"
dates=(2026-07-02 2026-08-02 2026-09-15 2026-10-08 2026-10-20)
files=("示例综合体投标图纸.zip" "示例综合体投标图纸0802.zip" "0915修改.zip" "1008补充.zip" "1020补充")  # 最后一批是文件夹
for i in 0 1 2 3 4; do
  echo; echo "===== 收图：${files[$i]} ====="
  bidbench -p "$OUT" receive "$SAMPLES/${files[$i]}" --date "${dates[$i]}"
  bidbench -p "$OUT" recognize --executor simulated
  if [ "$i" = 1 ]; then
    bidbench -p "$OUT" baseline "3层结构模型" --sheets 结施-01,结施-04,建施-03
  fi
  bidbench -p "$OUT" build
done
bidbench -p "$OUT" export current
bidbench -p "$OUT" export mirror
echo; echo "完成。启动工作台：bidbench -p $OUT serve"
