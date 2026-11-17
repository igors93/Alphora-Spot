from __future__ import annotations

from domain.models import FuturesPosition, PositionDirection


def is_open_position(position: FuturesPosition) -> bool:
    return position.is_open


def is_expected_position(position: FuturesPosition, direction: PositionDirection) -> bool:
    return position.is_open and position.direction == direction
