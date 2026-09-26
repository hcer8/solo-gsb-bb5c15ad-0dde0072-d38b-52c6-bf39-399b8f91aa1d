"""访谈字幕时间轴校准、锚点推荐与断行排版 API。

- ``POST /anchors/suggest``：接收原片与成片两条按开始时间严格递增的字幕序列，
  在允许跳过片段的前提下做全局最优的文字精确匹配，返回可直接传给
  ``/calibrate`` 的同步锚点。
- ``POST /calibrate``：客户端提交以原录音毫秒计时的字幕片段，以及至少两组
  "原时间 -> 成片时间" 同步锚点；服务端在相邻锚点间做线性换算，
  并对超出锚点覆盖范围、换算后时长非正的片段返回错误。
- ``POST /layout/wrap``：按显示宽度为字幕片段建议断行。宽度按 Unicode 码点
  计算（东亚全角/宽字符 2 格，其余 1 格），只在字符之间断行并遵守行首/行尾
  禁则；对每段在所有可行方案中依次按行数最少、最大行宽最小、行宽平方和最小、
  断点序列字典序最小做全局最优选择（而非逐行贪心）。
"""
from __future__ import annotations

from array import array
from bisect import bisect_right
from fractions import Fraction
from typing import List, Optional, Tuple
from unicodedata import east_asian_width

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
class Anchor(BaseModel):
    """一组同步锚点：原录音时间 -> 成片时间（单位：毫秒）。"""

    original: int = Field(..., description="原录音时间（毫秒）")
    final: int = Field(..., description="成片时间（毫秒）")


class Segment(BaseModel):
    """一条以原录音毫秒计时的字幕片段。"""

    start: int = Field(..., description="片段原起始时间（毫秒）")
    end: int = Field(..., description="片段原结束时间（毫秒）")
    text: str = Field("", description="字幕文字")


class CalibrateRequest(BaseModel):
    anchors: List[Anchor] = Field(
        ..., description="同步锚点，至少两组；两列时间均须严格递增"
    )
    segments: List[Segment] = Field(..., description="待校准的字幕片段")

    @field_validator("segments")
    @classmethod
    def _segments_not_empty(cls, v: List[Segment]) -> List[Segment]:
        if not v:
            raise ValueError("segments 不能为空")
        return v


class CalibratedSegment(BaseModel):
    index: int = Field(..., description="片段在输入中的位置（从 0 开始）")
    start: int = Field(..., description="换算后起始时间（毫秒，整数）")
    end: int = Field(..., description="换算后结束时间（毫秒，整数）")
    text: str = Field("", description="原字幕文字")


class CalibrateResponse(BaseModel):
    segments: List[CalibratedSegment]


class SubtitleCue(BaseModel):
    """一条字幕：整数毫秒起始时间与文字。"""

    start: int = Field(..., description="起始时间（毫秒）")
    text: str = Field(..., description="字幕文字")


class SuggestAnchorsRequest(BaseModel):
    original: List[SubtitleCue] = Field(
        ..., description="原片字幕序列，start 须为非负整数且严格递增"
    )
    final: List[SubtitleCue] = Field(
        ..., description="成片字幕序列，start 须为非负整数且严格递增"
    )


class SuggestedAnchor(BaseModel):
    """一组推荐锚点及其来源下标，可原样作为 /calibrate 的锚点。"""

    original: int = Field(..., description="原片字幕起始时间（毫秒）")
    final: int = Field(..., description="成片字幕起始时间（毫秒）")
    original_index: int = Field(..., description="原片字幕下标（从 0 开始）")
    final_index: int = Field(..., description="成片字幕下标（从 0 开始）")


class SuggestAnchorsResponse(BaseModel):
    anchors: List[SuggestedAnchor]


class WrapSegment(BaseModel):
    """一条待断行的字幕片段：时码与文字原样保留。"""

    start: int = Field(..., description="片段起始时间（毫秒）")
    end: int = Field(..., description="片段结束时间（毫秒）")
    text: str = Field("", description="字幕文字")


