"""枚举使用标准库优先，3.10 使用同语义回移植。"""
import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum as StrEnum
else:
    from backports.strenum import StrEnum as StrEnum
