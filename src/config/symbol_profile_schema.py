from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from module_paths import qualify_module_path

RETIRED_SYMBOL_PROFILE_STRATEGY_KEYS = (
    "strategy_module",
    "strategy_class",
)


class SymbolProfileStrategyConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SymbolProfileCanonicalization:
    payload: dict[str, Any]
    removed_keys: tuple[str, ...]
    warnings: tuple[str, ...]


def canonicalize_symbol_profile_payload(
    payload: Mapping[str, Any],
    *,
    global_strategy_module: str | None = None,
    global_strategy_class: str | None = None,
    reject_strategy_conflicts: bool = True,
) -> SymbolProfileCanonicalization:
    if not isinstance(payload, Mapping):
        raise ValueError("Cada perfil de moeda precisa ser um objeto JSON")

    result = dict(payload)
    removed: list[str] = []
    warnings: list[str] = []
    symbol = str(payload.get("symbol", "")).upper().strip() or "?"

    for key in RETIRED_SYMBOL_PROFILE_STRATEGY_KEYS:
        if key not in result:
            continue

        raw_value = str(result[key] or "").strip()
        if not raw_value:
            del result[key]
            removed.append(key)
            continue

        global_value: str | None
        if key == "strategy_module":
            # Profiles saved before the alphora namespace use the old path.
            raw_value = qualify_module_path(raw_value)
            global_value = qualify_module_path(global_strategy_module or "") or None
        else:
            global_value = (global_strategy_class or "").strip() or None

        if global_value is None:
            if reject_strategy_conflicts:
                raise SymbolProfileStrategyConflict(
                    f"{symbol}: o perfil contém '{key}' mas a estratégia global não foi "
                    "fornecida para validar conflito. "
                    "Defina a estratégia global em ALPHORA_STRATEGY_MODULE e "
                    "ALPHORA_STRATEGY_CLASS antes de iniciar."
                )
            del result[key]
            removed.append(key)
            warnings.append(
                f"{symbol}: '{key}' removido do perfil (estratégia global não disponível)."
            )
            continue

        if raw_value == global_value:
            del result[key]
            removed.append(key)
            warnings.append(
                f"{symbol}: '{key}={raw_value}' removido do perfil — "
                "a estratégia já está definida globalmente."
            )
        else:
            if reject_strategy_conflicts:
                raise SymbolProfileStrategyConflict(
                    f"{symbol}: o perfil solicita '{key}={raw_value}' mas a estratégia global é "
                    f"'{global_value}'. Perfis por símbolo não podem selecionar estratégia. "
                    "Corrija a estratégia global ou remova o campo do perfil."
                )
            del result[key]
            removed.append(key)
            warnings.append(
                f"{symbol}: '{key}' em conflito removido do perfil "
                f"(legado={raw_value!r}, global={global_value!r})."
            )

    return SymbolProfileCanonicalization(
        payload=result,
        removed_keys=tuple(removed),
        warnings=tuple(warnings),
    )


def canonicalize_profiles_for_persistence(
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strip retired keys without checking conflict — used only when writing to disk."""
    result = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("Cada perfil precisa ser um objeto JSON")
        cleaned = {
            k: v for k, v in profile.items() if k not in RETIRED_SYMBOL_PROFILE_STRATEGY_KEYS
        }
        result.append(cleaned)
    return result


def atomic_write_json(
    path: os.PathLike[str] | str,
    payload: Any,
) -> None:
    import json
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