class WrapRequest(BaseModel):
    segments: List[WrapSegment] = Field(..., description="待断行的字幕片段")
    max_width: int = Field(
        ..., gt=0, description="每行最大显示宽度（格，正整数；全角/宽字符占 2 格）"
    )
    max_lines: int = Field(..., ge=1, le=4, description="每段最多行数（1～4）")


class WrappedSegment(BaseModel):
    index: int = Field(..., description="片段在输入中的位置（从 0 开始）")
    start: int = Field(..., description="原起始时间（毫秒）")
    end: int = Field(..., description="原结束时间（毫秒）")
    text: str = Field("", description="原字幕文字")
    lines: List[str] = Field(
        ..., description="断行后的各行文字，按顺序拼接即等于原文"
    )
    breaks: List[int] = Field(
        ...,
        description=(
            "断行字符偏移（按 Unicode 码点计）：每个偏移是下一行行首字符在原文中的"
            "下标，即第 i 条断行发生在 text[breaks[i]] 之前"
        ),
    )


class WrapResponse(BaseModel):
    segments: List[WrappedSegment]


class ErrorItem(BaseModel):
    location: str = Field(..., description="出错位置，如 anchor / segment")
    index: Optional[int] = Field(None, description="出错元素下标（从 0 开始）")
    code: str = Field(..., description="错误代码")
    message: str = Field(..., description="错误说明")


# --------------------------------------------------------------------------- #
# 核心换算逻辑
# --------------------------------------------------------------------------- #
def round_half_up(value: Fraction) -> int:
    """四舍五入到整数（.5 一律向远离零方向进位）。"""
    q, r = divmod(value.numerator, value.denominator)
    if r * 2 >= value.denominator:
        q += 1
    return q


def build_anchor_lines(anchors: List[Anchor]) -> List[tuple]:
    """把相邻锚点预编译为 (o0, f0, slope) 线段，slope 为精确分数。"""
    lines = []
    for a, b in zip(anchors, anchors[1:]):
        slope = Fraction(b.final - a.final, b.original - a.original)
        lines.append((a.original, a.final, slope))
    return lines


def map_time(
    t: int,
    originals: List[int],
    finals: List[int],
    lines: List[tuple],
) -> Optional[Fraction]:
    """把单个原时间线性换算为成片时间；超出锚点覆盖范围返回 None。"""
    first, last = originals[0], originals[-1]
    if t < first or t > last:
        return None
    # 最后一个锚点原样命中；其余时间点归入其左侧锚点所在线段
    i = bisect_right(originals, t) - 1
    if i == len(originals) - 1:
        return Fraction(finals[-1])
    o0, f0, slope = lines[i]
    return f0 + slope * (t - o0)


def validate_anchors(anchors: List[Anchor]) -> List[ErrorItem]:
    """校验锚点数量及两列时间严格递增。"""
    errors: List[ErrorItem] = []
    if len(anchors) < 2:
        errors.append(
            ErrorItem(
                location="anchors",
                index=None,
                code="not_enough_anchors",
                message="至少需要两组同步锚点",
            )
        )
        return errors

    for i in range(1, len(anchors)):
        if anchors[i].original <= anchors[i - 1].original:
            errors.append(
                ErrorItem(
                    location="anchor.original",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"锚点 original 未严格递增：第 {i} 组为 {anchors[i].original}，"
                        f"前一组为 {anchors[i - 1].original}"
                    ),
                )
            )
        if anchors[i].final <= anchors[i - 1].final:
            errors.append(
                ErrorItem(
                    location="anchor.final",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"锚点 final 未严格递增：第 {i} 组为 {anchors[i].final}，"
                        f"前一组为 {anchors[i - 1].final}"
                    ),
                )
            )
    return errors


