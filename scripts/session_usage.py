# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read a session's spend and token usage, and total it across a run.

``usage.json`` comes from the review job, so the check reads it as
untrusted input: every figure must be a finite, non-negative number,
a count is kept as an integer, and a model name must look like the
``model`` input. A field that fails is left out; nothing here fails
the check. A result file is a claim too, so the report checks each
figure again as ``usage_totals`` sums it.
"""

from __future__ import annotations

import math
import re
from typing import Any, cast

# The model input's pattern; a forged file cannot list models without bound.
MODEL_RE = re.compile(r"^[a-z0-9.-]+$")
MAX_MODEL_CHARS = 100
MAX_USAGE_MODELS = 8
USAGE_TOKEN_FIELDS = (
    ("input_tokens", "inputTokens"),
    ("cache_read_tokens", "cacheReadTokens"),
    ("cache_write_tokens", "cacheWriteTokens"),
    ("output_tokens", "outputTokens"),
    ("reasoning_tokens", "reasoningTokens"),
)
# The integer figures of a model's usage record, in table order, with
# their column headings; premium requests sit after requests.
USAGE_COUNTS = (
    ("requests", "Requests"),
    ("input_tokens", "Input tokens"),
    ("cache_read_tokens", "Cache read"),
    ("cache_write_tokens", "Cache write"),
    ("output_tokens", "Output tokens"),
    ("reasoning_tokens", "Reasoning tokens"),
    ("nano_aiu", "Nano AIU"),
)


def finite_number(value: object) -> float | None:
    """A finite, non-negative number from usage.json, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def usage_count(value: object) -> int | None:
    """A count from usage.json as an integer, or None when malformed."""
    if finite_number(value) is None:
        return None
    return value if isinstance(value, int) else int(cast("float", value))


def usage_model(value: object) -> bool:
    """Whether a usage.json key is a model name the record may carry."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_MODEL_CHARS
        and MODEL_RE.fullmatch(value) is not None
    )


def put_figure(
    record: dict[str, Any], name: str, value: object, *, count: bool = True
) -> None:
    """Store one usage figure under ``name`` when it passes the check."""
    figure = usage_count(value) if count else finite_number(value)
    if figure is not None:
        record[name] = figure


def object_of(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def model_record(name: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """One model's figures from usage.json's ``modelMetrics``."""
    requests = object_of(metrics.get("requests"))
    tokens = object_of(metrics.get("usage"))
    record: dict[str, Any] = {"model": name}
    put_figure(record, "requests", requests.get("count"))
    put_figure(record, "premium_requests", requests.get("cost"), count=False)
    for field, key in USAGE_TOKEN_FIELDS:
        put_figure(record, field, tokens.get(key))
    put_figure(record, "nano_aiu", metrics.get("totalNanoAiu"))
    return record


def usage_record(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """The session's token usage by model, or None when usage.json holds none."""
    if usage is None:
        return None
    record: dict[str, Any] = {}
    put_figure(record, "nano_aiu", usage.get("totalNanoAiu"))
    put_figure(record, "last_call_input_tokens", usage.get("lastCallInputTokens"))
    metrics = object_of(usage.get("modelMetrics"))
    names = sorted(
        name
        for name, value in metrics.items()
        if usage_model(name) and isinstance(value, dict)
    )
    # A model with no accepted figure is left out before the limit, so an
    # empty entry cannot displace one that carries usage.
    records = (model_record(name, object_of(metrics[name])) for name in names)
    models = [record for record in records if len(record) > 1][:MAX_USAGE_MODELS]
    if models:
        record["models"] = models
    return record or None


def usage_numbers(usage: dict[str, Any] | None) -> tuple[float | None, int | None]:
    """Premium request cost and agent seconds from the session usage file."""
    if usage is None:
        return None, None
    cost = finite_number(usage.get("totalPremiumRequestCost"))
    duration = finite_number(usage.get("totalApiDurationMs"))
    return cost, None if duration is None else int(duration // 1000)


def accepted_figures(row: dict[str, Any]) -> dict[str, int | float]:
    """The figures of one result row that pass the check, by field."""
    figures: dict[str, int | float] = {}
    premium = finite_number(row.get("premium_requests"))
    if premium is not None:
        figures["premium_requests"] = premium
    for field, _ in USAGE_COUNTS:
        value = row.get(field)
        if type(value) is int and finite_number(value) is not None:
            figures[field] = value
    return figures


def model_rows(result: dict[str, Any]) -> list[tuple[str, dict[str, int | float]]]:
    """Each model of a result's usage record with its accepted figures.

    A row with no accepted figure is left out, each name counts once,
    and at most eight rows are read.
    """
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return []
    models = cast("dict[str, Any]", usage).get("models")
    if not isinstance(models, list):
        return []
    rows: dict[str, dict[str, int | float]] = {}
    for row in cast("list[Any]", models)[:MAX_USAGE_MODELS]:
        if not isinstance(row, dict):
            continue
        name = cast("dict[str, Any]", row).get("model")
        if not usage_model(name) or name in rows:
            continue
        figures = accepted_figures(cast("dict[str, Any]", row))
        if figures:
            rows[cast("str", name)] = figures
    return list(rows.items())


def usage_totals(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Token usage by model: sessions that used it and the sum of each figure.

    A result file is a claim, so each figure is checked again as it is
    summed, and an absent or malformed one adds nothing. A figure that
    would carry a total past a finite number adds nothing either.
    """
    totals: dict[str, dict[str, Any]] = {}
    for result in results:
        for name, figures in model_rows(result):
            total = totals.setdefault(
                name,
                {
                    "model": name,
                    "sessions": 0,
                    "premium_requests": 0.0,
                    **{field: 0 for field, _ in USAGE_COUNTS},
                },
            )
            total["sessions"] += 1
            for field, value in figures.items():
                candidate = total[field] + value
                if finite_number(candidate) is not None:
                    total[field] = candidate
    for total in totals.values():
        total["premium_requests"] = round(total["premium_requests"], 2)
    return [totals[name] for name in sorted(totals)]
