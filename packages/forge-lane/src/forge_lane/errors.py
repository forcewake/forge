"""Stable outcomes for automation; exception text never contains wire secrets."""

from enum import IntEnum


class ExitCode(IntEnum):
    COMPLETED = 0
    CONFIGURATION = 2
    AUTHENTICATION = 3
    APPROVAL = 4
    DRIVER = 5
    DEADLINE = 6
    TRANSPORT = 7
    GIT = 8
    EMPTY_DIFF = 9
    PAUSED = 10
    CANCELLED = 11
    CHECKPOINT = 12


class LaneFailure(Exception):
    def __init__(self, code: ExitCode, classification: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.classification = classification
        self.detail = detail
