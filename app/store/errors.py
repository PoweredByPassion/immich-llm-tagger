"""领域校验错误。

`Problem` 用 **i18n key + 位置参数** 描述一条问题，而非成品中文串——store 层
不再持有用户可见文案（本地化由 web 层用当前请求语言渲染，见
`app.web.i18n.render_problems`）。`str()` 只回 key，供日志/调试。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    key: str
    args: tuple = ()

    def __str__(self) -> str:
        return self.key


class ValidationError(Exception):
    """配置校验失败。problems 列出全部问题，便于 UI 一次显示完。"""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("；".join(str(p) for p in problems))
