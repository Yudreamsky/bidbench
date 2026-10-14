"""Claude 多模态执行器：读任务包里的页面图片和文字层，调用 Claude 识别，写出与 schema 一致的结果。

分级识别：
- economy：claude-haiku-5-5，整页缩略图 + 图签放大图 + 文字层，适合首次全量识别
- strong： claude-opus-5-5，另加 3×3 局部切片，适合复核冲突页、低置信页和重点图纸

执行器只读任务包、只写 results/，与外部 agent 遵守同一份格式约定。
凭据按 Anthropic SDK 的默认顺序解析（ANTHROPIC_API_KEY 等），代码里不保存密钥。
识别过程逐页记录：实时交给界面（log 回调），同时追加到任务包的 results/progress.log，续跑时接着写。
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pymupdf

from ..recognition import load_schema, page_markdown

log = logging.getLogger(__name__)

EXECUTOR = "claude"
INSTRUCTION_VERSION = "0.3"
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
MODEL_NOTES = {
    "claude-haiku-5-5": "便宜、快，适合首次全量识别",
    "claude-sonnet-5-5": "均衡",
    "claude-opus-5-5": "最强，适合复核冲突页和重点图纸",
}


def model_list() -> list[dict]:
    """给工作台的模型下拉框。"""
    return [{"slug": m, "name": m, "description": MODEL_NOTES.get(m, "")} for m in PRICES]


# 这些模型支持服务端拒答回退：被安全分类器拒绝时由服务端换模型重跑
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """你是建筑工程投标图纸的识别员。每次会收到一页图纸：整页缩略图、图签区域放大图，可能还有局部切片，以及这一页 PDF 的文字层（逐行文字和位置）。
请把这一页识别成结构化数据，严格按给定的 JSON 格式输出。识别结果会进入图纸台账，用于检查缺漏图纸、判断现行版本、交叉比对工程事实，所以准确比完整更重要。

识别要求：
1. 页面类型 page_kind：图纸、图纸目录、设计说明、变更单、答疑或澄清、封面、其他。
2. 图签 title_block：图号、图名、专业、版次、出图日期、比例、项目名称、设计单位，以及修改记录栏的每一行（版次、日期、修改内容）。图号保持图上写法，只去掉多余空格。title_block.bbox 是图签区域的位置。
3. 引用 references：逐条记录原文、指向的图号、类型、编号 mark 和位置。详图索引：圆圈中间一道横线，上方详图编号、下方图号（下方是短横线表示在本图，不记），或“⑤/建施-12”写法，mark 写详图编号（⑤ 写 5）；剖切索引：剖切符号的编号和剖面所在图（“1-1 剖面见建施-05”，英文图写成 301/1、图号在前），mark 写 1-1；立面索引：立面索引符号（201/1），mark 写立面编号；文字引用、大样引用（“详见建施-17”“未注明者详见结施-01”），mark 为 null；图集引用的 target_drawing_no 为 null；变更涉及：变更单、答疑页上条文写到的每个图号都记一条，写到详图编号就填 mark。
4. 图纸目录 catalog_entries：仅目录页填写，逐行提取图号、图名、版次，一行都不能漏；其他页为空数组。
5. 全页内容 content：
   - summary：一两句话说明这张图画的是什么
   - notes：设计说明、附注，按条目分开，label 写条目编号
   - tables：表格，保留表头和每一行
   - key_values：层数、层高、标高、建筑高度、面积、轴网尺寸、构件截面、墙厚、材料强度等级等关键数值，写名称、数值、单位、原文和位置；原文连同紧跟着的限定说明一起抄，例如“地上 21 层（含屋顶机房层）”，不要只截到数字。名称用通用说法，例如“地上层数”“建筑高度”“1F 层高”“3F~20F 层高”“KL3 截面（三层梁配筋图）”“Q1 墙厚”“混凝土强度等级”；只对这张图有效的值照写“本层结构标高”“适用楼层”这样的名称，系统不会拿它跟别的图比较
   - standards：引用的规范和标准，code 写编号和年版，name 写名称
   - details：本图上画的详图和剖面，写编号（① 写 1，剖面写 1-1）、详图名称、比例、位置；没有为空数组
   - xrefs：DWG 里的外部参照名；PDF 和图片填空数组
   - coverage：这张图管哪一块。zone 分区（A区、塔楼、裙房），axis_x 数字轴从哪根到哪根（如 1~5），axis_y 字母轴（如 A~D）；读不到就 value、source、confidence 都填 null
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

