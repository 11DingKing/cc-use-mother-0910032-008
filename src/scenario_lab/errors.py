"""情景试算的领域异常。"""


class DomainError(Exception):
    """领域规则冲突。"""


class NotFoundError(DomainError):
    """对象不存在。"""


class AlreadyExistsError(DomainError):
    """对象已存在且不可覆盖。"""


class StateError(DomainError):
    """状态机不允许的操作。"""


class ReleaseError(DomainError):
    """正式发布违规。"""


class BatchInterrupted(Exception):
    """批次被中断的控制流信号（非错误），断点已落盘，可恢复。"""
