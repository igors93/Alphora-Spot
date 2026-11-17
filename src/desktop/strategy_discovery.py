import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from strategies.futures.base import FuturesStrategy

FIXED_STRATEGY_PACKAGE = "strategies.futures.fixed."
AI_STRATEGY_PACKAGE = "strategies.futures.ai."


@dataclass(frozen=True)
class StrategyInfo:
    module_name: str
    class_name: str
    qualified_name: str
    docstring: str
    has_from_env: bool
    has_from_params: bool = False
    has_score_symbol: bool = False
    has_intent_symbol: bool = False
    has_decide_symbol: bool = False
    required_history: int | None = None
    required_timeframes: list[str] | None = None
    required_indicators: list[str] | None = None
    supports_short: bool | None = None
    strategy_category: str = ""
    hidden_from_fixed_list: bool = False
    import_error: str = ""


_DEFAULT_STRATEGIES_DIR = Path(__file__).parent.parent / "strategies" / "futures"


def discover_futures_strategies(
    strategies_dir: str | Path | None = None,
    *,
    include_hidden: bool = False,
) -> list[StrategyInfo]:
    module_names = _discover_module_names(strategies_dir)
    strategies: list[StrategyInfo] = []

    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            strategies.append(
                StrategyInfo(
                    module_name=module_name,
                    class_name="<import_error>",
                    qualified_name=module_name,
                    docstring="",
                    has_from_env=False,
                    import_error=str(error),
                )
            )
            continue

        for _, strategy_class in inspect.getmembers(module, inspect.isclass):
            if strategy_class is FuturesStrategy:
                continue
            if inspect.isabstract(strategy_class):
                continue
            if strategy_class.__module__ != module.__name__:
                continue
            if not issubclass(strategy_class, FuturesStrategy):
                continue

            hidden = bool(getattr(strategy_class, "hide_from_fixed_strategy_list", False))
            import_error = ""
            if not hidden and not module_name.startswith(FIXED_STRATEGY_PACKAGE):
                import_error = (
                    "Estrategias fixas devem ficar em strategies.futures.fixed.<arquivo>."
                )
            if hidden and not include_hidden:
                continue

            strategies.append(
                StrategyInfo(
                    module_name=module_name,
                    class_name=strategy_class.__name__,
                    qualified_name=f"{module_name}.{strategy_class.__name__}",
                    docstring=inspect.getdoc(strategy_class) or "",
                    has_from_env=callable(getattr(strategy_class, "from_env", None)),
                    has_from_params=callable(getattr(strategy_class, "from_params", None)),
                    has_score_symbol=callable(getattr(strategy_class, "score_symbol", None)),
                    has_intent_symbol=callable(getattr(strategy_class, "intent_symbol", None)),
                    has_decide_symbol=callable(getattr(strategy_class, "decide_symbol", None)),
                    required_history=_metadata_value(strategy_class, "required_history"),
                    required_timeframes=_metadata_list(strategy_class, "required_timeframes"),
                    required_indicators=_metadata_list(strategy_class, "required_indicators"),
                    supports_short=_metadata_value(strategy_class, "supports_short"),
                    strategy_category=str(getattr(strategy_class, "strategy_category", "")),
                    hidden_from_fixed_list=hidden,
                    import_error=import_error,
                )
            )

    return sorted(strategies, key=lambda item: item.qualified_name)


def _discover_module_names(strategies_dir: str | Path | None) -> list[str]:
    if strategies_dir is not None:
        return _filesystem_module_names(Path(strategies_dir))

    filesystem_names = _filesystem_module_names(_DEFAULT_STRATEGIES_DIR)
    if filesystem_names:
        return filesystem_names

    try:
        package = importlib.import_module("strategies.futures")
        package_paths = getattr(package, "__path__", ())
    except Exception:
        return []

    names = [
        item.name
        for item in pkgutil.walk_packages(package_paths, prefix="strategies.futures.")
        if not item.ispkg
    ]
    return sorted(dict.fromkeys(names))


def _filesystem_module_names(directory: Path) -> list[str]:
    if not directory.exists():
        return []

    names = []
    for file_path in sorted(directory.rglob("*.py")):
        if file_path.name == "__init__.py":
            continue
        names.append(_module_name_from_path(file_path))
    return sorted(dict.fromkeys(names))


def _metadata_value(strategy_class, name: str):
    metadata = getattr(strategy_class, "metadata", None)
    if metadata is not None and hasattr(metadata, name):
        return getattr(metadata, name)
    if hasattr(strategy_class, name):
        return getattr(strategy_class, name)
    return None


def _metadata_list(strategy_class, name: str) -> list[str] | None:
    value = _metadata_value(strategy_class, name)
    if value is None:
        return None
    return [str(item) for item in value]


def _module_name_from_path(file_path: Path) -> str:
    parts = list(file_path.with_suffix("").parts)
    try:
        strategies_index = parts.index("strategies")
    except ValueError:
        return ".".join(parts)

    return ".".join(parts[strategies_index:])
