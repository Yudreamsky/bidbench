# bidbench 投标图纸工作台

面向 AI agent 的投标图纸数据底座，配一个本地可视化工作台。MVP 重点解决两类最容易出错的问题：

- **缺漏图纸**：目录对账、引用悬空、编号断号、楼层覆盖、藏在内层压缩包或加密包里的图
- **误用旧版本图纸**：逐张图判断现行版，识别“改图不升版”，建模任务锁定图纸基线并在升版时提醒

> 识别有三种执行器，输出格式相同（`bidbench/spec/schema.json`），可以随时替换：
> - **Claude 执行器**（内置）：调用 Claude 多模态识别，分经济、强两档，见下文“图纸识别”
> - **外部 agent**：把识别任务包交给 Codex 等 agent 的目标模式，见 [docs/外部执行器.md](docs/外部执行器.md)
> - **模拟执行器**：只读 PDF 文字层、按规则抽取，用于演示和测试，扫描件无法识别

## 安装（Windows）

到 [发布页](https://github.com/Yudreamsky/bidbench/releases) 下载最新的 `bidbench-setup-b*.exe`，双击安装，
开始菜单里打开“投标图纸工作台”。这是一个独立窗口，收图、识别、导出、设置都在里面点按钮完成；
有新版本时点“更新并重启”就能升级。详见 [docs/安装与更新.md](docs/安装与更新.md)。

下面是源码版的用法（开发、或在 macOS / Linux 上用）。

## 快速体验

```bash
pip install -e ".[dev]"
./samples/run_demo.sh demo-project      # 生成 5 批合成图纸（4 个压缩包 + 1 个文件夹），逐批收图、识别、计算
bidbench -p demo-project serve          # 浏览器打开 http://127.0.0.1:8765/
bidbench -p demo-project desktop        # 或者开独立窗口（需要 pip install -e ".[desktop]"）
```

演示数据覆盖：目录列出但缺失的图、引用不存在的图、合订本、整套重发中消失的图、内层压缩包补来的图、加密压缩包、
同版次不同内容、设计变更单、GBK 文件名的压缩包、事实冲突（地上层数 20 / 21）、建模基线过期、
风险文件（acad.lsp、双扩展名程序、带宏的表格、带脚本的 PDF，内容都是无害的占位）、
直接给文件夹的补图（夹着压缩包、系统缩略图缓存和 Office 临时锁文件）。

## 日常用法

```bash
bidbench init 项目目录 --name "某某项目"
cd 项目目录
bidbench receive ~/下载/投标图纸0915.zip --date 2026-09-15   # 每收到一批图纸就登记一次
bidbench receive D:/共享盘/1020补充 --date 2026-10-20         # 也可以是一个文件夹（连子文件夹一起收）
bidbench receive 建筑.zip 结构.zip 说明.pdf --date 2026-10-21  # 或同一次收到的几个文件，算同一批
bidbench tasks                                               # 生成识别任务包，交给外部 agent
bidbench import recognition/tasks/<任务包>/results           # 校验并导入识别结果
bidbench recognize                                           # 或：用模拟执行器直接识别并导入
bidbench build                                               # 计算台账、缺图、事实层，写报告
bidbench baseline "3层结构模型" --sheets 结施-01,结施-04     # 开始建模前锁定图纸
bidbench export current                                      # 导出只含现行版的图纸文件夹
bidbench export mirror                                       # 导出给 agent 读的数据镜像
bidbench serve                                               # 本地工作台
bidbench build --as-of 2                                     # 还原到第 2 批时的状态
bidbench log                                                 # 操作日志：什么时候收了什么、做过哪些决定
```

人工决定可以在工作台里点按钮完成，也可以用命令：

| 决定 | 命令 |
| --- | --- |
| 同版次内容不同，采用其中一份 | `bidbench decide sheet_pick "电施-01\|B" <page_id>` |
| 指定现行版次 | `bidbench decide sheet_current 结施-04 C` |
| 整套重发中消失的图：已作废 / 仍有效 | `bidbench decide sheet_void 建施-05 voided`（或 `keep`） |
| 缺图：列入答疑 / 不需要 | `bidbench decide missing 建施-09 ask`（或 `not_needed`） |
| 风险文件：安全 / 有风险 | `bidbench decide risk <文件编号> safe`（或 `dangerous`），编号见收图输出或问题中心 |
| 确认事实的值 | `bidbench decide fact 地上层数 "20 层" --note "结施-01 计入了机房层"` |

## 图纸识别

有两种识别方式，工作台「识别」里可以选，也可以选模型（会记住上次的选择）：

| 识别方式 | 费用 | 需要 |
| --- | --- | --- |
| Codex | 不另外花钱，占用 ChatGPT 账号的 Codex 用量额度 | 本机装好 Codex 命令行并用 ChatGPT 账号登录 |
| Claude API | 按用量付费，先估算花费 | Anthropic API 密钥 |

### 用 Codex 识别

```bash
npm install -g @openai/codex@latest    # 装好或更新 Codex 命令行（gpt-6.1-sol 需要 0.162 以上）
codex login                            # 用 ChatGPT 账号登录
bidbench recognize --executor codex --dry-run               # 只生成任务包并估算用时
bidbench recognize --executor codex                         # 经济档：gpt-6.1-sol，中等思考
bidbench recognize --executor codex --tier strong --scope conflict   # 强档：加局部切片，深入思考
bidbench recognize --executor codex --model gpt-6-astra     # 换模型（能用的模型见 codex debug models）
```

- 每页调用一次 `codex exec`，一页约半分钟到一分钟，默认同时识别 3 页。识别要求、格式校验、失败重试、续跑、逐页记录都和 Claude 一样。
- 安全：Codex 在只读沙箱里运行，不读你自己的 Codex 设置（不带上全权限、插件和 MCP），不保存会话记录，
  工作目录是只放了这一页图片的临时文件夹。PATH 里的 Codex 和 Codex 桌面版自带的命令行，自动用版本新的那个。
- 模型不能用、没登录、额度用完时第一页就停下，并说明怎么办；任务包保留，之后用 `--task` 续跑。
- 图纸页面会通过 Codex 发送给 OpenAI 识别。图纸涉密时，先确认这符合项目的保密要求。

### 用 Claude API 识别

先配置 Claude API 凭据（任选其一）：

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # 或者：ant auth login
pip install -e ".[claude]"
```

推荐的分级用法：先用经济档全量识别，再用强档复核有问题的页面。

```bash
bidbench recognize --executor claude --dry-run              # 只生成任务包并估算花费
bidbench recognize --executor claude                        # 经济档：claude-haiku-5-5，识别新页面
bidbench build
bidbench recognize --executor claude --tier strong --scope conflict   # 强档复核冲突页面
bidbench recognize --executor claude --tier strong --scope weak       # 强档复核识别不完整的页面
```

| 档位 | 模型 | 送给模型的内容 | 适合 |
| --- | --- | --- | --- |
| economy（默认） | claude-haiku-5-5 | 整页缩略图、图签放大图、文字层 | 首次全量识别 |
| strong | claude-opus-5-5 | 再加 3×3 局部切片，effort 为 high | 冲突页、低置信页、重点图纸 |

| 范围 `--scope` | 挑选的页面 |
| --- | --- |
| new（默认） | 尚未识别的页面，以及只有模拟执行器结果的页面 |
| weak | 识别不完整：模拟结果、有问题记录、缺图号或图签把握低于 0.6 |
| conflict | 涉及版本冲突、版次倒退或事实冲突的页面 |
| unrecognized | 尚未识别的页面 |
| all | 全部页面 |

说明：

- 像素完全相同的页面只识别一次，结果自动复用；整套重发时没变的图不会重复花钱。
- 使用结构化输出约束格式，导入前再用完整 schema 校验；没通过就带着错误重试一次。若 API 不接受这份 schema，会自动改为在提示里附格式说明，结果照样校验。
- 强档模型启用了服务端拒答回退（`fallbacks: "default"`），被安全分类器拒绝时由服务端换模型重跑。
- 系统提示开启了提示缓存。每次运行在任务包的 `results/run_log.md` 和 `usage.json` 里记录用量和估算花费。
- 识别过程逐页显示（识别出的图号、用时、用量，失败的原因），同时追加到任务包的 `results/progress.log`。
- 中断后用 `--task <任务包>` 续跑，已完成的页面会跳过。凭据缺失、模型不可用时第一页就会停下。
- 图纸页面会发送到 Anthropic API 识别。图纸涉密时，先确认这符合项目的保密要求。

## 更新

安装版见 [docs/安装与更新.md](docs/安装与更新.md)。下面是用 git clone 安装的源码版。

工作台检测到 GitHub 上有新版本时，顶部会出现“有新版本”，点开能看到更新内容，点“更新并重启”即可；
页头的版本号也能打开这个面板。命令行：

```bash
bidbench update --check     # 只检查
bidbench update             # 更新（会先列出更新内容并确认）
bidbench update --rollback  # 回到上一次更新之前的版本
```

- 需要用 `git clone` 安装，跟随 `main` 分支（每个 PR 合并前都通过 CI）。沿用本机 git 的登录凭据。
- 只做快进更新：本地改过代码、不在 `main`、与远程分叉时停下，不覆盖、不自动合并。
- 依赖有变化时自动补装（保留你已装的可选功能，如 `claude`、`mcp`）；Windows 上在工作台退出后再装，避开被占用的 `bidbench.exe`。
- 项目数据不在代码目录里，更新不会碰到。数据库结构升级时会先备份为 `bidbench.sqlite.bak-v版本-时间`。
- 更新过程记录在代码目录的 `.git/bidbench_update.log`。

## 让 Codex 帮忙验收

`docs/验收清单.md` 列出了要核对的功能（测试、演示流水线、收图、台账与问题、日志、工作台接口、识别、安全约束）。
运行下面的命令，Codex 会在一个临时的代码副本里按清单实际操作，写出验收报告；它只验证、不改代码，只用合成数据，
不调用 Claude API，也不真的用 Codex 识图：

```bash
.venv/Scripts/python tools/codex_accept.py                # 全部条目，约十几分钟
.venv/Scripts/python tools/codex_accept.py --items A1,D1  # 只验部分条目
```

报告在 `验收报告/` 里（不入库），终端也会打印每一项的结果。验收的是已提交的版本，未提交的改动不在范围内。

## 日志

| 日志 | 内容 | 在哪里看 |
| --- | --- | --- |
| 操作日志 | 收图、识别、导出、安全扫描等每次操作（命令行和工作台都记）：时间、结果、出错原因、当时的完整输出；人工决定、锁定基线、AI 助手的提议和确认也合在同一条时间线里 | 工作台上方的「日志」；`bidbench log`（`--lines` 带上每次的输出）；文件在项目的 `logs/` 里，按月一个，跟着项目走 |
| 识别过程日志 | 每识别完一页一行：识别出的图号和图名、用时、用量；失败、重试的原因 | 识别时实时显示；任务包的 `results/progress.log` |
| 排错日志（给开发用） | 程序内部的运行记录：调用解压工具、杀毒软件的命令和返回结果，更新下载，AI 助手调用了哪些工具，出错时的完整错误信息 | 工作台「设置」→「打开排错日志文件夹」；Windows 上在 `%APPDATA%\bidbench\logs`，每天一个文件，保留 14 天 |

- 操作日志只追加、不改写；半途断电留下的残行会被跳过。关窗口时还有操作没做完，会记一笔“中断”。
- 排错日志不记图纸内容、识别出的文字、API 密钥和压缩包密码（命令里的密码写成 `***`），会有文件路径和出错信息。
  出了问题，把这个文件夹里当天的文件发给开发者即可。设置环境变量 `BIDBENCH_DEBUG=1` 可以记得更细。

## AI 助手

### 在工作台里直接问（Codex）

工作台上方的「AI 助手」：用本机的 Codex（ChatGPT 订阅额度，不另外花钱）回答这个项目的问题，比如“还缺哪些图？”
“地上层数到底是多少？”“帮我起草答疑问题清单”。

- 它用的是下面 MCP 那一套工具：查询全部只读；**处理问题、确认事实、锁定基线、记笔记只会生成提议**，
  提议卡片就显示在回答下面，你点“确认”才生效（问题中心的“AI 助手提议”里也有）。
- 回答过程实时显示它查了什么；回答里的图号可以点，直接跳到图谱上那张图。可以连续追问，「新对话」重新开始。
- 每个问题约半分钟到一分钟，可以随时“停止”。模型可选，默认 gpt-6.1-sol，会记住上次的选择。
- 对话记录存在项目的 `logs/AI助手对话.jsonl`，关掉再打开还能接着看；每个问题也记进操作日志。
- 安全：Codex 在只读沙箱里运行，不读你自己的 Codex 设置，不保存会话记录，只能用 bidbench 的工具。
  问题和查到的图纸信息会通过 Codex 发送给 OpenAI，图纸涉密时先确认这符合项目的保密要求。
- **改进建议**：助手用着发现工作台本身的不足（缺工具、数据不对、不好用），可以提一条“改进建议”（也是提议，
  不带项目信息）。你确认后记进本机的改进建议清单，开发者按清单改代码、开 PR；助手自己碰不到代码和 GitHub。
  查看清单：`bidbench suggestions`。

### 接入其他 agent（MCP）

`bidbench -p 项目目录 mcp` 把项目作为 MCP 工具集提供给 Claude 桌面版、Claude Code、Codex 等 agent。
助手能查询图纸、事实、问题、楼层覆盖、批次差异，并起草答疑；**改动只能提议**，在工作台“AI 助手提议”
或用 `bidbench confirm <编号>` 确认后才生效。接入方法见 [docs/MCP.md](docs/MCP.md)。

## 压缩包支持

| 格式 | 怎么解 | 需要安装什么 |
| --- | --- | --- |
| zip（含 GBK 文件名、ZipCrypto 加密）、tar | Python 自带 | 不需要 |
| rar4 / rar5、7z（不加密） | 系统自带的 bsdtar | Windows 10 及以上、macOS 自带，不需要；Linux 装 `libarchive-tools` |
| 加密的 rar | unrar 或 7-Zip | 装 WinRAR 或 7-Zip（Windows 上按默认安装目录和注册表找，装在别的位置也找得到） |
| 加密的 7z（含连文件名都加密的） | py7zr 或 7-Zip | `pip install -e ".[archives]"`（约 20 MB，主要是加密库），或装 7-Zip |

默认安装不增加任何体积。按 bsdtar → unrar → 7-Zip 的顺序尝试，成功即停；任何工具失败都整体作废，不用解了一半的文件。
压缩包里的符号链接一律跳过；含符号链接的压缩包只用 bsdtar 解，防止被链接带到压缩包以外。
一个工具都没有时，解包报告会写明需要装什么。分卷压缩包（`.part1.rar`、`.z01`）目前仍需先手工合并。
中文 Windows 自带的 tar 解不了文件名里带 `Ø` 这类字符的 rar；这时会提示安装 7-Zip 或 WinRAR，装好后重新收图即可。

## 文件夹和多个文件

收图来源除了压缩包，也可以是一个文件夹（连子文件夹一起收），或同一次收到的几个文件（工作台里在对话框中一起选中）。

- 整个文件夹或这几个文件算一个批次；里面的压缩包照样逐层解开、去重、检查风险文件。
  文件夹里直接放的压缩包相当于外层包，压缩包里再套的才算内层。
- 每个文件按内容存进项目，另在 `archives/` 下留一份只读的文件清单，记下原来的位置和结构。
- 不收系统自动生成的文件：`Thumbs.db`、`desktop.ini`、`.DS_Store`、Office 打开文档时留下的 `~$` 临时锁文件。
  符号链接和目录联接不跟进去；项目自己的目录在里面时自动跳过。
- 同一个文件夹内容没变时拒收；设计院在同一个文件夹里改了图，再收一次算新批次，
  按“整套重发”判断，文件夹里不见了的图会问你是作废还是漏发。
- 读不出来的文件（正被其他程序占用、没有权限）登记为“读不出来”，提醒你关掉占用它的程序后重收。

## 文件安全检查

收图时自动检查每个文件，只看文件名和内容特征，**从不打开或运行文件**：

| 检查项 | 风险 |
| --- | --- |
| CAD 自动加载脚本：`acad.lsp`、`acaddoc.lsp`、`acad.fas`、`*.mnl`（含中望、浩辰同类文件） | 高：CAD 打开同目录图纸时自动运行，是 CAD 病毒最常见的传播方式 |
| LISP 脚本里有复制文件、写注册表、启动程序等操作 | 高 |
| 可执行文件、双扩展名（`图纸.pdf.exe`）、文件名含反向显示字符、扩展名和实际内容不符 | 高 |
| Office 文件含 VBA 宏、会从外部地址加载模板 | 高 |
| PDF 含 JavaScript、启动外部程序、Flash | 高（Flash 为中） |
| 其他 CAD 插件（`.vlx` `.dvb` `.arx` 等）、启用宏的格式、AutoCAD 命令脚本 `.scr` | 中 |

- 有本机杀毒软件时一并扫描：Windows 上自动调用系统自带的 Windows Defender，其他系统用 ClamAV（需先 `freshclam` 下载病毒库）。
  只报告不删除；收图后被实时防护隔离的文件也会标出来。`bidbench receive --no-av` 跳过。
- 收图时被杀毒软件拦下的文件（读不出来，或解出后马上不见了）按高风险文件列进问题中心：
  它没有收进项目，如果是图纸，需要发图方重新发一份干净的。解出后再和压缩包的文件清单核对一遍，少了的不会静默漏掉。
- 发现的问题进入问题中心，由你决定“确认安全”或“确认有风险”（后者列入答疑，请发图方核实）。AI 助手不参与这类判断。
- “导出现行图纸”和工作台里打开的 PDF 会去掉脚本、自动动作和附件，图面不变。
- 老项目或规则更新后，运行 `bidbench scan` 按最新规则重查全部文件并做一次杀毒扫描，报告在 `reports/文件安全检查.md`。
- DWG 文件本身不能运行代码，风险在同目录的脚本和插件。建议在 CAD 里把 `SECURELOAD` 设为 1 或 2，只从受信任位置加载脚本。

## 楼层覆盖检查

按图名（“一层平面图”“三层梁配筋图”“地下一层平面图”“屋顶平面图”“3~20层平面图”）读出每张图覆盖的楼层；
图名没写楼层的标准层图，再从“适用楼层 4F~20F”这类说明里读。

| 图类 | 检查方式 |
| --- | --- |
| 建筑平面图 | 按事实层的地上层数、地下层数，加上屋顶，检查每一层都有图 |
| 结构梁、板配筋图，机电平面图 | 只检查已出现楼层之间有没有断档 |

缺层生成一条“楼层覆盖”问题，写明依据、已有哪些层和缺哪些层，可以列入答疑或确认不需要。
层数本身有冲突时会注明“确认后结果可能变化”；缺口变化后，之前的“不需要”不再适用，会重新提示。
标准层图没写适用楼层时，单独提示。

## 流水线

```
收图（批次登记）→ 递归解包 → 三级去重 → 拆页与文字层 → 识别任务包 → 执行器 → 校验导入
   → 图纸台账与版本合并 → 批次性质判断 → 缺图检查 → 关系图谱 → 事实层 → 问题中心 / 报告 / 导出
```

| 环节 | 说明 | 代码 |
| --- | --- | --- |
| 收图与解包 | 压缩包、文件、文件夹或几个文件为一批；原件只读保存；zip / tar 原生支持，rar / 7z 调用系统解压工具；GBK 文件名修复；加密、损坏、分卷、缺工具、读不出来都登记为问题 | `bidbench/ingest.py`、`bidbench/extractors.py` |
| 文件安全 | 静态规则 + 本机杀毒软件；导出时去除 PDF 脚本 | `bidbench/risk.py`、`bidbench/antivirus.py` |
| 去重 | 文件级 SHA-256；页面级像素指纹（合订本中的一页能对上单独的 PDF）；内容级差值哈希 + 文字相似度 | `bidbench/pdfpages.py` |
| 识别 | 任务包 + `spec/schema.json` 约定输出；执行器可替换；导入前严格校验 | `bidbench/recognition.py`、`bidbench/executors/` |
| 派生状态 | 台账、版本链、现行版、冲突、作废还是漏发、缺图、关系、事实层、基线 | `bidbench/state.py` |
| 报告与导出 | 解包报告、到图差异报告、图纸台账、缺图清单、答疑问题清单、项目事实；现行图纸文件夹；数据镜像 | `bidbench/reports.py`、`bidbench/export.py` |
| 工作台 | 图纸知识图谱、事实思维导图、问题中心，可回看任意批次；收图、识别、导出等操作在后台任务里跑 | `bidbench/server.py`、`bidbench/web/index.html`、`bidbench/ops.py` |
| 桌面版与安装包 | 独立窗口（WebView2）；PyInstaller + Inno Setup 打包；从发布仓库下载安装包更新 | `bidbench/desktop.py`、`bidbench/release.py`、`packaging/` |

**数据原则**：数据库只存原始登记（批次、文件、出处、页面、识别结果）和人工决定。台账、事实等派生结果每次重新计算，
所以任意批次的状态都能还原，规则改进后旧项目也能直接受益。

## 项目目录

```
项目/
  bidbench.sqlite      原始登记 + 人工决定
  archives/            收到的压缩包原件（只读）
  blobs/               解出的文件，按内容哈希存放
  recognition/tasks/   识别任务包
  reports/             报告（Markdown）
  exports/             现行图纸文件夹、mirror/ 数据镜像、state.json
  logs/                操作日志（按月一个文件）
```

项目目录含有图纸，不要提交到仓库（`.gitignore` 已排除常见位置）。

## 已知限制

- 模拟执行器只认文字层里“图号 / 图名 / 版次 …”这类标签写法，真实图纸需要多模态执行器。
- 图谱布局是按专业分区的网格，图纸多时连线会交叉，靠聚焦模式和筛选缓解。
- DWG 内容解析、多人协作暂未实现。
