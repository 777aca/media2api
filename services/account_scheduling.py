from __future__ import annotations


DEFAULT_PRIORITY = 0
DEFAULT_WEIGHT = 1
MAX_PRIORITY = 1000
MAX_WEIGHT = 1000


def normalize_scheduling_value(value: object, *, minimum: int, maximum: int, default: int) -> int:
    """旧数据或导入文件中缺失、类型错误的设置使用默认值。"""
    if type(value) is not int or not minimum <= value <= maximum:
        return default
    return value


class AccountScheduler:
    """在已通过可用性筛选的账号中，优先级优先，同级平滑加权轮询。

    调用方持有账号池锁；按稳定账号 ID 计分，凭据轮换不会重置权重。
    """

    def __init__(self) -> None:
        self._scores: dict[str, dict[str, int]] = {}

    def select(self, candidates: list[dict], *, group: str) -> dict:
        priority = max(account["priority"] for account in candidates)
        eligible = [account for account in candidates if account["priority"] == priority]
        previous = self._scores.get(group, {})
        scores = {
            account["pool_account_id"]: previous.get(account["pool_account_id"], 0) + account["weight"]
            for account in eligible
        }
        selected = max(eligible, key=lambda account: scores[account["pool_account_id"]])
        scores[selected["pool_account_id"]] -= sum(account["weight"] for account in eligible)
        self._scores[group] = scores
        return selected

    def forget(self, account_id: str) -> None:
        for scores in self._scores.values():
            scores.pop(account_id, None)
