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
- “帮我起草答疑问题清单。”
- “把‘机房层不计入层数’记成项目笔记。”（会生成提议，等你确认）

## 工具一览

| 类别 | 工具 |
| --- | --- |
| 全局 | `project_overview`、`list_batches`、`batch_diff`、`compare_batches` |
| 图纸 | `list_sheets`、`get_sheet`、`get_page`、`search` |
| 事实与问题 | `list_facts`、`get_fact`、`floor_coverage`、`list_issues`、`draft_qa`、`list_baselines` |
| 其他 | `estimate_recognition`（只估算，不运行）、`list_notes`、`list_proposals` |
| 提议（需确认） | `propose_issue_resolution`、`propose_fact_confirmation`、`propose_baseline`、`propose_note` |

## 确认提议

- 工作台：问题中心最上方的“AI 助手提议”，点确认或驳回。
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
