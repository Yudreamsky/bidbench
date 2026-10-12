"""Claude 多模态执行器：读任务包里的页面图片和文字层，调用 Claude 识别，写出与 schema 一致的结果。

分级识别：
- economy：claude-haiku-5-5，整页缩略图 + 图签放大图 + 文字层，适合首次全量识别
- strong： claude-opus-5-5，另加 3×3 局部切片，适合复核冲突页、低置信页和重点图纸

执行器只读任务包、只写 results/，与外部 agent 遵守同一份格式约定。
凭据按 Anthropic SDK 的默认顺序解析（ANTHROPIC_API_KEY 等），代码里不保存密钥。
"""

from __future__ import annotations

import base64
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pymupdf

from ..recognition import load_schema, page_markdown

EXECUTOR = "claude"
INSTRUCTION_VERSION = "0.2"
MAX_IMAGE_EDGE = 1568          # 长边超过这个尺寸会被服务端缩小，先在本地缩好
MAX_TEXT_CHARS = 40000         # 文字层过长时截断，并在提示里写明
MAX_TOKENS = 32000
BODY_KEYS = ("page_kind", "title_block", "references", "catalog_entries", "content", "issues")

TIERS = {
    "economy": {"model": "claude-haiku-5-5", "effort": "medium", "tiles": 0},
    "strong": {"model": "claude-opus-5-5", "effort": "high", "tiles": 3},
}
# 每百万 token 的美元价格（输入, 输出），用于估算花费
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# 这些模型支持服务端拒答回退：被安全分类器拒绝时由服务端换模型重跑
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """你是建筑工程投标图纸的识别员。每次会收到一页图纸：整页缩略图、图签区域放大图，可能还有局部切片，以及这一页 PDF 的文字层（逐行文字和位置）。
请把这一页识别成结构化数据，严格按给定的 JSON 格式输出。识别结果会进入图纸台账，用于检查缺漏图纸、判断现行版本、交叉比对工程事实，所以准确比完整更重要。

识别要求：
1. 页面类型 page_kind：图纸、图纸目录、设计说明、变更单、答疑或澄清、封面、其他。
2. 图签 title_block：图号、图名、专业、版次、出图日期、比例、项目名称、设计单位，以及修改记录栏的每一行（版次、日期、修改内容）。图号保持图上写法，只去掉多余空格。title_block.bbox 是图签区域的位置。
3. 引用 references：详图索引符号（圆圈内上方编号、下方图号，或“⑤/建施-12”写法）、文字引用（“详见建施-17”“未注明者详见结施-01”）、图集引用（target_drawing_no 为 null）。逐条记录原文、指向的图号、类型和位置。
4. 图纸目录 catalog_entries：仅目录页填写，逐行提取图号、图名、版次，一行都不能漏；其他页为空数组。
5. 全页内容 content：
   - summary：一两句话说明这张图画的是什么
   - notes：设计说明、附注，按条目分开，label 写条目编号
   - tables：表格，保留表头和每一行
   - key_values：层数、层高、标高、建筑高度、面积、轴网尺寸、构件截面、墙厚、材料强度等级等关键数值，写名称、数值、单位、原文和位置。名称用通用说法，例如“地上层数”“建筑高度”“1F 层高”“3F~20F 层高”“KL3 截面（三层梁配筋图）”“Q1 墙厚”“混凝土强度等级”
   - standards：引用的规范和标准，code 写编号和年版，name 写名称
6. 问题 issues：字迹不清、图签缺失、一页多图、文字层与图像不一致等。

来源与把握程度：
- source：text_layer（来自文字层）、vision（来自看图）、both（两者一致）
- confidence：0~1。0.9 以上表示文字层清楚或两者一致；0.6~0.9 表示看图且图像清晰；0.6 以下表示不确定
- bbox：[x0, y0, x1, y1]，相对整页宽高，取值 0~1，原点左上角；局部切片里看到的内容要换算回整页坐标；无法确定时为 null

硬性规则：
1. 不编造。看不清或找不到的内容填 null，并在 issues 里说明原因。宁可留空，不要猜。
2. 文字层优先。文字层与图像不一致时，以文字层为值、在 issues 里注明“文字层与图像不一致”。
3. 图签字段不存在时，value、source、confidence 都填 null。
4. 只输出这一页的识别结果。"""


# ---------------- 格式 ----------------

_DROP = {"minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern", "multipleOf",
         "$id", "$schema"}


