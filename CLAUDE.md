# 开发约定

- 面向用户的文字（CLI 输出、报告、界面、错误信息）一律用简体中文；代码标识符用英文。
- 改动后运行 `pytest -q`，涉及流水线的改动再跑一次 `./samples/run_demo.sh /tmp/demo`。
- 数据库只存原始登记和人工决定；台账、事实、问题等派生结果放在 `bidbench/state.py` 里每次重新计算，不要落库。
- 识别结果的格式以 `bidbench/spec/schema.json` 为准；改格式要同步改 `spec/任务说明.md`、模拟执行器和测试。
- 拿不准的判断（版本、作废、冲突）不要自动拍板，生成问题并给出决定选项。
- `bidbench/web/index.html` 是本地工具，不加载任何外部资源（字体、CDN），图纸不出电脑。
- 新的到图情况先在 `samples/make_sample.py` 里加一个合成案例，再写对应测试。
- 识别执行器只读任务包、只写 `results/`；新执行器放在 `bidbench/executors/`，测试用假的 API 客户端，不联网。
- Claude 执行器的提示词在 `bidbench/executors/claude.py` 的 `SYSTEM_PROMPT`，改识别要求时与 `spec/任务说明.md` 保持一致。
