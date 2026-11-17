import json
import os
from math import isfinite
from numbers import Real


def _get_env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    value = raw_value.strip().lower()

    if value in {"true", "1", "yes", "y", "sim"}:
        return True

    if value in {"false", "0", "no", "n", "nao", "não"}:
        return False

    raise ValueError(f"{name} precisa ser true ou false")


def _get_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} precisa ser um numero inteiro") from error


def _get_non_negative_int(name: str, default: int) -> int:
    value = _get_int(name, default)
    if value < 0:
        raise ValueError(f"{name} precisa ser maior ou igual a zero")
    return value


def _get_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)

    if raw_value is None:
        value = float(default)
    else:
        try:
            value = float(raw_value)
        except ValueError as error:
            raise ValueError(f"{name} precisa ser um numero") from error

    _validate_positive_number(name, value)
    return value


def _get_non_negative_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        value = float(default)
    else:
        try:
            value = float(raw_value)
        except ValueError as error:
            raise ValueError(f"{name} precisa ser um numero") from error

    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{name} precisa ser um numero valido")

    if value < 0:
        raise ValueError(f"{name} precisa ser maior ou igual a zero")

    return value


def _get_probability(name: str, default: float) -> float:
    value = _get_non_negative_float(name, default)
    _validate_probability(name, value)
    return value


def _get_env_alias(primary_name: str, legacy_name: str) -> str | None:
    primary_value = os.getenv(primary_name)
    if primary_value is not None and primary_value.strip():
        return primary_value

    legacy_value = os.getenv(legacy_name)
    if legacy_value is not None and legacy_value.strip():
        return legacy_value

    return None


def _get_non_negative_float_alias(
    primary_name: str,
    legacy_name: str,
    default: float,
) -> float:
    raw_value = _get_env_alias(primary_name, legacy_name)

    if raw_value is None:
        value = float(default)
    else:
        try:
            value = float(raw_value)
        except ValueError as error:
            raise ValueError(f"{primary_name}/{legacy_name} precisa ser um numero") from error

    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{primary_name}/{legacy_name} precisa ser um numero valido")

    if value < 0:
        raise ValueError(f"{primary_name}/{legacy_name} precisa ser maior ou igual a zero")

    return value


def _get_finite_float_alias(
    primary_name: str,
    legacy_name: str,
    default: float,
) -> float:
    raw_value = _get_env_alias(primary_name, legacy_name)

    if raw_value is None:
        value = float(default)
    else:
        try:
            value = float(raw_value)
        except ValueError as error:
            raise ValueError(f"{primary_name}/{legacy_name} precisa ser um numero") from error

    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{primary_name}/{legacy_name} precisa ser um numero valido")

    return value


def _get_json_dict(name: str, default: dict) -> dict:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return dict(default)
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} precisa conter JSON valido") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} precisa ser um objeto JSON")
    return value


def _validate_finite_number(name: str, value: float) -> None:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{name} precisa ser um numero valido")


def _validate_non_negative_number(name: str, value: float) -> None:
    _validate_finite_number(name, value)

    if value < 0:
        raise ValueError(f"{name} precisa ser maior ou igual a zero")


def _validate_probability(name: str, value: float) -> None:
    _validate_finite_number(name, value)
    if value < 0 or value > 1:
        raise ValueError(f"{name} precisa ficar entre 0 e 1")


def _validate_positive_number(name: str, value: float) -> None:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{name} precisa ser um numero valido")

    if value <= 0:
        raise ValueError(f"{name} precisa ser maior que zero")
