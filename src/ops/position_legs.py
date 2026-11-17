"""Position-leg reading shared by the operational tools.

``positionRisk`` returns one row per leg: a single ``BOTH`` row in One-way mode,
separate ``LONG``/``SHORT`` rows in Hedge Mode. Reading only the first nonzero
row hides the other legs, so every tool that acts on a position enumerates all
of them and refuses what a single One-way leg cannot describe.
"""

from decimal import Decimal, InvalidOperation


def is_hedge_mode(position_mode: dict) -> bool:
    return position_mode.get("dualSidePosition") in {True, "true", "TRUE", "True"}


def extract_open_legs(positions: list[dict]) -> list[dict]:
    if not positions:
        raise ValueError("Resposta positionRisk vazia; estado da posicao desconhecido")
    legs = []
    for item in positions:
        if not isinstance(item, dict) or "positionAmt" not in item:
            raise ValueError("Resposta positionRisk sem positionAmt")
        try:
            amount = Decimal(str(item["positionAmt"]))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Resposta positionRisk com positionAmt invalido") from error
        if not amount.is_finite():
            raise ValueError("Resposta positionRisk com positionAmt nao finito")
        if amount == 0:
            continue
        legs.append(
            {
                "is_open": True,
                "direction": "LONG" if amount > 0 else "SHORT",
                "quantity": str(abs(amount)),
                "entry_price": item.get("entryPrice", "0"),
                "position_side": str(item.get("positionSide") or "BOTH").upper(),
            }
        )
    return legs


def position_summary(legs: list[dict]) -> dict:
    if not legs:
        return {"is_open": False, "direction": "NONE", "quantity": "0", "entry_price": "0"}
    if len(legs) == 1:
        return {key: value for key, value in legs[0].items() if key != "position_side"}
    return {"is_open": True, "direction": "MULTIPLE", "quantity": None, "entry_price": None}


def unsupported_legs_reason(legs: list[dict], *, hedge_mode: bool) -> str | None:
    """Return why these legs are not a single One-way position, or None."""

    if not legs:
        return None
    if hedge_mode:
        return "HEDGE_MODE"
    if len(legs) > 1:
        return "MULTIPLE_OPEN_LEGS"
    if legs[0]["position_side"] != "BOTH":
        return "UNEXPECTED_POSITION_SIDE"
    return None
