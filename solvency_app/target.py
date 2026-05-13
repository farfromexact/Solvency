from __future__ import annotations

from dataclasses import dataclass
from math import isclose

from .scenario import Adjustment, PolicyParameters, run_scenario
from .workbook import WorkbookData


METRIC_CORE = "核心偿付能力充足率"
METRIC_COMPREHENSIVE = "综合偿付能力充足率"
ASSET_TYPE_DIMENSION = "资产类型"
VALUE_COL = "认可价值"


@dataclass(frozen=True)
class TargetSolveResult:
    mode: str
    metric: str
    asset_type: str
    duration_bucket: str
    solved: bool
    reason: str
    baseline_ratio: float
    target_ratio: float
    achieved_ratio: float
    change_amount: float
    change_pct: float
    minimum_capital_delta: float
    actual_capital_delta: float


def solve_target_change(
    data: WorkbookData,
    asset_type: str,
    metric: str,
    target_delta_pct_points: float,
    mode: str,
    duration_bucket: str = "存量平均",
    policy: PolicyParameters | None = None,
    max_multiple: float = 5.0,
    tolerance_pct_points: float = 0.01,
    scan_steps: int = 80,
    binary_steps: int = 50,
) -> TargetSolveResult:
    effective_policy = _policy_for_mode(policy or PolicyParameters(), mode)
    current_value = _asset_value(data, asset_type)
    baseline = run_scenario(data, [], effective_policy).scenario
    baseline_ratio = float(baseline[metric])
    target_ratio = baseline_ratio + target_delta_pct_points / 100.0
    tolerance = tolerance_pct_points / 100.0

    if isclose(target_delta_pct_points, 0.0, abs_tol=1e-12):
        return _result(
            data=data,
            policy=effective_policy,
            mode=mode,
            metric=metric,
            asset_type=asset_type,
            duration_bucket=duration_bucket,
            target_ratio=target_ratio,
            change_amount=0.0,
            solved=True,
            reason="目标变化为 0。",
        )

    if current_value <= 0:
        return _unsolved(mode, metric, asset_type, duration_bucket, baseline_ratio, target_ratio, "当前资产类型认可价值为 0，无法按正向金额求解。")

    max_amount = current_value * max_multiple
    baseline_gap = baseline_ratio - target_ratio
    if _meets_target(baseline_ratio, target_ratio, target_delta_pct_points, tolerance):
        return _result(
            data=data,
            policy=effective_policy,
            mode=mode,
            metric=metric,
            asset_type=asset_type,
            duration_bucket=duration_bucket,
            target_ratio=target_ratio,
            change_amount=0.0,
            solved=True,
            reason="基准已经达到目标。",
        )

    previous_amount = 0.0
    previous_ratio = baseline_ratio
    bracket: tuple[float, float] | None = None
    for step in range(1, scan_steps + 1):
        amount = max_amount * step / scan_steps
        ratio = _ratio_at(data, effective_policy, mode, metric, asset_type, duration_bucket, amount)
        if _crosses_target(previous_ratio, ratio, target_ratio, baseline_gap):
            bracket = (previous_amount, amount)
            break
        previous_amount = amount
        previous_ratio = ratio

    if bracket is None:
        upper_ratio = _ratio_at(data, effective_policy, mode, metric, asset_type, duration_bucket, max_amount)
        return _unsolved(
            mode,
            metric,
            asset_type,
            duration_bucket,
            baseline_ratio,
            target_ratio,
            f"正向变化到当前资产认可价值的 {max_multiple:.0f} 倍仍未达到目标；上限结果为 {upper_ratio:.4%}。",
        )

    low, high = bracket
    for _ in range(binary_steps):
        mid = (low + high) / 2.0
        ratio = _ratio_at(data, effective_policy, mode, metric, asset_type, duration_bucket, mid)
        if _meets_target(ratio, target_ratio, target_delta_pct_points, tolerance):
            high = mid
        else:
            low = mid

    return _result(
        data=data,
        policy=effective_policy,
        mode=mode,
        metric=metric,
        asset_type=asset_type,
        duration_bucket=duration_bucket,
        target_ratio=target_ratio,
        change_amount=high,
        solved=True,
        reason="已找到正向最小变化金额。",
    )


def _policy_for_mode(policy: PolicyParameters, mode: str) -> PolicyParameters:
    return policy


def _asset_value(data: WorkbookData, asset_type: str) -> float:
    mask = data.kbqs[ASSET_TYPE_DIMENSION].astype(str) == str(asset_type)
    return float(data.kbqs.loc[mask, VALUE_COL].sum())


def _adjustment(asset_type: str, mode: str, duration_bucket: str, amount: float) -> Adjustment:
    return Adjustment(
        dimension=ASSET_TYPE_DIMENSION,
        member=asset_type,
        change_pct=0.0,
        mode=mode,
        change_amount=amount,
        duration_bucket=duration_bucket,
    )


def _ratio_at(
    data: WorkbookData,
    policy: PolicyParameters,
    mode: str,
    metric: str,
    asset_type: str,
    duration_bucket: str,
    amount: float,
) -> float:
    result = run_scenario(data, [_adjustment(asset_type, mode, duration_bucket, amount)], policy)
    return float(result.scenario[metric])


def _meets_target(value: float, target: float, target_delta_pct_points: float, tolerance: float) -> bool:
    if target_delta_pct_points >= 0:
        return value + tolerance >= target
    return value - tolerance <= target


def _crosses_target(previous: float, current: float, target: float, baseline_gap: float) -> bool:
    current_gap = current - target
    return current_gap == 0 or current_gap * baseline_gap <= 0 or min(previous, current) <= target <= max(previous, current)


def _result(
    data: WorkbookData,
    policy: PolicyParameters,
    mode: str,
    metric: str,
    asset_type: str,
    duration_bucket: str,
    target_ratio: float,
    change_amount: float,
    solved: bool,
    reason: str,
) -> TargetSolveResult:
    result = run_scenario(data, [_adjustment(asset_type, mode, duration_bucket, change_amount)], policy)
    base = run_scenario(data, [], policy).scenario
    scenario = result.scenario
    current_value = _asset_value(data, asset_type)
    return TargetSolveResult(
        mode=mode,
        metric=metric,
        asset_type=asset_type,
        duration_bucket=duration_bucket,
        solved=solved,
        reason=reason,
        baseline_ratio=float(base[metric]),
        target_ratio=target_ratio,
        achieved_ratio=float(scenario[metric]),
        change_amount=change_amount,
        change_pct=change_amount / current_value if current_value else 0.0,
        minimum_capital_delta=float(scenario["最低资本"] - base["最低资本"]),
        actual_capital_delta=float(scenario["实际资本"] - base["实际资本"]),
    )


def _unsolved(
    mode: str,
    metric: str,
    asset_type: str,
    duration_bucket: str,
    baseline_ratio: float,
    target_ratio: float,
    reason: str,
) -> TargetSolveResult:
    return TargetSolveResult(
        mode=mode,
        metric=metric,
        asset_type=asset_type,
        duration_bucket=duration_bucket,
        solved=False,
        reason=reason,
        baseline_ratio=baseline_ratio,
        target_ratio=target_ratio,
        achieved_ratio=baseline_ratio,
        change_amount=0.0,
        change_pct=0.0,
        minimum_capital_delta=0.0,
        actual_capital_delta=0.0,
    )
