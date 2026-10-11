# bidbench 投标图纸工作台

面向 AI agent 的投标图纸数据底座，配一个本地可视化工作台。MVP 重点解决两类最容易出错的问题：

- **缺漏图纸**：目录对账、引用悬空、编号断号、藏在内层压缩包或加密包里的图
- **误用旧版本图纸**：逐张图判断现行版，识别“改图不升版”，建模任务锁定图纸基线并在升版时提醒

> 当前状态：流水线完整可用，**图纸识别暂由“模拟执行器”代替**。它只读取 PDF 文字层并按规则抽取，扫描件无法识别。
> 真实的多模态识别通过“识别任务包”交给 Codex 等外部 agent 或以后的内置 agent，输出格式相同，其余环节不用改。

## 快速体验

```bash
pip install -e ".[dev]"
./samples/run_demo.sh demo-project      # 生成 4 批合成图纸，逐批收图、识别、计算
bidbench -p demo-project serve          # 打开 http://127.0.0.1:8765/
```

演示数据覆盖：目录列出但缺失的图、引用不存在的图、合订本、整套重发中消失的图、内层压缩包补来的图、加密压缩包、
同版次不同内容、设计变更单、GBK 文件名的压缩包、事实冲突（地上层数 20 / 21）、建模基线过期。

## 日常用法

```bash
bidbench init 项目目录 --name "某某项目"
cd 项目目录
bidbench receive ~/下载/投标图纸0915.zip --date 2026-09-15   # 每收到一个压缩包就登记一次
bidbench tasks                                               # 生成识别任务包，交给外部 agent
bidbench import recognition/tasks/<任务包>/results           # 校验并导入识别结果
bidbench recognize                                           # 或：用模拟执行器直接识别并导入
bidbench build                                               # 计算台账、缺图、事实层，写报告
bidbench baseline "3层结构模型" --sheets 结施-01,结施-04     # 开始建模前锁定图纸
bidbench export current                                      # 导出只含现行版的图纸文件夹
bidbench export mirror                                       # 导出给 agent 读的数据镜像
bidbench serve                                               # 本地工作台
bidbench build --as-of 2                                     # 还原到第 2 批时的状态
```

人工决定可以在工作台里点按钮完成，也可以用命令：

| 决定 | 命令 |
| --- | --- |
| 同版次内容不同，采用其中一份 | `bidbench decide sheet_pick "电施-01\|B" <page_id>` |
| 指定现行版次 | `bidbench decide sheet_current 结施-04 C` |
| 整套重发中消失的图：已作废 / 仍有效 | `bidbench decide sheet_void 建施-05 voided`（或 `keep`） |
| 缺图：列入答疑 / 不需要 | `bidbench decide missing 建施-09 ask`（或 `not_needed`） |
| 确认事实的值 | `bidbench decide fact 地上层数 "20 层" --note "结施-01 计入了机房层"` |

## 流水线

```
收图（批次登记）→ 递归解包 → 三级去重 → 拆页与文字层 → 识别任务包 → 执行器 → 校验导入
   → 图纸台账与版本合并 → 批次性质判断 → 缺图检查 → 关系图谱 → 事实层 → 问题中心 / 报告 / 导出
```

| 环节 | 说明 | 代码 |
| --- | --- | --- |
| 收图与解包 | 原件只读保存；zip / tar 原生支持，7z / rar 可选；GBK 文件名修复；加密、损坏、分卷、缺工具都登记为问题 | `bidbench/ingest.py` |
| 去重 | 文件级 SHA-256；页面级像素指纹（合订本中的一页能对上单独的 PDF）；内容级差值哈希 + 文字相似度 | `bidbench/pdfpages.py` |
| 识别 | 任务包 + `spec/schema.json` 约定输出；执行器可替换；导入前严格校验 | `bidbench/recognition.py`、`bidbench/executors/` |
| 派生状态 | 台账、版本链、现行版、冲突、作废还是漏发、缺图、关系、事实层、基线 | `bidbench/state.py` |
| 报告与导出 | 解包报告、到图差异报告、图纸台账、缺图清单、答疑问题清单、项目事实；现行图纸文件夹；数据镜像 | `bidbench/reports.py`、`bidbench/export.py` |
| 工作台 | 图纸知识图谱、事实思维导图、问题中心，可回看任意批次 | `bidbench/server.py`、`bidbench/web/index.html` |

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
```

项目目录含有图纸，不要提交到仓库（`.gitignore` 已排除常见位置）。

## 已知限制

- 模拟执行器只认文字层里“图号 / 图名 / 版次 …”这类标签写法，真实图纸需要多模态执行器。
- 图谱布局是按专业分区的网格，图纸多时连线会交叉，靠聚焦模式和筛选缓解。
- 楼层覆盖检查、DWG 内容解析、多人协作暂未实现。