def _api_compatible(node):
    """把完整 schema 转成结构化输出支持的子集：去掉数值和长度约束，oneOf/类型数组改为 anyOf。
    被去掉的约束在本地用完整 schema 校验。"""
    if isinstance(node, list):
        return [_api_compatible(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in _DROP:
            continue
        if k == "oneOf":
            out["anyOf"] = _api_compatible(v)
        elif k == "const":
            out["enum"] = [v]
        else:
            out[k] = _api_compatible(v)
    t = out.get("type")
    if isinstance(t, list):
        rest = {k: v for k, v in out.items() if k not in ("type", "description")}
        branches = []
        for one in t:
            b = {"type": one}
            if one == "array" and "items" in rest:
                b["items"] = rest.pop("items")
            branches.append(b)
        out = {"anyOf": branches, **({"description": out["description"]} if "description" in out else {}), **rest}
    return out


def body_schema() -> dict:
    full = load_schema()
    body = {
        "type": "object",
        "additionalProperties": False,
        "required": list(BODY_KEYS),
        "properties": {k: copy.deepcopy(full["properties"][k]) for k in BODY_KEYS},
        "$defs": copy.deepcopy(full["$defs"]),
    }
    return _api_compatible(body)


def _sanitize(node):
    """修正结构化输出无法强制的约束：把 confidence 限制在 0~1，bbox 不是 4 个数时置空并裁到 0~1。"""
    if isinstance(node, list):
        return [_sanitize(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "confidence" and isinstance(v, (int, float)):
            out[k] = min(1.0, max(0.0, float(v)))
        elif k == "bbox":
            if isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v):
                out[k] = [min(1.0, max(0.0, float(x))) for x in v]
            else:
                out[k] = None
        else:
            out[k] = _sanitize(v)
    return out


# ---------------- 图片 ----------------

def _png_b64(path: Path, clip_frac: tuple | None = None) -> str:
    doc = pymupdf.open(path)
    try:
        page = doc[0]
        r = page.rect
        clip = r if not clip_frac else pymupdf.Rect(
            r.x0 + r.width * clip_frac[0], r.y0 + r.height * clip_frac[1],
            r.x0 + r.width * clip_frac[2], r.y0 + r.height * clip_frac[3])
        s = min(1.0, MAX_IMAGE_EDGE / max(clip.width, clip.height))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), clip=clip, alpha=False)
        return base64.standard_b64encode(pix.tobytes("png")).decode()
    finally:
        doc.close()


def _image_block(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def build_content(task_dir: Path, item: dict, tiles: int) -> list:
    content: list = []
    src = item["source"]
    content.append({"type": "text", "text": (
        f"页面编号 {item['page_id']}，文件 {src['file_path']} 第 {src['page_index']}/{src['page_count']} 页，"
        f"{'有' if src['has_text_layer'] else '没有'} PDF 文字层。")})
    if item.get("image"):
        content += [{"type": "text", "text": "整页缩略图："}, _image_block(_png_b64(task_dir / item["image"]))]
    if item.get("titleblock_image"):
        content += [{"type": "text", "text": "图签区域放大图（页面右下角；图签不在这里时以整页为准）："},
                    _image_block(_png_b64(task_dir / item["titleblock_image"]))]
    if item.get("image") and tiles > 1:
        for row in range(tiles):
            for col in range(tiles):
                frac = (col / tiles, row / tiles, (col + 1) / tiles, (row + 1) / tiles)
                content += [{"type": "text", "text": f"局部切片 第 {row + 1} 行第 {col + 1} 列，对应整页坐标 "
                                                     f"x {frac[0]:.2f}~{frac[2]:.2f}，y {frac[1]:.2f}~{frac[3]:.2f}："},
                            _image_block(_png_b64(task_dir / item["image"], frac))]
    lines = json.loads((task_dir / item["text_lines"]).read_text(encoding="utf-8"))["lines"]
    text = "\n".join(f"[{','.join(f'{x:.3f}' for x in ln['bbox'])}] {ln['text']}" for ln in lines)
    note = ""
    if len(text) > MAX_TEXT_CHARS:
        kept = text[:MAX_TEXT_CHARS].rsplit("\n", 1)[0]
        note = f"\n（文字层共 {len(lines)} 行，过长，这里只给出前 {kept.count(chr(10)) + 1} 行；其余内容请从图像识别，并在 issues 里注明文字层被截断。）"
        text = kept
    content.append({"type": "text", "text": ("PDF 文字层（每行开头是 [x0,y0,x1,y1] 归一化位置）：\n" + text + note)
                    if lines else "这一页没有文字层，请完全依靠图像识别，source 用 vision。"})
    return content


# ---------------- 调用 ----------------

@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, u) -> None:
        with self.lock:
            self.input += getattr(u, "input_tokens", 0) or 0
            self.output += getattr(u, "output_tokens", 0) or 0
            self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
            self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        pin, pout = PRICES[model]
        return (self.input * pin + self.cache_write * pin * 1.25 + self.cache_read * pin * 0.1 + self.output * pout) / 1e6


class RecognitionError(Exception):
    """单页失败：记下来，继续其他页面。"""


class FatalError(Exception):
    """整次运行无法继续：凭据缺失或无效、模型不存在、没有权限。"""


def _fatal(e: Exception) -> str | None:
    name = type(e).__name__
    if name == "AuthenticationError" or (isinstance(e, TypeError) and "authentication" in str(e).lower()):
        return "Claude API 凭据缺失或无效。请设置环境变量 ANTHROPIC_API_KEY（或用 ant auth login 登录）后重试"
    if name == "PermissionDeniedError":
        return f"当前 API Key 没有调用权限：{e}"
    if name == "NotFoundError":
        return f"模型不存在或当前账号不可用：{e}"
    return None


class _Mode:
    """结构化输出是首选；如果 API 拒绝这份 schema，整次运行退回“提示词附格式 + 本地校验”。"""

    def __init__(self):
        self.structured = True
        self.lock = threading.Lock()

    def downgrade(self) -> None:
        with self.lock:
            self.structured = False


def _schema_rejected(e: Exception) -> bool:
    msg = str(e).lower()
    return type(e).__name__ == "BadRequestError" and any(w in msg for w in ("schema", "output_config", "format"))


def _request(client, model: str, effort: str, messages: list, schema: dict, mode: _Mode | None = None):
    mode = mode or _Mode()
    while True:
        structured = mode.structured
        output_config = {"effort": effort}
        msgs = messages
        if structured:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        else:
            hint = ("只输出一个 JSON 对象，不要任何其他文字，必须符合下面的 JSON Schema：\n"
                    + json.dumps(schema, ensure_ascii=False))
            msgs = [{"role": messages[0]["role"], "content": messages[0]["content"] + [{"type": "text", "text": hint}]},
                    *messages[1:]]
        params = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=msgs,
            output_config=output_config,
        )
        try:
            if model in FALLBACK_MODELS:
                stream = client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            else:
                stream = client.messages.stream(**params)
            with stream as s:
                return s.get_final_message()
        except Exception as e:  # noqa: BLE001
            if structured and _schema_rejected(e):
                mode.downgrade()
                continue
            raise


