import importlib
from typing import Any

from module_paths import qualify_module_path
from strategies.futures.base import FuturesStrategy
from utils.events import EventChannel, observe_event
from utils.logger import get_logger

FIXED_STRATEGY_PACKAGE = "strategies.futures.fixed."
AI_STRATEGY_PACKAGE = "strategies.futures.ai."


class StrategyLoadError(ValueError):
    pass


def load_strategy(
    module_name: str,
    class_name: str,
    strategy_params: dict | None = None,
) -> FuturesStrategy:
    module_name = _normalize_module_name(module_name)

    if not module_name.startswith("strategies."):
        raise StrategyLoadError(
            "Estrategias de Futures devem ficar dentro do pacote 'strategies'. "
            f"Modulo recebido: {module_name}"
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise StrategyLoadError(
            f"Nao foi possivel importar o modulo da estrategia: {module_name}"
        ) from error

    try:
        strategy_class = getattr(module, class_name)
    except AttributeError as error:
        raise StrategyLoadError(
            f"Classe de estrategia '{class_name}' nao encontrada em {module_name}"
        ) from error

    if not isinstance(strategy_class, type):
        raise StrategyLoadError(f"{module_name}.{class_name} nao e uma classe")

    if not issubclass(strategy_class, FuturesStrategy):
        raise StrategyLoadError(f"{module_name}.{class_name} precisa herdar de FuturesStrategy")

    _validate_strategy_location(module_name, class_name)

    strategy, instantiation_source = _instantiate_strategy(strategy_class, strategy_params or {})
    get_logger(__name__).info(
        "Estrategia carregada | strategy=%s.%s | instantiation=%s | strategy_params=%s",
        module_name,
        class_name,
        instantiation_source,
        "presentes" if strategy_params else "vazios",
    )
    observe_event(
        "strategy_loaded",
        component=__name__,
        category="strategy",
        channel=EventChannel.AUDIT,
        strategy=f"{module_name}.{class_name}",
        strategy_module=module_name,
        strategy_class=class_name,
        instantiation_source=instantiation_source,
        strategy_params_present=bool(strategy_params),
    )

    if not isinstance(strategy, FuturesStrategy):
        raise StrategyLoadError(f"{module_name}.{class_name} retornou uma instancia incompatavel")

    return strategy


def _instantiate_strategy(
    strategy_class: type[FuturesStrategy],
    strategy_params: dict,
) -> tuple[FuturesStrategy, str]:
    from_params = getattr(strategy_class, "from_params", None)
    if strategy_params and callable(from_params):
        strategy: Any = from_params(strategy_params)
        return strategy, "from_params"

    from_env = getattr(strategy_class, "from_env", None)

    if callable(from_env):
        if strategy_params:
            get_logger(__name__).warning(
                "strategy_params foram informados, mas %s nao tem from_params; usando from_env",
                strategy_class.__name__,
            )
        strategy = from_env()
        return strategy, "from_env"

    if strategy_params:
        get_logger(__name__).warning(
            "strategy_params foram informados, mas %s nao tem from_params/from_env; "
            "usando construtor padrao",
            strategy_class.__name__,
        )
    return strategy_class(), "default_constructor"


def _normalize_module_name(module_name: str) -> str:
    return qualify_module_path(module_name)


def _validate_strategy_location(module_name: str, class_name: str) -> None:
    if module_name.startswith(AI_STRATEGY_PACKAGE):
        return
    if module_name.startswith(FIXED_STRATEGY_PACKAGE):
        return
    raise StrategyLoadError(
        "Estrategias fixas devem ficar em 'strategies.futures.fixed.<arquivo>'. "
        f"Estrategia recebida: {module_name}.{class_name}"
    )
