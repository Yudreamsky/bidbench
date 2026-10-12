# 开发约定

- 面向用户的文字（CLI 输出、报告、界面、错误信息）一律用简体中文；代码标识符用英文。
- 改动后运行 `pytest -q`，涉及流水线的改动再跑一次 `./samples/run_demo.sh /tmp/demo`。
- 测试不往真实的用户目录和系统临时文件夹里写东西：`tests/conftest.py` 已把设置目录和 `tempfile` 指到每个测试自己的目录。程序自己建的临时文件夹，失败时要删掉；要留到以后的（如更新下载的安装包）要有清理的时机。
- 数据库只存原始登记和人工决定；台账、事实、问题等派生结果放在 `bidbench/state.py` 里每次重新计算，不要落库。
- 识别结果的格式以 `bidbench/spec/schema.json` 为准；改格式要同步改 `spec/任务说明.md`、模拟执行器和测试。
- 拿不准的判断（版本、作废、冲突）不要自动拍板，生成问题并给出决定选项。
- `bidbench/web/index.html` 是本地工具，不加载任何外部资源（字体、CDN），图纸不出电脑。
- 新的到图情况先在 `samples/make_sample.py` 里加一个合成案例，再写对应测试。
- 识别执行器只读任务包、只写 `results/`；新执行器放在 `bidbench/executors/`，逐页流程用 `claude.run_pages`，测试用假的 API 客户端或假的 Codex（`tests/test_codex_executor.py` 的 FakeCodex），不联网。
- 调用外部命令行程序（解压工具、杀毒软件、Codex）都加 `**proc.NO_WINDOW`，桌面版才不会弹出黑色命令行窗口。
- Claude 执行器的提示词在 `bidbench/executors/claude.py` 的 `SYSTEM_PROMPT`，改识别要求时与 `spec/任务说明.md` 保持一致。
- AI 助手的工具都在 `bidbench/assistant.py`，MCP 只是一层注册（`bidbench/mcp_server.py`）。改动类工具只能写 `proposals` 表，确认、驳回不得暴露给 AI。工作台的 AI 助手面板（`bidbench/chat.py`）让 Codex 通过同一个 MCP 服务查询和提议，不另开后门；助手对软件本身的意见只能走 `propose_improvement`，确认后进本机的改进建议清单（`bidbench suggestions`），不给它改代码或碰 GitHub 的能力。
- 修改已有数据表结构时：`store.SCHEMA_SQL` 写成最新样子，`SCHEMA_VERSION` 加 1，在 `MIGRATIONS` 里写升级 SQL，并加迁移测试。
- 风险文件规则在 `bidbench/risk.py`，杀毒软件对接在 `bidbench/antivirus.py`。样例和测试里的风险文件必须是无害占位，不要写入 EICAR 等真实病毒特征（会被本机杀毒软件拦截）；杀毒软件测试用假的扫描输出。
- 收图、识别、导出等日常操作写在 `bidbench/ops.py`（输出走 log 回调），命令行和工作台后台任务都调用它，不要在两边各写一份。
- 日志：会改动项目的新命令或新后台任务，要记进项目操作日志（`bidbench/oplog.py`；命令行在 `cli._op` 里登记，工作台在 `server.PROJECT_JOBS` 里登记）；人工决定、提议已在数据库里，查看时合并，不要重复记。程序内部情况用 `logging.getLogger(__name__)` 写排错日志（`bidbench/debuglog.py`），不得写入图纸内容、识别出的文字、API 密钥和密码，外部命令用 `debuglog.safe_command()` 去掉密码。
- 工作台的改动类接口都要校验令牌（`X-Bidbench-Token`）和 Host 头；新接口放进 `server.py` 已有的 POST 分发里，不要绕过。
- 安装包相关在 `packaging/` 和 `.github/workflows/windows-installer.yml`；新增依赖时确认 `packaging/bidbench.spec` 能打进去。高速迭代期 PR 上不打包，合并到 main 后自动打包发布，打包出问题在 main 上修。
