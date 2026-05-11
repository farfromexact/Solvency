from __future__ import annotations

from pathlib import Path

import pandas as pd


POLICY_OVERLAYS = [
    {
        "政策": "规则II过渡期/一司一策",
        "影响位置": "分子/分母/实施节奏",
        "一期处理": "作为说明性政策层保留，不默认改变计算。",
    },
    {
        "政策": "金规〔2023〕5号中小人身险最低资本差异化系数",
        "影响位置": "最低资本",
        "一期处理": "通过“最低资本折扣/乘数”参数手工模拟。",
    },
    {
        "政策": "权益、科创板、REITs、战略新兴产业等风险因子优化",
        "影响位置": "市场风险最低资本",
        "一期处理": "通过市场风险乘数或后续资产类型因子表模拟。",
    },
    {
        "政策": "长期持有股票风险因子调整",
        "影响位置": "权益价格风险最低资本",
        "一期处理": "预留参数入口，第一版不自动识别持仓年限。",
    },
    {
        "政策": "保单未来盈余计入核心资本比例调整",
        "影响位置": "核心资本",
        "一期处理": "不自动重算，可作为后续资本分子情景。",
    },
]


def load_policy_overlays(policy_file: str | Path = "颁布后政策更新.txt") -> pd.DataFrame:
    df = pd.DataFrame(POLICY_OVERLAYS)
    path = Path(policy_file)
    if path.exists():
        df["资料来源"] = path.name
    else:
        df["资料来源"] = "内置摘要"
    return df
