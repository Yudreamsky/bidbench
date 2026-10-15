# 接入 AI 助手（MCP）

`bidbench mcp` 把一个项目作为 MCP 工具集提供给 agent。助手可以查询全部内容，但**只能提议**改动：
处理问题、确认事实、锁定基线、记录项目笔记，都要你在工作台或命令行确认后才生效。

## 准备

**安装版**（用安装包装的）已经带了 MCP，`bidbench.exe` 在
`%LOCALAPPDATA%\Programs\bidbench\bidbench.exe`（安装时勾选了“加入 PATH”的话，直接写 `bidbench` 也行）。

**源码版**：

```bash
pip install -e ".[mcp]"          # MCP Python SDK，1.x 和 2.x 都支持
bidbench -p 项目目录 mcp           # 能启动即可（它通过标准输入输出通信，按 Ctrl+C 退出）
```

下面的配置里，`bidbench` 最好写成**完整路径**，因为桌面应用不会帮你激活虚拟环境：
安装版是 `C:\Users\你\AppData\Local\Programs\bidbench\bidbench.exe`，
源码版 Windows 是 `项目源码\.venv\Scripts\bidbench.exe`，macOS / Linux 是 `项目源码/.venv/bin/bidbench`。

## Claude Code

```bash
claude mcp add bidbench -- /完整路径/bidbench -p "/图纸项目目录" mcp
```

## Claude 桌面版

编辑 `claude_desktop_config.json`（设置 → 开发者 → 编辑配置），加入：

```json
{
  "mcpServers": {
    "bidbench": {
      "command": "C:\\Users\\你\\AppData\\Local\\Programs\\bidbench\\bidbench.exe",
      "args": ["-p", "D:\\投标\\示例项目", "mcp"]
    }
  }
}
```

保存后重启桌面版。

## Codex（命令行、桌面版、编辑器插件）

三者共用一份设置 `~/.codex/config.toml`（Windows 是 `C:\Users\你\.codex\config.toml`）。**不用手写**，两种办法任选：

- 工作台：打开项目 → AI 助手 → 点“接到 Codex”。
- 命令行：`bidbench -p "项目目录" connect-codex`（加 `--print` 只显示、不写）。

它会写入下面这样一节（路径按本机自动填），已有的 bidbench 一节整节替换，别的设置不动，原文件备份成 `config.toml.bak`。
同一时间只接一个项目，换项目再点一次。写完**重新打开 Codex**，在 Codex 里输入 `/mcp` 能看到 bidbench 就是接上了。

```toml
[mcp_servers.bidbench]
command = "C:\\Users\\你\\AppData\\Local\\Programs\\bidbench\\bidbench.exe"
args = ["-p", "D:\\投标\\示例项目", "mcp"]
startup_timeout_sec = 60     # 大项目第一次算台账要十几秒，Codex 默认只等 10 秒
tool_timeout_sec = 180
env = { BIDBENCH_CONFIG_DIR = "C:\\Users\\你\\AppData\\Roaming\\bidbench", PYTHONUTF8 = "1" }
```

手写时注意：TOML 双引号里的反斜杠要写两遍（`\\`），或者整段路径改用单引号 `'D:\投标\示例项目'`。

## 让 Codex 照图建模

bidbench 不画模型，它负责告诉 Codex：建某一层某个专业该看哪些图、图上写的“详见”指到哪、缺了哪些图、哪些被变更单改过。
建议开两个 Codex：一个建模，一个检查（单开对话，不看建模过程）。建模的把每个构件的依据（图号、版次、page_id、bbox、读到的数值）
记进 `建模依据.json`，检查的用 `check_basis` 逐条对照现行图纸，再对数、抽查。两段现成的第一句话见 [建模提示词.md](建模提示词.md)。

`check_basis(items)` 每条构件返回“对得上 / 要复核 / 不合格”和原因：图在不在台账、是不是现行版、page_id 是不是这张图的现行页、
有没有没按它改的变更单（改过的写进 `notices_applied`）和没处理的问题、`value` 在原图那块地方找不找得到。
图纸升版、收到变更单以后，把整份清单再核一遍，“要复核”的就是受影响的构件。

图纸里的尺寸靠识别读出来，目前给到的是图号、图名、楼层、轴线范围、详图编号、引用关系和文字，**不是 CAD 几何**；
精确的尺寸和定位还是要对照原图（`locate` 给出本机文件和页码）。

## 和 Revit MCP 一起用

bidbench 负责“看图”（该看哪些图、现行版、引用、缺图、变更），Revit MCP 负责在 Revit 里建模和翻模。两个都写进 Codex 的设置：
bidbench 一节用“接到 Codex”写入，Revit 一节照 revit-mcp 的 `clients/codex-config.example.toml` 加上（要建模把
`REVIT_MCP_PROFILE` 设成含 `modeling`）。两节互不影响，一个 Codex 两边都能调用。

翻模的顺序：

1. `find_sheets_for_task` 找到这一层这个专业的主图。
2. `cad_file(图号)` 拿到这张图现行版的 DXF：DWG 用安装包附带的 LibreDWG（dwg2dxf）转好，放在项目的 `exports/dxf`。
   返回里 `where` 是这张图在模型空间还是哪个布局，`same_file_sheets` 是同一个 DWG 里的其他图（翻模只取这一块），
   `xrefs` 是外部参照（轴网、底图常在里面，收到了的也转好）。只有 PDF 的图会报错，可以列进答疑。