def calibrate(req: CalibrateRequest) -> tuple[Optional[CalibrateResponse], List[ErrorItem]]:
    """执行校准；成功返回响应，失败返回错误列表。"""
    errors = validate_anchors(req.anchors)
    if errors:
        return None, errors

    lines = build_anchor_lines(req.anchors)
    originals = [a.original for a in req.anchors]
    finals = [a.final for a in req.anchors]
    results: List[CalibratedSegment] = []

    for idx, seg in enumerate(req.segments):
        if seg.end <= seg.start:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"原片段时长非正：start={seg.start}，end={seg.end}，要求 end > start"
                    ),
                )
            )
            continue

        start_mapped = map_time(seg.start, originals, finals, lines)
        end_mapped = map_time(seg.end, originals, finals, lines)
        if start_mapped is None or end_mapped is None:
            lo, hi = req.anchors[0].original, req.anchors[-1].original
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="out_of_anchor_range",
                    message=(
                        f"第 {idx} 段 [{seg.start}, {seg.end}] 超出锚点覆盖范围 "
                        f"[{lo}, {hi}]"
                    ),
                )
            )
            continue

        if end_mapped <= start_mapped:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_mapped_duration",
                    message=(
                        f"第 {idx} 段换算后时长非正："
                        f"start={float(start_mapped):.3f}ms，end={float(end_mapped):.3f}ms"
                    ),
                )
            )
            continue

        start_ms = round_half_up(start_mapped)
        end_ms = round_half_up(end_mapped)
        if end_ms <= start_ms:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_mapped_duration_after_rounding",
                    message=(
                        f"第 {idx} 段四舍五入后时长非正：start={start_ms}，end={end_ms}"
                    ),
                )
            )
            continue

        results.append(
            CalibratedSegment(
                index=idx, start=start_ms, end=end_ms, text=seg.text
            )
        )

    if errors:
        return None, errors
    return CalibrateResponse(segments=results), []


# --------------------------------------------------------------------------- #
# 锚点推荐逻辑
# --------------------------------------------------------------------------- #
def normalize_text(text: str) -> str:
    """去首尾空白，并把连续空白（含全角空格等 Unicode 空白）统一为单个空格。"""
    return " ".join(text.split())


def validate_subtitle_track(
    cues: List[SubtitleCue], track: str
) -> List[ErrorItem]:
    """校验单条字幕轨道：start 非负、严格递增、文字去空白后非空。"""
    errors: List[ErrorItem] = []
    for idx, cue in enumerate(cues):
        if cue.start < 0:
            errors.append(
                ErrorItem(
                    location=f"{track}.start",
                    index=idx,
                    code="negative_time",
                    message=f"{track} 第 {idx} 条 start 为负：{cue.start}",
                )
            )
        if idx > 0 and cue.start <= cues[idx - 1].start:
            errors.append(
                ErrorItem(
                    location=f"{track}.start",
                    index=idx,
                    code="not_strictly_increasing",
                    message=(
                        f"{track} start 未严格递增：第 {idx} 条为 {cue.start}，"
                        f"前一条为 {cues[idx - 1].start}"
                    ),
                )
            )
        if not normalize_text(cue.text):
            errors.append(
                ErrorItem(
                    location=f"{track}.text",
                    index=idx,
                    code="empty_text",
                    message=f"{track} 第 {idx} 条 text 去空白后为空",
                )
            )
    return errors