def _parse_json(raw: str) -> dict:
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    if not t.lstrip().startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    return json.loads(t)


def _text_of(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def recognize_page(client, task_dir: Path, item: dict, model: str, effort: str, tiles: int,
                   usage: Usage, validator, schema: dict, mode: _Mode | None = None) -> dict:
    messages = [{"role": "user", "content": build_content(task_dir, item, tiles)}]
    last_error = None
    for _attempt in range(2):  # 第二次只在本地校验失败时，带着错误重来
        msg = _request(client, model, effort, messages, schema, mode)
        usage.add(msg.usage)
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise RecognitionError(f"模型拒绝识别（{cat or '未说明原因'}）")
        if msg.stop_reason == "max_tokens":
            raise RecognitionError("输出超过长度上限，页面内容过多")
        raw = _text_of(msg)
        try:
            body = _parse_json(raw)
            if not isinstance(body, dict):
                raise json.JSONDecodeError("不是对象", raw, 0)
        except json.JSONDecodeError as e:
            raise RecognitionError(f"模型输出不是合法 JSON：{e}") from e
        body = _sanitize({k: body.get(k) for k in BODY_KEYS})
        record = {
            "schema_version": "0.1",
            "page_id": item["page_id"],
            "source": item["source"],
            "run": {"executor": EXECUTOR, "model": getattr(msg, "model", None) or model,
                    "instruction_version": INSTRUCTION_VERSION,
                    "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            **body,
        }
        errs = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
        if not errs:
            return record
        last_error = "；".join(f"{'/'.join(str(x) for x in e.absolute_path) or '(根)'}: {e.message}" for e in errs[:5])
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"上面的输出没有通过校验：{last_error}。请输出修正后的完整 JSON。"},
        ]
    raise RecognitionError(f"两次输出都没有通过校验：{last_error}")


@dataclass
class RunResult:
    done: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    seconds: float = 0.0
    structured: bool = True


