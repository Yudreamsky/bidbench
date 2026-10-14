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

## Codex

在 `~/.codex/config.toml` 里加入：

```toml
[mcp_servers.bidbench]
command = "/完整路径/bidbench"
args = ["-p", "/图纸项目目录", "mcp"]
```

## 可以这样问

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
| 建模找图 | `find_sheets_for_task`、`related_sheets`、`locate`（见下） |
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