def png_bytes(path: Path, clip_frac: tuple | None = None, max_edge: int = MAX_IMAGE_EDGE) -> bytes:
    """页面图片（可以只取一块），长边缩到 max_edge 以内。"""
    doc = pymupdf.open(path)
    try:
        page = doc[0]
        r = page.rect
        clip = r if not clip_frac else pymupdf.Rect(
            r.x0 + r.width * clip_frac[0], r.y0 + r.height * clip_frac[1],
            r.x0 + r.width * clip_frac[2], r.y0 + r.height * clip_frac[3])
        s = min(1.0, max_edge / max(clip.width, clip.height))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), clip=clip, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _png_b64(path: Path, clip_frac: tuple | None = None) -> str:
    return base64.standard_b64encode(png_bytes(path, clip_frac)).decode()


def _image_block(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


# 页面说明、图片说明和文字层：Claude 和 Codex 执行器共用，保证两边给模型的信息一样

def page_header(item: dict) -> str:
    src = item["source"]
    return (f"页面编号 {item['page_id']}，文件 {src['file_path']} 第 {src['page_index']}/{src['page_count']} 页，"
            f"{'有' if src['has_text_layer'] else '没有'} PDF 文字层。")


TITLEBLOCK_NOTE = "图签区域放大图（页面右下角；图签不在这里时以整页为准）"


def page_images(item: dict, tiles: int) -> list[tuple[str, tuple | None, str]]:
    """要附上的图片：(任务包里的图片, 裁切范围, 说明)。"""
    out = []
    if item.get("image"):
        out.append((item["image"], None, "整页缩略图"))
    if item.get("titleblock_image"):
        out.append((item["titleblock_image"], None, TITLEBLOCK_NOTE))
    if item.get("image") and tiles > 1:
        for row in range(tiles):
            for col in range(tiles):
                frac = (col / tiles, row / tiles, (col + 1) / tiles, (row + 1) / tiles)
                out.append((item["image"], frac, f"局部切片 第 {row + 1} 行第 {col + 1} 列，对应整页坐标 "
                                                 f"x {frac[0]:.2f}~{frac[2]:.2f}，y {frac[1]:.2f}~{frac[3]:.2f}"))
    return out


def text_layer(task_dir: Path, item: dict) -> str:
    lines = json.loads((task_dir / item["text_lines"]).read_text(encoding="utf-8"))["lines"]
    if not lines:
        return "这一页没有文字层，请完全依靠图像识别，source 用 vision。"
    text = "\n".join(f"[{','.join(f'{x:.3f}' for x in ln['bbox'])}] {ln['text']}" for ln in lines)
    note = ""
    if len(text) > MAX_TEXT_CHARS:
        kept = text[:MAX_TEXT_CHARS].rsplit("\n", 1)[0]
        note = f"\n（文字层共 {len(lines)} 行，过长，这里只给出前 {kept.count(chr(10)) + 1} 行；其余内容请从图像识别，并在 issues 里注明文字层被截断。）"
        text = kept
    return "PDF 文字层（每行开头是 [x0,y0,x1,y1] 归一化位置）：\n" + text + note


def build_content(task_dir: Path, item: dict, tiles: int) -> list:
    content: list = [{"type": "text", "text": page_header(item)}]
    for image, frac, note in page_images(item, tiles):
        content += [{"type": "text", "text": note + "："}, _image_block(_png_b64(task_dir / image, frac))]
    content.append({"type": "text", "text": text_layer(task_dir, item)})
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

    def merge(self, other: "Usage") -> None:
        with self.lock:
            self.input += other.input
            self.output += other.output
            self.cache_read += other.cache_read
            self.cache_write += other.cache_write

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        pin, pout = PRICES[model]
        return (self.input * pin + self.cache_write * pin * 1.25 + self.cache_read * pin * 0.1 + self.output * pout) / 1e6


class RecognitionError(Exception):
    """单页失败：记下来，继续其他页面。reason 是不含页面内容的失败类别，写排错日志用。"""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


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

    def __init__(self, note=None):
        self.structured = True
        self.lock = threading.Lock()
        self.note = note

    def downgrade(self, e: Exception | None = None) -> None:
        with self.lock:
            if not self.structured:
                return
            self.structured = False
        log.warning("API 不接受结构化输出格式，改为提示里附格式：%s", e)
        if self.note:
            self.note("  注意：API 不接受这份结构化输出格式，改为在提示里附上格式说明，结果照样经过本地校验。")


def _schema_rejected(e: Exception) -> bool:
    msg = str(e).lower()
    return type(e).__name__ == "BadRequestError" and any(w in msg for w in ("schema", "output_config", "format"))


def _request(client, model: str, effort: str, messages: list, schema: dict, mode: _Mode | None = None,
             system: str = SYSTEM_PROMPT):
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
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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
                mode.downgrade(e)
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


def ask_json(client, prompt: str, schema: dict, system: str, model: str, effort: str = "medium", usage=None) -> dict:
    """不带图片问一次 Claude，按给定格式输出 JSON（智能分类用）。凭据、权限这类整次都会失败的错误抛 FatalError。"""
    try:
        msg = _request(client, model, effort, [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                       _api_compatible(schema), system=system)
    except Exception as e:  # noqa: BLE001 - 凭据、权限问题整次停下，其余只算这一批失败
        fatal = _fatal(e)
        if fatal:
            raise FatalError(fatal) from e
        raise RecognitionError(f"Claude API 出错：{type(e).__name__}: {e}", type(e).__name__) from e
    if usage is not None:
        usage.add(msg.usage)
    if msg.stop_reason == "refusal":
        raise RecognitionError("模型拒绝回答", "拒绝")
    if msg.stop_reason == "max_tokens":
        raise RecognitionError("输出超过长度上限", "输出超长")
    try:
        body = _parse_json(_text_of(msg))
    except json.JSONDecodeError as e:
        raise RecognitionError(f"模型输出不是合法 JSON：{e}", "输出不是合法 JSON") from e
    if not isinstance(body, dict):
        raise RecognitionError("模型输出不是 JSON 对象", "输出不是合法 JSON")
    return body


def recognize_page(client, task_dir: Path, item: dict, model: str, effort: str, tiles: int,
                   usage: Usage, validator, schema: dict, mode: _Mode | None = None, note=None) -> dict:
    messages = [{"role": "user", "content": build_content(task_dir, item, tiles)}]
    last_error = None
    for attempt in range(2):  # 第二次只在本地校验失败时，带着错误重来
        if attempt and note:
            note(f"  ↻ {page_label(item)}：输出没有通过格式校验，带着错误重试一次")
        msg = _request(client, model, effort, messages, schema, mode)
        usage.add(msg.usage)
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise RecognitionError(f"模型拒绝识别（{cat or '未说明原因'}）", f"拒绝识别（{cat}）")
        if msg.stop_reason == "max_tokens":
            raise RecognitionError("输出超过长度上限，页面内容过多", "输出超长")
        raw = _text_of(msg)
        try:
            body = _parse_json(raw)
            if not isinstance(body, dict):
                raise json.JSONDecodeError("不是对象", raw, 0)
        except json.JSONDecodeError as e:
            raise RecognitionError(f"模型输出不是合法 JSON：{e}", "输出不是合法 JSON") from e
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
    raise RecognitionError(f"两次输出都没有通过校验：{last_error}", "两次输出都没有通过格式校验")


def page_label(item: dict) -> str:
    src = item["source"]
    return f"{src['file_path'].split(' / ')[-1]} 第 {src['page_index']}/{src['page_count']} 页"


def _found(rec: dict) -> str:
    """识别出来的是什么：图号和图名，没有图号时写页面类型。"""
    tb = rec.get("title_block") or {}
    no, title = ((tb.get(k) or {}).get("value") for k in ("drawing_no", "drawing_title"))
    return " ".join(x for x in (no, title) if x) or rec.get("page_kind") or "?"


class ProgressLog:
    """识别过程日志：每页一行，交给界面（log 回调），同时追加到任务包的 results/progress.log（带时间）。"""

    def __init__(self, task_dir: Path, log_fn=None):
        self.path = Path(task_dir) / "results" / "progress.log"
        self.log_fn = log_fn
        self.lock = threading.Lock()

    def __call__(self, msg: str, show: bool = True) -> None:
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg.strip()}\n")
            except OSError:
                pass
            if show and self.log_fn:
                self.log_fn(msg)


@dataclass
class RunResult:
    done: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    seconds: float = 0.0
    structured: bool = True


def run_pages(task_dir: Path, recognize, settings: str, workers: int, note: ProgressLog, model: str = "",
              progress=None, fatal=None) -> RunResult:
    """各执行器共用的逐页流程：跳过已有合格结果（续跑）；先单独跑第一页，凭据、模型这类整体性问题在这里暴露，
    不会让每一页各失败一次；其余页并发。每页一行识别过程日志。

    recognize(item, usage, note) 返回一页的识别记录，单页失败抛 RecognitionError，要停下整次运行抛 FatalError；
    fatal(e) 判断其他异常是否要停下整次运行（返回给人看的原因），不停就记为这一页失败。
    """
    task_dir = Path(task_dir)
    manifest = json.loads((task_dir / "manifest_in.json").read_text(encoding="utf-8"))
    out = task_dir / "results" / "pages"
    out.mkdir(parents=True, exist_ok=True)
    validator = jsonschema.Draft202012Validator(load_schema())
    res = RunResult(model=model)
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
    workers = max(1, workers)
    if todo:
        note(f"开始识别 {len(todo)} 页" + (f"（已有结果的 {res.skipped} 页跳过）" if res.skipped else "")
             + f"：{settings}，同时识别 {workers} 页。")
    log.info("识别开始：%s 页，跳过 %s 页，%s，并发 %s", len(todo), res.skipped, settings, workers)

    def one(item):
        u, t = Usage(), time.monotonic()
        try:
            rec = recognize(item, u, note)
        finally:
            res.usage.merge(u)
        (out / f"{item['page_id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / f"{item['page_id']}.md").write_text(page_markdown(rec), encoding="utf-8")
        return rec, u, time.monotonic() - t

    def stop(reason: str, e: Exception):
        note(f"停止：{reason}")
        log.error("识别停止：%s（%s: %s）", reason, type(e).__name__, e)

    def settle(it, fn):
        n = f"{res.done + len(res.failed) + 1}/{len(todo)}"
        try:
            rec, u, secs = fn()
            res.done += 1
            note(f"  ✓ {n} {page_label(it)} → {_found(rec)}（{secs:.0f} 秒，输入 {u.input} / 输出 {u.output} tokens）")
        except FatalError as e:
            stop(str(e), e)
            raise
        except RecognitionError as e:
            res.failed.append({"page_id": it["page_id"], "file": it["source"]["file_path"], "error": str(e)})
            note(f"  ✗ {n} {page_label(it)}：{e}")
            log.warning("第 %s 页识别失败：%s", it["page_id"], e.reason)  # 校验错误里可能带着识别出的文字，不写进排错日志
        except Exception as e:  # noqa: BLE001 - API 错误等，记下后继续其他页面
            reason = fatal(e) if fatal else None
            if reason:
                stop(reason, e)
                raise FatalError(reason) from e
            res.failed.append({"page_id": it["page_id"], "file": it["source"]["file_path"],
                               "error": f"{type(e).__name__}: {e}"})
            note(f"  ✗ {n} {page_label(it)}：{type(e).__name__}: {e}")
            log.warning("第 %s 页识别出错：%s: %s", it["page_id"], type(e).__name__, e)
        if progress:
            progress(res.done + len(res.failed), len(todo))

    if todo:
        settle(todo[0], lambda: one(todo[0]))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {pool.submit(one, it): it for it in todo[1:]}
        for f in as_completed(futs):
            settle(futs[f], f.result)
    except FatalError:
        pool.shutdown(wait=True, cancel_futures=True)  # 不再开始排队中的页面
        raise
    pool.shutdown(wait=True)
    res.seconds = time.monotonic() - t0
    if todo:
        note(f"结束：识别 {res.done} 页，失败 {len(res.failed)} 页，用时 {res.seconds:.0f} 秒。", show=False)
    log.info("识别结束：完成 %s 页，失败 %s 页，用时 %.0f 秒，输入 %s / 输出 %s tokens", res.done, len(res.failed),
             res.seconds, res.usage.input, res.usage.output)
    return res


def run(task_dir: Path, tier: str = "economy", model: str | None = None, effort: str | None = None,
        tiles: int | None = None, workers: int = 4, client=None, progress=None, log_fn=None) -> RunResult:
    """log_fn 收到逐页的识别过程（给人看的一行字）。"""
    cfg = TIERS[tier]
    model = model or cfg["model"]
    effort = effort or cfg["effort"]
    tiles = cfg["tiles"] if tiles is None else tiles
    if client is None:
        import anthropic
        client = anthropic.Anthropic(max_retries=4)
    validator = jsonschema.Draft202012Validator(load_schema())
    schema = body_schema()
    note = ProgressLog(task_dir, log_fn)
    mode = _Mode(note)

    def recognize(item, usage, note):
        return recognize_page(client, Path(task_dir), item, model, effort, tiles, usage, validator, schema, mode, note)

    settings = f"模型 {model}，effort {effort}" + (f"，局部切片 {tiles}×{tiles}" if tiles > 1 else "")
    res = run_pages(task_dir, recognize, settings, workers, note, model, progress, _fatal)
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


def estimate_rows(rows, tier: str = "economy", model: str | None = None, tiles: int | None = None) -> dict:
    """不生成任务包的粗略估算（按页面记录算）：口径同 estimate()。"""
    cfg = TIERS[tier]
    model = model or cfg["model"]
    tiles = cfg["tiles"] if tiles is None else tiles
    per_image = MAX_IMAGE_EDGE * MAX_IMAGE_EDGE * 0.7 / 750
    images = len(rows) * (2 + (tiles * tiles if tiles > 1 else 0))
    chars = sum(min(len(r["text"] or "") * 1.6, MAX_TEXT_CHARS) for r in rows)
    tokens_in = images * per_image + chars / 1.5 + len(rows) * 1500
    tokens_out = len(rows) * 2500
    pin, pout = PRICES.get(model, (None, None))
    usd = (tokens_in * pin + tokens_out * pout) / 1e6 if pin is not None else None
    return {"images": images, "input_tokens": int(tokens_in), "output_tokens": int(tokens_out), "model": model,
            "estimated_usd": usd}


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