def run(task_dir: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
        tiles: int | None = None, workers: int = 4, client=None, progress=None) -> RunResult:
    cfg = TIERS[tier]
    model = model or cfg["model"]
    effort = effort or cfg["effort"]
    tiles = cfg["tiles"] if tiles is None else tiles
    if client is None:
        import anthropic
        client = anthropic.Anthropic(max_retries=4)
    task_dir = Path(task_dir)
    manifest = json.loads((task_dir / "manifest_in.json").read_text(encoding="utf-8"))
    out = task_dir / "results" / "pages"
    out.mkdir(parents=True, exist_ok=True)
    validator = jsonschema.Draft202012Validator(load_schema())
    schema = body_schema()
    res = RunResult(model=model)
    mode = _Mode()
    t0 = time.monotonic()
    todo = []
    for item in manifest["pages"]:
        target = out / f"{item['page_id']}.json"
        if target.exists():
            try:
                if not list(validator.iter_errors(json.loads(target.read_text(encoding="utf-8")))):
                    res.skipped += 1  # 已有合格结果，支持续跑
                    continue
            except json.JSONDecodeError:
                pass
        todo.append(item)

    def one(item):
        rec = recognize_page(client, task_dir, item, model, effort, tiles, res.usage, validator, schema, mode)
        (out / f"{item['page_id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / f"{item['page_id']}.md").write_text(page_markdown(rec), encoding="utf-8")
        return item

    def settle(it, fn):
        try:
            fn()
            res.done += 1
        except RecognitionError as e:
            res.failed.append({"page_id": it["page_id"], "file": it["source"]["file_path"], "error": str(e)})
        except Exception as e:  # noqa: BLE001 - API 错误等，记下后继续其他页面
            fatal = _fatal(e)
            if fatal:
                raise FatalError(fatal) from e
            res.failed.append({"page_id": it["page_id"], "file": it["source"]["file_path"],
                               "error": f"{type(e).__name__}: {e}"})
        if progress:
            progress(res.done + len(res.failed), len(todo))

    # 先单独跑第一页：凭据、模型这类整体性问题在这里暴露，不会让每一页各失败一次
    if todo:
        settle(todo[0], lambda: one(todo[0]))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(one, it): it for it in todo[1:]}
        for f in as_completed(futs):
            settle(futs[f], f.result)
    res.seconds = time.monotonic() - t0
    res.structured = mode.structured
    _write_log(task_dir, res, tier, effort, tiles)
    return res


def _write_log(task_dir: Path, res: RunResult, tier: str, effort: str, tiles: int) -> None:
    u = res.usage
    cost = u.cost(res.model)
    lines = ["# 运行记录", "", f"执行器：{EXECUTOR}　档位：{tier}　模型：{res.model}　effort：{effort}　局部切片：{tiles}×{tiles}" if tiles > 1
             else f"执行器：{EXECUTOR}　档位：{tier}　模型：{res.model}　effort：{effort}　局部切片：无", "",
             f"本次识别 {res.done} 页，跳过已有结果 {res.skipped} 页，失败 {len(res.failed)} 页，用时 {res.seconds:.0f} 秒。", "",
             f"用量：输入 {u.input} tokens，输出 {u.output} tokens，缓存读取 {u.cache_read}，缓存写入 {u.cache_write}。"
             + (f" 估算花费约 ${cost:.4f}。" if cost is not None else "")]
    if not res.structured:
        lines += ["", "注意：API 不接受这份结构化输出格式，本次改为在提示里附上格式说明，结果全部经过本地校验。"]
    if res.failed:
        lines += ["", "## 失败页面", "", "| 页面 | 文件 | 原因 |", "| --- | --- | --- |"]
        lines += [f"| {x['page_id']} | {x['file']} | {x['error']} |" for x in res.failed]
    else:
        lines += ["", "问题：无"]
    (task_dir / "results" / "run_log.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (task_dir / "results" / "usage.json").write_text(json.dumps({
        "model": res.model, "tier": tier, "input_tokens": u.input, "output_tokens": u.output,
        "cache_read_input_tokens": u.cache_read, "cache_creation_input_tokens": u.cache_write,
        "estimated_usd": cost, "done": res.done, "failed": len(res.failed)}, ensure_ascii=False, indent=2), encoding="utf-8")


def estimate(task_dir: Path, tier: str = "economy", model: str | None = None, tiles: int | None = None) -> dict:
    """不调用 API 的粗略估算：图片按 宽×高/750 计 token，文字按 1.5 字符/token。"""
    cfg = TIERS[tier]
    model = model or cfg["model"]
    tiles = cfg["tiles"] if tiles is None else tiles
    manifest = json.loads((Path(task_dir) / "manifest_in.json").read_text(encoding="utf-8"))
    per_image = MAX_IMAGE_EDGE * MAX_IMAGE_EDGE * 0.7 / 750
    images = 0
    chars = 0
    for it in manifest["pages"]:
        images += (1 if it.get("image") else 0) + (1 if it.get("titleblock_image") else 0) + (tiles * tiles if tiles > 1 and it.get("image") else 0)
        chars += min(len((Path(task_dir) / it["text"]).read_text(encoding="utf-8")) * 1.6, MAX_TEXT_CHARS)
    tokens_in = images * per_image + chars / 1.5 + len(manifest["pages"]) * 1500
    tokens_out = len(manifest["pages"]) * 2500
    pin, pout = PRICES.get(model, (None, None))
    usd = (tokens_in * pin + tokens_out * pout) / 1e6 if pin is not None else None
    return {"pages": len(manifest["pages"]), "images": images, "input_tokens": int(tokens_in),
            "output_tokens": int(tokens_out), "model": model, "estimated_usd": usd}