def best_alignment(a: List[str], b: List[str]) -> List[Tuple[int, int]]:
    """求两条文本序列允许跳过时的全局最优有序一对一匹配。

    依次优化：
      1. 匹配数最多；
      2. 配对序号差绝对值之和 ``sum(|i - j|)`` 最小；
      3. 配对序号序列 ``[(i, j), ...]`` 字典序最小（决定重复台词等平局）。

    用后缀动态规划：``F(i, j)`` 表示后缀 ``a[i:]``、``b[j:]`` 上的最优解，
    决策为匹配 (i, j)（仅当文本相同）、跳过 a[i] 或跳过 b[j]。每个单元存
    (匹配数, 代价, 第一个配对)；在匹配数与代价持平时比较候选后继的第一个配对
    ——第一个配对相同则整条配对序列完全相同。重建时，首配对为 (pi, pj) 的
    单元，其后续必然是 F(pi+1, pj+1)，沿此跳转即可。所有行使用定长数组保留，
    额外内存 O(n*m)。
    """
    n, m = len(a), len(b)

    all_count: List[array] = [array("i", [0]) * (m + 1)]
    all_cost: List[array] = [array("q", [0]) * (m + 1)]
    all_pi: List[array] = [array("i", [-1]) * (m + 1)]
    all_pj: List[array] = [array("i", [-1]) * (m + 1)]

    for i in range(n - 1, -1, -1):
        cur_count = array("i", [0]) * (m + 1)
        cur_cost = array("q", [0]) * (m + 1)
        cur_pi = array("i", [-1]) * (m + 1)
        cur_pj = array("i", [-1]) * (m + 1)
        nxt_count = all_count[-1]
        nxt_cost = all_cost[-1]
        nxt_pi = all_pi[-1]
        nxt_pj = all_pj[-1]
        ai = a[i]

        for j in range(m - 1, -1, -1):
            j1 = j + 1
            # 候选 1：跳过 b[j]，后继为同一行的 F(i, j+1)
            best_c = cur_count[j1]
            best_k = cur_cost[j1]
            best_pi = cur_pi[j1]
            best_pj = cur_pj[j1]

            # 候选 2：跳过 a[i]，后继为 F(i+1, j)
            c2, k2 = nxt_count[j], nxt_cost[j]
            p2i, p2j = nxt_pi[j], nxt_pj[j]
            if (
                c2 > best_c
                or c2 == best_c
                and (
                    k2 < best_k
                    or k2 == best_k
                    and (
                        # 平局比首配对；首配对为 -1 表示空序列，排在最后
                        p2i != -1
                        and (
                            best_pi == -1
                            or (p2i, p2j) < (best_pi, best_pj)
                        )
                    )
                )
            ):
                best_c, best_k = c2, k2
                best_pi, best_pj = p2i, p2j

            # 候选 3：匹配 (i, j)，后继为 F(i+1, j+1)
            if ai == b[j]:
                c3 = nxt_count[j1] + 1
                k3 = nxt_cost[j1] + abs(i - j)
                # 匹配候选在匹配数/代价持平时，首配对 (i, j) 必然最小
                if c3 > best_c or (c3 == best_c and k3 <= best_k):
                    best_c, best_k = c3, k3
                    best_pi, best_pj = i, j

            cur_count[j] = best_c
            cur_cost[j] = best_k
            cur_pi[j] = best_pi
            cur_pj[j] = best_pj

        all_count.append(cur_count)
        all_cost.append(cur_cost)
        all_pi.append(cur_pi)
        all_pj.append(cur_pj)

    # all_*[n - i] 保存的是行 F(i, *)；首配对 (pi, pj) 的后续即 F(pi+1,pj+1)
    pairs: List[Tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        r = n - i
        pi, pj = all_pi[r][j], all_pj[r][j]
        if pi == -1:
            break
        pairs.append((pi, pj))
        i, j = pi + 1, pj + 1
    return pairs


def suggest_anchors(
    req: SuggestAnchorsRequest,
) -> tuple[Optional[SuggestAnchorsResponse], List[ErrorItem]]:
    """校验两条轨道并求全局最优匹配，生成可直接用于 /calibrate 的锚点。"""
    errors: List[ErrorItem] = []
    errors.extend(validate_subtitle_track(req.original, "original"))
    errors.extend(validate_subtitle_track(req.final, "final"))
    if errors:
        return None, errors

    original_text = [normalize_text(c.text) for c in req.original]
    final_text = [normalize_text(c.text) for c in req.final]
    pairs = best_alignment(original_text, final_text)

    if len(pairs) < 2:
        errors.append(
            ErrorItem(
                location="anchors",
                index=None,
                code="not_enough_matches",
                message=(
                    f"匹配片段仅 {len(pairs)} 对，至少需要 2 对才能构成同步锚点"
                ),
            )
        )
        return None, errors

    anchors = [
        SuggestedAnchor(
            original=req.original[i].start,
            final=req.final[j].start,
            original_index=i,
            final_index=j,
        )
        for i, j in pairs
    ]
    return SuggestAnchorsResponse(anchors=anchors), []


# --------------------------------------------------------------------------- #
# 断行（排版）逻辑
# --------------------------------------------------------------------------- #
# 行首禁则字符：不允许在某一行开头出现，即不允许在它们前面断行
NO_LINE_START = frozenset("，。！？；：）】")
# 行尾禁则字符：不允许在某一行末尾出现，即不允许在它们后面断行
NO_LINE_END = frozenset("（【")


def char_cells(ch: str) -> int:
    """按 Unicode 码点计宽：东亚全角/宽字符占 2 格，其余占 1 格。"""
    return 2 if east_asian_width(ch) in ("F", "W") else 1


def wrap_text(
    text: str, max_width: int, max_lines: int
) -> Optional[Tuple[List[str], List[int]]]:
    """对单段文字做全局最优断行，返回 (各行文字, 断行字符偏移)；不可行返回 None。

    可行方案须满足：只在字符之间断行；每行非空且宽度不超过 ``max_width``；
    行首不为 ``NO_LINE_START`` 中的字符、行尾不为 ``NO_LINE_END`` 中的字符；
    行数不超过 ``max_lines``。在所有可行方案中依次选择：

      1. 行数最少；
      2. 最大行宽最小；
      3. 各行宽平方和最小；
      4. 断点序列字典序最小。

    分三层动态规划求解（而非逐行贪心）：先求最少行数 L，再在恰好 L 行的
    方案中求最小的最大行宽 W，最后在每行宽不超过 W 的方案中优化
    (行宽平方和, 断点序列)。由于 max 会把后缀的最大行宽"掩盖"掉，三层目标
    不能合并成一次 DP，必须逐层把上一层的最优值固化为约束。
    """
    n = len(text)
    if n == 0:
        return [], []

    # 码点宽度前缀和：text[k:j] 的宽度 = prefix[j] - prefix[k]
    prefix = [0] * (n + 1)
    for i, ch in enumerate(text):
        prefix[i + 1] = prefix[i] + char_cells(ch)

    def breakable(k: int) -> bool:
        """是否允许在字符偏移 k 处断行（k 为下一行行首字符的下标，1 <= k < n）。"""
        return text[k] not in NO_LINE_START and text[k - 1] not in NO_LINE_END

    # 第一层：最少行数。min_lines[k] 为 text[k:] 所需的最少行数；
    # 不可行时保持初始值 n + 1（可行解至多 n 行，每行至少一个字符）
    min_lines = [n + 1] * (n + 1)
    min_lines[n] = 0
    for k in range(n - 1, -1, -1):
        for j in range(k + 1, n + 1):
            if prefix[j] - prefix[k] > max_width:
                break  # 行宽随 j 单调增大，之后的候选只会更宽
            if j < n and not breakable(j):
                continue
            if min_lines[j] + 1 < min_lines[k]:
                min_lines[k] = min_lines[j] + 1
    lines_needed = min_lines[0]
    if lines_needed > n or lines_needed > max_lines:
        return None

    # 第二层：恰好 lines_needed 行时，最大行宽的最小值。
    # best_max[k][l] 为 text[k:] 排成 l 行时最大行宽的最小值，-1 表示不可行
    best_max = [[-1] * (lines_needed + 1) for _ in range(n + 1)]
    best_max[n][0] = 0
    for k in range(n - 1, -1, -1):
        for l in range(1, lines_needed + 1):
            best = -1
            for j in range(k + 1, n + 1):
                w = prefix[j] - prefix[k]
                if w > max_width:
                    break
                if j < n and not breakable(j):
                    continue
                sub = best_max[j][l - 1]
                if sub < 0:
                    continue
                cand = w if w > sub else sub
                if best < 0 or cand < best:
                    best = cand
            best_max[k][l] = best
    width_cap = best_max[0][lines_needed]

    # 第三层：每行宽不超过 width_cap 时，(行宽平方和, 断点序列) 字典序最小。
    # best_tail[k][l] = (平方和, 断点元组)，None 表示不可行
    best_tail: List[List[Optional[Tuple[int, Tuple[int, ...]]]]] = [
        [None] * (lines_needed + 1) for _ in range(n + 1)
    ]
    best_tail[n][0] = (0, ())
    for k in range(n - 1, -1, -1):
        for l in range(1, lines_needed + 1):
            best: Optional[Tuple[int, Tuple[int, ...]]] = None
            for j in range(k + 1, n + 1):
                w = prefix[j] - prefix[k]
                if w > width_cap:
                    break
                if j < n and not breakable(j):
                    continue
                sub = best_tail[j][l - 1]
                if sub is None:
                    continue
                cand = (w * w + sub[0], ((j,) if j < n else ()) + sub[1])
                if best is None or cand < best:
                    best = cand
            best_tail[k][l] = best

    result = best_tail[0][lines_needed]
    assert result is not None  # 前两层已保证可行
    breaks = list(result[1])
    bounds = [0] + breaks + [n]
    lines = [text[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    return lines, breaks


def layout_wrap(req: WrapRequest) -> tuple[Optional[WrapResponse], List[ErrorItem]]:
    """对每段字幕做全局最优断行；任一片段失败则收集错误并整体返回 422。"""
    errors: List[ErrorItem] = []
    results: List[WrappedSegment] = []

    for idx, seg in enumerate(req.segments):
        if seg.end <= seg.start:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"片段时长非正：start={seg.start}，end={seg.end}，要求 end > start"
                    ),
                )
            )
            continue

        wrapped = wrap_text(seg.text, req.max_width, req.max_lines)
        if wrapped is None:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="cannot_wrap",
                    message=(
                        f"第 {idx} 段文字无法在 max_width={req.max_width}、"
                        f"max_lines={req.max_lines} 的约束下完成断行"
                    ),
                )
            )
            continue

        lines, breaks = wrapped
        results.append(
            WrappedSegment(
                index=idx,
                start=seg.start,
                end=seg.end,
                text=seg.text,
                lines=lines,
                breaks=breaks,
            )
        )

    if errors:
        return None, errors
    return WrapResponse(segments=results), []


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="访谈字幕时间轴校准 API",
    description=(
        "1. ``POST /anchors/suggest``：对比原片与成片两条字幕序列，"
        "全局最优匹配文字相同的片段，返回可直接使用的「原时间 -> 成片时间」"
        "同步锚点；\n"
        "2. ``POST /calibrate``：根据至少两组同步锚点，在相邻锚点间线性换算"
        "字幕片段的起止时间（毫秒）；\n"
        "3. ``POST /layout/wrap``：按显示宽度为字幕片段建议断行，保留原时码"
        "与文字，返回每段的各行文字与断行字符偏移。"
    ),
    version="1.2.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/anchors/suggest", response_model=SuggestAnchorsResponse)
def suggest_anchors_endpoint(
    req: SuggestAnchorsRequest,
) -> SuggestAnchorsResponse:
    resp, errors = suggest_anchors(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "anchor_suggest_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.post("/calibrate", response_model=CalibrateResponse)
def calibrate_endpoint(req: CalibrateRequest) -> CalibrateResponse:
    resp, errors = calibrate(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "calibration_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.post("/layout/wrap", response_model=WrapResponse)
def layout_wrap_endpoint(req: WrapRequest) -> WrapResponse:
    resp, errors = layout_wrap(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "layout_wrap_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.exception_handler(RequestValidationError)
def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": exc.errors()},
    )