3. Revit MCP 的 `read_cad(dxf)` 看图层，和你确认哪层是轴网、柱、墙、梁，再 `cad_to_model` 翻模。
4. 每个构件把依据（图号、版次、`page_id`，`cad_file` 返回里都有）记进 `建模依据.json`。
5. 检查的 Codex：Revit MCP 的 `compare_cad_with_model` 对几何，bidbench 的 `check_basis` 对出处和版本。



- “这个项目现在缺哪些图？哪些可以直接列进答疑？”
- “结施-04 升版影响了什么？3 层结构模型要不要更新？”
- “地上层数到底是 20 还是 21？把证据列出来。”
- “我要建 3 层结构梁，该看哪些图？”（`find_sheets_for_task`）
- “建施-08 楼梯详图被哪些图引用了？再往外找一层。”（`related_sheets`）
- “二层梁配筋图上写的‘详见结施-07’在图上哪里？”（`locate`）
- “帮我起草答疑问题清单。”
- “把‘机房层不计入层数’记成项目笔记。”（会生成提议，等你确认）

## 工具一览

| 类别 | 工具 |
| --- | --- |
| 全局 | `project_overview`、`list_batches`、`batch_diff`、`compare_batches` |
| 图纸 | `list_sheets`、`get_sheet`、`get_page`、`search` |
| 建模找图 | `find_sheets_for_task`、`related_sheets`、`locate`（见下）、`check_basis`（核对建模依据）、`cad_file`（转出 DXF 给 Revit 翻模） |
| 事实与问题 | `list_facts`、`get_fact`、`floor_coverage`、`list_issues`、`draft_qa`、`list_baselines` |
| 其他 | `estimate_recognition`（只估算，不运行）、`list_notes`、`list_proposals` |
| 提议（需确认） | `propose_issue_resolution`、`propose_fact_confirmation`、`propose_baseline`、`propose_note`、`propose_improvement`（工作台本身的改进建议，确认后记进改进建议清单，`bidbench suggestions` 查看） |

## 建模找图

建模的 AI 不用一张张翻卡片，按这个顺序查：

1. `find_sheets_for_task(floor, discipline, keyword)`：例如 `floor="3F", discipline="结构", keyword="梁"`。
   楼层可以写 3F、3层、三层、B1、屋面，或项目图名里的写法（Level 03）；关键词几个用空格隔开。
   每张图有 `role`：

   | role | 是什么 |
   | --- | --- |
   | 主图 | 楼层、专业、关键词都对得上；这一层的平面图也算 |
   | 按图名找到 | 没写楼层、图名有关键词的图（门窗表、梁柱节点详图） |
   | 通用图 | 设计说明、没写楼层的柱平面等 |
   | 引用到的图 | 主图上写了“详见”的图（`why` 写着哪张图哪句话） |
   | 引用它的图 | 没给楼层时才有：写了“详见”主图的图，知道详图用在哪 |
   | 同层其他图 | 楼层、专业对得上，图名没有关键词，按需看 |

   另有 `missing`（引用到但没发来的图）、`notices`（改过这些图的变更单）、`issues`（相关问题）。只给现行版。
2. `related_sheets(no, depth, types, direction)`：从一张图或一张变更单往外走。关系种类：引用、跨专业引用、目录列出、变更单、
   同层、通用图、共用参照（DWG 里用了同一个外部参照）；同层、通用图、共用参照只从起点算一步。`via` 写着连过来的关系（“A → B” 是 A 上写了 B）、
   原文、`page_id` 和位置 `bbox`，以及 `form`（引用长什么样：详图索引、剖切、立面、文字引用、变更涉及、目录列出）、`mark`（详图或剖面编号）、
   `target`（被指那张图上同编号的那块详图的 `page_id` 和 `bbox`，合订本里也能直接跳过去）。
3. `locate(page_id, bbox)`：这页属于哪张图哪一版（不是现行版会提醒）、本机文件路径和第几页；给了 `bbox` 再读出那块地方的文字和引用。
   `bbox` 是 `[x0, y0, x1, y1]`，相对整页 0~1，原点左上角，`via` 里的可以直接用。

关系从看图识别来：“详见”文字、圆圈详图索引、剖切符号、变更单条文写到的图、目录条目，加上按楼层算的同层、通用图（种类见 `docs/图纸字段.md` 9.6）。
每张图还带 `covers`（分区和轴线范围，如“1~12/A~F 轴”）和 `details`（图上的详图编号和名称）。DWG 里的内容等读 DWG 做好以后接上。

不接 MCP 的 agent 可以读数据镜像（`bidbench export mirror`）里的 `relations.json`：内容和上面的工具同一份。

## 确认提议

- 工作台：问题中心最上方的“AI 助手提议”，点确认或驳回。工作台开着时，外部助手新提的提议十几秒内会自动出现，不用重新打开项目。
- 命令行：

  ```bash
  bidbench proposals            # 查看待确认的提议
  bidbench confirm 3            # 确认第 3 条
  bidbench reject 4 --note 原因  # 驳回第 4 条
  bidbench notes                # 查看已确认的项目笔记
  ```

确认时会重新核对：如果问题已经被处理、选项已经变化，提议自动失效，不会误执行。
由助手提议、你确认的决定，记录人写作“AI 助手建议，我确认”，理由一并保存。

## 数据去向

MCP 服务只在本机运行。但你用的 agent（Claude、Codex 等）会把工具返回的内容发给它背后的模型服务。
工具返回的是台账、事实、识别出的文字等，**不包含图纸图片**；如果项目有保密要求，请先确认可以这样使用。
