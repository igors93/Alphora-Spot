"""Request handlers of the desktop bridge.

Every handler is a thin adapter over ``desktop.services``: it loads the local
configuration, calls the service and returns plain JSON. Configuration writes are
serialized by one lock so concurrent requests never interleave read-modify-write cycles.
"""

from __future__ import annotations

import csv
import os
import platform
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.enums import (
    ExitModelType,
    ProtectionPolicy,
    ProtectionWorkingType,
    RecoveryAction,
)
from desktop.bridge.errors import BridgeError
from desktop.runtime_paths import prepare_workspace, resolve_workspace_root
from desktop.services.ai_training_service import (
    apply_ai_training_result,
    normalize_ai_training_settings,
    parse_ai_training_result,
    prepare_ai_training_plan,
    summarize_selected_model,
    validate_ai_training_settings,
)
from desktop.services.backtest_service import (
    artifact_map,
    fetch_symbol_rules,
    load_backtest_result,
    normalize_backtest_settings,
    parse_downloaded_path,
    prepare_backtest_plan,
    prepare_download_plan,
    validate_backtest_settings,
)
from desktop.services.credentials_service import (
    CREDENTIAL_ENVIRONMENTS,
    prepare_credentials_update,
    secure_credentials_file,
    summarize_credentials,
)
from desktop.services.observability_service import (
    SNAPSHOT_SECTIONS,
    build_observability_snapshot,
    first_existing_source,
    resolve_project_path,
    rotate_observability_journals,
)
from desktop.services.operation_models import (
    ConfirmationRequest,
    ConfirmationResponse,
    OperationLaunchPlan,
    OperationMessage,
    PreflightResult,
    PreflightStatus,
    authorize_operation,
)
from desktop.services.operation_service import OperationPreparationService
from desktop.services.preflight_service import PreflightService, apply_partial_approval
from desktop.services.strategy_service import (
    StrategyDescriptor,
    allowed_activation_levels,
    build_intelligence_diagnostics,
    discover_strategy_descriptors,
    format_strategy_params,
    normalize_intelligence_settings,
    prepare_strategy_save,
    validate_global_ai_model,
    validate_intelligence_settings,
    validate_strategy_configuration,
)
from desktop.services.symbol_service import (
    build_symbol_rows,
    duplicate_symbol_profile,
    load_governor_state,
    new_symbol_profile,
    normalize_portfolio_risk,
    normalize_symbol_profile,
    validate_portfolio_risk,
    validate_symbol_profile,
)
from desktop.settings_store import (
    PORTFOLIO_RISK_PATH,
    SETTINGS_PATH,
    SYMBOL_PROFILES_PATH,
    PortfolioRiskStore,
    SettingsStore,
    SymbolProfilesStore,
    allowed_intelligent_activation_levels,
    normalize_environment,
    operational_module_modes_for_settings,
)
from domain.timeframe import SUPPORTED_TIMEFRAMES, TIMEFRAME_LABELS
from operational_module_modes import MODULE_ENUMS, MODULE_MODE_ATTRS
from utils.audit_logger import append_run_log
from utils.events import EventChannel, observe_event
from utils.privacy import sanitize_for_audit

EXECUTION_MODES = ("DRY_RUN", "PAPER_LIVE", "TESTNET_LIVE", "REAL_LIVE")
ENVIRONMENTS = ("TESTNET", "REAL")
STRATEGY_MODES = ("FIXED", "PROBABILISTIC_AI")
SYMBOL_SCOPES = ("CURRENT", "ENABLED")
TRAINING_KINDS = ("GLOBAL", "REFINED", "ALL")
EVENT_SEVERITIES = (
    "INFO",
    "SHADOW",
    "APLICADO",
    "AGUARDANDO",
    "WAIT",
    "REVISÃO",
    "BLOQUEADO",
    "ERRO",
)

_MODULE_LABELS = {
    "dynamic_exit_mode": "Saída dinâmica",
    "smart_sizing_mode": "Sizing inteligente",
    "smart_execution_mode": "Execução inteligente",
    "state_reconciliation_mode": "Reconciliação de estado",
    "learning_advisory_mode": "Learning advisory",
    "portfolio_allocator_mode": "Alocador de portfólio",
}

_MODE_LABELS = {
    "OFF": ("Desligado", "Módulo desligado."),
    "OBSERVE": ("Observação", "Calcula diagnósticos, mas não altera decisões."),
    "SHADOW": ("Sombra", "Simula a ação e registra auditoria, sem alterar execução."),
    "ACTIVE": ("Ativo", "Pode alterar decisões conforme as regras do módulo."),
    "REDUCE_ONLY": ("Reduzir risco", "Só pode reduzir risco, nunca aumentar."),
    "ALERT": ("Alertar", "Alerta divergências sem bloquear."),
    "BLOCK": ("Bloquear se divergente", "Pode bloquear novas entradas se houver divergência."),
    "REPAIR_SAFE": ("Reparar se seguro", "Pode tentar reparo apenas em casos seguros."),
    "REDUCE_PRIORITY": ("Reduzir prioridade", "Pode reduzir prioridade, sem bloquear trade."),
    "PAPER_BLOCK": ("Bloquear em paper", "Pode bloquear somente em paper."),
}

_PROBABILITY_FIELDS = (
    ("ai_min_long_probability", "Probabilidade mínima long"),
    ("ai_min_short_probability", "Probabilidade mínima short"),
    ("ai_min_direction_gap", "Gap mínimo de direção"),
    ("ai_min_expected_value_bps", "Expected value mínimo (bps)"),
    ("ai_close_long_min_short_probability", "Fechar long quando prob. short atingir"),
    ("ai_close_short_min_long_probability", "Fechar short quando prob. long atingir"),
)

_PORTFOLIO_FIELDS = (
    ("max_open_positions", "Máx. posições abertas", "integer"),
    ("max_total_exposure_usdt", "Exposição total máxima (USDT)", "decimal"),
    ("max_long_exposure_usdt", "Exposição long máxima (USDT)", "decimal"),
    ("max_short_exposure_usdt", "Exposição short máxima (USDT)", "decimal"),
    ("max_symbol_exposure_usdt", "Exposição máxima por símbolo", "decimal"),
    ("max_group_exposure_usdt", "Exposição máxima por grupo", "decimal"),
    ("max_daily_loss_usdt", "Perda diária máxima (USDT)", "decimal"),
    ("stop_after_daily_profit_usdt", "Parar após lucro diário (USDT)", "decimal"),
    ("max_correlated_positions", "Máx. posições correlacionadas", "integer"),
    ("max_new_positions_per_cycle", "Novas posições por ciclo", "integer"),
    ("governor_max_symbols_per_cycle", "Símbolos por ciclo do Governor", "integer"),
    ("allow_multiple_same_direction", "Permitir várias posições na mesma direção", "boolean"),
    (
        "allow_multiple_positions_same_symbol",
        "Permitir várias posições no mesmo símbolo",
        "boolean",
    ),
)

_INTELLIGENCE_BOOL_DEFAULTS = {
    "ai_probabilistic_enabled": False,
    "ai_require_model_approved": True,
    "ai_use_symbol_refined_models": True,
    "ai_fallback_to_global_model": True,
    "operational_module_modes_customized": False,
}
_INTELLIGENCE_TEXT_KEYS = (
    "intelligent_activation_level",
    "ai_global_model_path",
    "ai_operational_timeframe",
    "ai_operational_model_id",
    *(key for key, _ in _PROBABILITY_FIELDS),
    *MODULE_MODE_ATTRS.values(),
)

_BACKTEST_TEXT_KEYS = (
    "symbol",
    "backtest_interval",
    "backtest_start",
    "backtest_end",
    "backtest_csv_path",
    "secondary_csv_path",
    "backtest_symbol_scope",
    "strategy_selection_mode",
    "backtest_quantity_step",
    "backtest_min_quantity",
    "backtest_min_notional",
    "backtest_price_tick",
    "backtest_initial_balance",
    "maker_fee_pct",
    "taker_fee_pct",
    "slippage_bps",
    "spread_bps",
    "default_funding_rate",
)
_BACKTEST_BOOL_KEYS = ("funding_enabled", "use_secondary_csv")
_RULE_KEYS = {
    "quantity_step": "backtest_quantity_step",
    "min_quantity": "backtest_min_quantity",
    "min_notional": "backtest_min_notional",
    "price_tick": "backtest_price_tick",
}

_EQUITY_POINTS = 720
_MODEL_CACHE_LIMIT = 64


@dataclass
class _OperationSession:
    plan: OperationLaunchPlan | None = None
    preflight: PreflightResult | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        self.plan = None
        self.preflight = None


class Bridge:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._config_lock = threading.RLock()
        self._settings_store = SettingsStore(root / SETTINGS_PATH)
        self._profiles_store = SymbolProfilesStore(root / SYMBOL_PROFILES_PATH)
        self._portfolio_store = PortfolioRiskStore(root / PORTFOLIO_RISK_PATH)
        self._preparation = OperationPreparationService()
        self._preflight = PreflightService(project_root=root, timeout_seconds=90.0)
        self._operation = _OperationSession()
        self._descriptors: tuple[StrategyDescriptor, ...] = ()
        self._discovery_lock = threading.Lock()
        self._model_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._model_cache_lock = threading.Lock()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "operation.overview": self.operation_overview,
            "operation.prepare": self.operation_prepare,
            "operation.authorize": self.operation_authorize,
            "operation.confirm_preflight": self.operation_confirm_preflight,
            "operation.cancel": self.operation_cancel,
            "symbols.load": self.symbols_load,
            "symbols.rows": self.symbols_rows,
            "symbols.new": self.symbols_new,
            "symbols.duplicate": self.symbols_duplicate,
            "symbols.check": self.symbols_check,
            "symbols.save": self.symbols_save,
            "symbols.files": self.symbols_files,
            "strategy.load": self.strategy_load,
            "strategy.discover": self.strategy_discover,
            "strategy.save": self.strategy_save,
            "intelligence.load": self.intelligence_load,
            "intelligence.models": self.intelligence_models,
            "intelligence.validate_model": self.intelligence_validate_model,
            "intelligence.save": self.intelligence_save,
            "intelligence.diagnostics": self.intelligence_diagnostics,
            "credentials.load": self.credentials_load,
            "credentials.save": self.credentials_save,
            "credentials.clear": self.credentials_clear,
            "backtest.load": self.backtest_load,
            "backtest.save": self.backtest_save,
            "backtest.download": self.backtest_download,
            "backtest.download_done": self.backtest_download_done,
            "backtest.rules": self.backtest_rules,
            "backtest.run": self.backtest_run,
            "backtest.result": self.backtest_result,
            "training.load": self.training_load,
            "training.save": self.training_save,
            "training.start": self.training_start,
            "training.done": self.training_done,
            "observability.snapshot": self.observability_snapshot,
            "observability.rotate": self.observability_rotate,
            "observability.locate": self.observability_locate,
            "audit.task": self.audit_task,
            "audit.output": self.audit_output,
        }

    @classmethod
    def from_environment(cls) -> Bridge:
        root = prepare_workspace(resolve_workspace_root(source_hint=Path(__file__)))
        os.environ["ALPHORA_PROJECT_ROOT"] = str(root)
        os.chdir(root)
        return cls(root)

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handler = self._handlers.get(method)
        if handler is None:
            raise BridgeError("Método desconhecido", f"O núcleo não reconhece '{method}'.")
        return handler(params)

    def hello(self) -> dict[str, Any]:
        from desktop.bridge.protocol import BRIDGE_PROTOCOL_VERSION  # noqa: PLC0415
        from observability.domain.run_context import get_run_id  # noqa: PLC0415

        return {
            "protocol": BRIDGE_PROTOCOL_VERSION,
            "root": str(self.root),
            "run_id": get_run_id(),
            "python": sys.executable,
            "python_version": platform.python_version(),
            "pid": os.getpid(),
            "options": {
                "execution_modes": list(EXECUTION_MODES),
                "environments": list(ENVIRONMENTS),
                "strategy_modes": list(STRATEGY_MODES),
                "symbol_scopes": list(SYMBOL_SCOPES),
                "event_severities": list(EVENT_SEVERITIES),
                "credential_environments": list(CREDENTIAL_ENVIRONMENTS),
                "timeframes": [
                    {
                        "value": value,
                        "label": TIMEFRAME_LABELS.get(value, value),
                        "minutes": duration // 60_000,
                    }
                    for value, duration in SUPPORTED_TIMEFRAMES.items()
                ],
                "activation_levels": {
                    mode: list(allowed_intelligent_activation_levels(mode))
                    for mode in EXECUTION_MODES
                },
                "modules": [
                    {
                        "key": attr,
                        "label": _MODULE_LABELS.get(attr, attr),
                        "options": [
                            {
                                "value": member.value,
                                "label": _MODE_LABELS.get(member.value, (member.value, ""))[0],
                                "description": _MODE_LABELS.get(member.value, ("", ""))[1],
                            }
                            for member in MODULE_ENUMS[module]
                        ],
                    }
                    for module, attr in MODULE_MODE_ATTRS.items()
                ],
                "probability_fields": [
                    {"key": key, "label": label} for key, label in _PROBABILITY_FIELDS
                ],
                "portfolio_fields": [
                    {"key": key, "label": label, "kind": kind}
                    for key, label, kind in _PORTFOLIO_FIELDS
                ],
                "profile_choices": {
                    "margin_type": ["ISOLATED", "CROSS"],
                    "exit_model": [member.value for member in ExitModelType],
                    "protection_policy": [member.value for member in ProtectionPolicy],
                    "protection_working_type": [member.value for member in ProtectionWorkingType],
                    "recovery_action": [member.value for member in RecoveryAction],
                },
            },
        }

    # ------------------------------------------------------------------ operation

    def operation_overview(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings, profiles = self._load_settings_and_profiles("Configuração inválida")
        enabled = [
            str(profile.get("symbol", "")).upper().strip()
            for profile in profiles
            if _as_bool(profile.get("enabled", True)) and str(profile.get("symbol", "")).strip()
        ]
        mode = str(settings.get("execution_mode", "DRY_RUN")).upper()
        environment = str(settings.get("environment", "TESTNET")).upper()
        return {
            "execution_mode": mode if mode in EXECUTION_MODES else "DRY_RUN",
            "environment": environment if environment in ENVIRONMENTS else "TESTNET",
            "strategy": str(settings.get("strategy_class", "") or "-"),
            "strategy_mode": str(settings.get("strategy_selection_mode", "FIXED")),
            "loop_interval": str(settings.get("loop_interval_seconds", "-")),
            "symbols": enabled,
            "ai_enabled": _as_bool(settings.get("ai_probabilistic_enabled", False)),
            "credentials": {
                env: summarize_credentials(settings, env).configured
                for env in CREDENTIAL_ENVIRONMENTS
            },
        }

    def operation_prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        mode = _choice(params.get("execution_mode"), EXECUTION_MODES, "Modo de execução")
        environment = _choice(params.get("environment"), ENVIRONMENTS, "Ambiente")
        settings, profiles = self._load_settings_and_profiles("Configuração inválida")
        settings["execution_mode"] = mode
        settings["environment"] = environment

        with self._operation.lock:
            self._operation.reset()
            result = self._preparation.prepare(settings, profiles)
            messages = [_message(item) for item in result.messages]
            if result.blocked or result.plan is None:
                last = result.messages[-1] if result.messages else None
                return {
                    "status": "BLOCKED",
                    "messages": messages,
                    "title": last.title if last else "Início bloqueado",
                    "message": last.message if last else "A operação não pôde ser preparada.",
                }
            self._operation.plan = result.plan
            return {
                "status": result.status.value,
                "messages": messages,
                "confirmations": [_confirmation(item) for item in result.plan.confirmations],
                "summary": result.plan.summary,
            }

    def operation_authorize(self, params: dict[str, Any]) -> dict[str, Any]:
        responses = [_response(item) for item in params.get("responses", []) or []]
        with self._operation.lock:
            plan = self._operation.plan
            if plan is None:
                raise BridgeError("Plano expirado", "Prepare a operação novamente.")
            authorization = authorize_operation(plan, {item.key: item for item in responses})
            if not authorization.authorized:
                self._operation.reset()
                details = ", ".join(
                    (
                        *authorization.missing_confirmation_keys,
                        *authorization.invalid_confirmation_keys,
                    )
                )
                return _blocked(
                    "Confirmação inválida",
                    f"A inicialização foi bloqueada: {details or 'confirmação desconhecida'}.",
                )
            try:
                self._persist_plan(plan)
            except (OSError, TypeError, ValueError) as error:
                self._operation.reset()
                return _blocked("Falha ao salvar configuração", _safe_text(error))

        # The preflight runs a subprocess against Binance; do not hold the session lock.
        preflight = self._preflight.run(plan.environment_dict())
        with self._operation.lock:
            if self._operation.plan is not plan:
                raise BridgeError("Plano expirado", "A operação foi cancelada.")
            return self._preflight_stage(plan, preflight)

    def operation_confirm_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        response = _response(params.get("response") or {})
        with self._operation.lock:
            plan = self._operation.plan
            result = self._operation.preflight
            if plan is None or result is None or result.confirmation is None:
                raise BridgeError("Plano expirado", "Prepare a operação novamente.")
            if not result.confirmation.accepts(response):
                self._operation.reset()
                return _blocked("Confirmação inválida", "O preflight não foi autorizado.")

            messages: list[dict[str, Any]] = []
            profiles_changed = False
            if result.status is PreflightStatus.PARTIAL:
                try:
                    with self._config_lock:
                        profiles = apply_partial_approval(self._profiles_store.load(), result)
                        self._profiles_store.save(profiles)
                except (OSError, TypeError, ValueError) as error:
                    self._operation.reset()
                    return _blocked("Falha ao aplicar preflight parcial", _safe_text(error))
                profiles_changed = True
                messages.append(
                    {
                        "title": "Preflight parcial",
                        "message": "Símbolos aprovados: " + ", ".join(result.approved_symbols),
                        "severity": "WARNING",
                    }
                )
            launch = self._launch_payload(plan)
            launch["messages"] = messages
            launch["profiles_changed"] = profiles_changed
            return launch

    def operation_cancel(self, _params: dict[str, Any]) -> dict[str, Any]:
        with self._operation.lock:
            self._operation.reset()
        return {}

    def _preflight_stage(
        self, plan: OperationLaunchPlan, result: PreflightResult
    ) -> dict[str, Any]:
        base = {
            "messages": [_message(item) for item in result.messages],
            "output": result.output,
            "profiles_changed": plan.multi_symbol,
        }
        if result.status is PreflightStatus.READY:
            return {**base, **self._launch_payload(plan)}
        if result.requires_confirmation and result.confirmation is not None:
            self._operation.preflight = result
            return {**base, "stage": "confirm", "confirmation": _confirmation(result.confirmation)}
        self._operation.reset()
        last = result.messages[-1] if result.messages else None
        return {
            **base,
            "stage": "blocked",
            "title": last.title if last else "Preflight bloqueado",
            "message": last.message if last else "O preflight bloqueou a inicialização.",
        }

    def _launch_payload(self, plan: OperationLaunchPlan) -> dict[str, Any]:
        self._operation.reset()
        return {
            "stage": "launch",
            "command": list(plan.command),
            "env": plan.environment_dict(),
            "summary": plan.summary,
            "mode": plan.mode.value,
            "symbols": [str(item.get("symbol", "")) for item in plan.runtime_profiles],
        }

    def _persist_plan(self, plan: OperationLaunchPlan) -> None:
        with self._config_lock:
            self._settings_store.save(plan.settings_dict())
            if plan.multi_symbol:
                self._profiles_store.save(plan.runtime_profiles_list())
                return
            if not plan.symbol_profiles_path:
                raise ValueError("Caminho do perfil single-symbol não foi definido.")
            SymbolProfilesStore(self.root / plan.symbol_profiles_path).save(
                plan.runtime_profiles_list()
            )

    # ------------------------------------------------------------------ symbols

    def symbols_load(self, _params: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._config_lock:
                profiles = self._profiles_store.load()
                portfolio = self._portfolio_store.load()
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao carregar símbolos", _safe_text(error)) from error
        return {
            "profiles": [normalize_symbol_profile(profile) for profile in profiles],
            "portfolio": normalize_portfolio_risk(portfolio),
        }

    def symbols_rows(self, params: dict[str, Any]) -> dict[str, Any]:
        settings, stored_profiles = self._load_settings_and_profiles("Falha ao carregar símbolos")
        profiles = params.get("profiles")
        if not isinstance(profiles, list):
            profiles = stored_profiles
        snapshot = load_governor_state(self.root / "runtime" / "governor_state.json")
        rows = build_symbol_rows(
            [item for item in profiles if isinstance(item, Mapping)],
            snapshot,
            settings,
            model_validator=self._validate_model,
        )
        return {
            "rows": rows,
            "state": {
                "available": snapshot.available,
                "updated_at": snapshot.updated_at,
                "error": snapshot.error,
            },
        }

    def symbols_new(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar símbolos")
        return {"profile": new_symbol_profile(settings)}

    def symbols_duplicate(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"profile": duplicate_symbol_profile(_mapping(params.get("profile")))}

    def symbols_check(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = normalize_symbol_profile(_mapping(params.get("profile")))
        existing = [item for item in params.get("profiles", []) or [] if isinstance(item, Mapping)]
        exclude = params.get("exclude_index")
        errors = validate_symbol_profile(
            profile,
            existing,
            exclude_index=int(exclude) if isinstance(exclude, int) else None,
            model_validator=self._validate_model,
        )
        return {"profile": profile, "errors": list(errors)}

    def symbols_save(self, params: dict[str, Any]) -> dict[str, Any]:
        profiles = [
            normalize_symbol_profile(item)
            for item in params.get("profiles", []) or []
            if isinstance(item, Mapping)
        ]
        portfolio = normalize_portfolio_risk(_mapping(params.get("portfolio")))
        if not profiles:
            raise BridgeError("Perfis inválidos", "Mantenha ao menos um perfil de símbolo.")
        portfolio_errors = validate_portfolio_risk(portfolio)
        if portfolio_errors:
            raise BridgeError("Risco de portfólio inválido", _bullets(portfolio_errors))
        profile_errors: list[str] = []
        for index, profile in enumerate(profiles):
            errors = validate_symbol_profile(
                profile,
                profiles,
                exclude_index=index,
                model_validator=self._validate_model,
            )
            profile_errors.extend(f"{profile.get('symbol') or index}: {error}" for error in errors)
        if profile_errors:
            raise BridgeError("Perfis de símbolos inválidos", _bullets(profile_errors))
        try:
            with self._config_lock:
                self._profiles_store.save(deepcopy(profiles))
                self._portfolio_store.save(deepcopy(portfolio))
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao salvar símbolos", _safe_text(error)) from error
        return self.symbols_load({})

    def symbols_files(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao localizar arquivos")
        symbol = str(params.get("symbol", "")).lower().strip()
        environment = normalize_environment(settings).lower()
        runtime = self.root / "runtime"
        data = self.root / "data"
        state = runtime / f"state_{symbol}_{environment}.json"
        journals = (
            data / f"trades_{symbol}_{environment}.jsonl",
            data / f"paper_trades_{symbol}_{environment}.jsonl",
            data / "trades.jsonl",
        )
        return {
            "state": str(state if state.exists() else runtime / "state.json"),
            "journal": str(next((path for path in journals if path.exists()), journals[-1])),
            "governor": str(runtime / "governor_state.json"),
        }

    # ------------------------------------------------------------------ strategy

    def strategy_load(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar estratégias")
        descriptors = self._discover()
        mode = str(settings.get("strategy_selection_mode", "FIXED")).upper()
        return {
            "descriptors": [_descriptor(item) for item in descriptors],
            "current": _qualified_strategy(settings),
            "selection_mode": mode if mode in STRATEGY_MODES else "FIXED",
            "params_text": format_strategy_params(settings.get("strategy_params", {})),
        }

    def strategy_discover(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"descriptors": [_descriptor(item) for item in self._discover(force=True)]}

    def strategy_save(self, params: dict[str, Any]) -> dict[str, Any]:
        qualified = str(params.get("qualified_name", "")).strip()
        mode = str(params.get("selection_mode", "FIXED")).upper().strip()
        params_text = str(params.get("params_text", ""))
        apply_to_profiles = bool(params.get("apply_to_profiles", False))
        descriptor = self._descriptor_by_name(qualified)
        validation = validate_strategy_configuration(descriptor, mode, params_text)
        if validation:
            raise BridgeError("Estratégia inválida", validation[0].message)
        assert descriptor is not None  # validated above
        try:
            with self._config_lock:
                result = prepare_strategy_save(
                    current_settings=self._settings_store.load(),
                    profiles=self._profiles_store.load(),
                    descriptor=descriptor,
                    selection_mode=mode,
                    params=params_text,
                    apply_to_profiles=apply_to_profiles,
                )
                self._settings_store.save(result.settings)
                if apply_to_profiles:
                    self._profiles_store.save(list(result.profiles))
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao salvar estratégia", _safe_text(error)) from error
        message = (
            f"Estratégia salva e aplicada a {result.applied_to_profiles} perfis."
            if result.applied_to_profiles
            else "Estratégia global salva."
        )
        return {
            "message": message,
            "current": qualified,
            "params_text": format_strategy_params(result.settings.get("strategy_params", {})),
        }

    def _discover(self, *, force: bool = False) -> tuple[StrategyDescriptor, ...]:
        """Strategy descriptors, imported once and cached until a forced refresh.

        Discovery imports every strategy module, so concurrent page loads share one run.
        """
        with self._discovery_lock:
            if self._descriptors and not force:
                return self._descriptors
            try:
                descriptors = discover_strategy_descriptors()
            except Exception as error:  # noqa: BLE001 - discovery imports user strategy modules
                raise BridgeError("Falha ao descobrir estratégias", _safe_text(error)) from error
            self._descriptors = descriptors
            return descriptors

    def _descriptor_by_name(self, qualified: str) -> StrategyDescriptor | None:
        def find(items: Sequence[StrategyDescriptor]) -> StrategyDescriptor | None:
            return next((item for item in items if item.qualified_name == qualified), None)

        # Strategy modules may change on disk between page load and save.
        return find(self._discover()) or find(self._discover(force=True))

    # ------------------------------------------------------------------ intelligence

    def intelligence_load(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar inteligência")
        values = _intelligence_values(settings)
        execution_mode = str(settings.get("execution_mode", "DRY_RUN")).upper()
        levels = allowed_activation_levels(execution_mode)
        if values["intelligent_activation_level"] not in levels:
            values["intelligent_activation_level"] = levels[0]
        if not values["ai_operational_timeframe"]:
            values["ai_operational_timeframe"] = "15m"
        if not values["operational_module_modes_customized"]:
            values.update(_effective_module_values(settings))
        return {
            "execution_mode": execution_mode,
            "values": values,
            "models": self._compatible_models(values["ai_operational_timeframe"], execution_mode),
        }

    def intelligence_models(self, params: dict[str, Any]) -> dict[str, Any]:
        timeframe = str(params.get("timeframe", "15m") or "15m")
        execution_mode = str(params.get("execution_mode", "DRY_RUN")).upper()
        return {"models": self._compatible_models(timeframe, execution_mode)}

    def intelligence_validate_model(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            metadata = validate_global_ai_model(
                str(params.get("path", "")),
                require_approved=bool(params.get("require_approved", True)),
                validator=self._validate_model,
            )
        except (OSError, TypeError, ValueError) as error:
            return {"ok": False, "status": _safe_text(error)}
        trained_at = str(metadata.get("trained_at", "-") or "-")
        return {"ok": True, "status": f"Modelo global válido · treino {trained_at}"}

    def intelligence_save(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar inteligência")
        patch = _intelligence_values({**settings, **_mapping(params.get("values"))})
        candidate = {**settings, **patch}
        validation = validate_intelligence_settings(candidate)
        if validation:
            raise BridgeError("Configuração inválida", validation[0].message)
        if patch["ai_probabilistic_enabled"]:
            self._check_selected_model(candidate)
            try:
                validate_global_ai_model(
                    patch["ai_global_model_path"],
                    require_approved=patch["ai_require_model_approved"],
                    validator=self._validate_model,
                )
            except (OSError, TypeError, ValueError) as error:
                raise BridgeError("Modelo de IA inválido", _safe_text(error)) from error
        try:
            with self._config_lock:
                prepared = normalize_intelligence_settings(self._settings_store.load(), patch)
                self._settings_store.save(prepared)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao salvar inteligência", _safe_text(error)) from error
        return self.intelligence_load({})

    def intelligence_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        settings, profiles = self._load_settings_and_profiles("Falha ao carregar inteligência")
        values = params.get("values")
        if isinstance(values, Mapping):
            settings.update(_intelligence_values({**settings, **values}))
        diagnostics = build_intelligence_diagnostics(settings, profiles)
        return {
            "layers": diagnostics.layers,
            "operation_review": diagnostics.operation_review,
            "process_review": diagnostics.process_review,
            "cache_health": diagnostics.cache_health,
        }

    def _compatible_models(self, timeframe: str, execution_mode: str) -> list[dict[str, Any]]:
        try:
            from research.ai.registry import list_compatible_ai_models  # noqa: PLC0415

            models = list_compatible_ai_models(
                interval=timeframe,
                execution_mode=execution_mode,
                scope="GLOBAL",
            )
        except Exception:  # noqa: BLE001 - an unreadable registry means "no models"
            return []
        return [
            _model_profile(entry, timeframe=timeframe)
            for entry in models
            if isinstance(entry, Mapping)
        ]

    def _check_selected_model(self, settings: Mapping[str, Any]) -> None:
        selected = str(settings.get("ai_operational_model_id", "")).strip()
        if not selected:
            return
        models = self._compatible_models(
            str(settings.get("ai_operational_timeframe", "15m")),
            str(settings.get("execution_mode", "DRY_RUN")).upper(),
        )
        profile = next((item for item in models if item["model_id"] == selected), None)
        if profile is None:
            raise BridgeError(
                "Perfil de IA incompatível",
                "O perfil selecionado não é compatível com o timeframe ou modo de execução.",
            )
        if not profile["input_contract_valid"]:
            raise BridgeError("Perfil de IA incompatível", profile["input_contract_status"])

    # ------------------------------------------------------------------ credentials

    def credentials_load(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar credenciais")
        return {
            "summaries": [
                summarize_credentials(settings, env).as_qml_map() for env in CREDENTIAL_ENVIRONMENTS
            ],
            "storage_path": str(self._settings_store.path),
            "protection": self._storage_protection(),
        }

    def credentials_save(self, params: dict[str, Any]) -> dict[str, Any]:
        api_key = str(params.get("api_key", "") or "")
        api_secret = str(params.get("api_secret", "") or "")
        if not api_key and not api_secret:
            raise BridgeError("Credenciais", "Nenhuma credencial nova foi informada.")
        return self._update_credentials(
            str(params.get("environment", "")),
            api_key=api_key or None,
            api_secret=api_secret or None,
        )

    def credentials_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._update_credentials(str(params.get("environment", "")), clear=True)

    def _update_credentials(self, environment: str, **changes: Any) -> dict[str, Any]:
        try:
            with self._config_lock:
                result = prepare_credentials_update(
                    self._settings_store.load(),
                    environment=environment,
                    **changes,
                )
                if result.changed:
                    self._settings_store.save(result.settings)
                    secure_credentials_file(self._settings_store.path)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Credenciais inválidas", _safe_text(error)) from error
        payload = self.credentials_load({})
        payload["changed"] = result.changed
        return payload

    def _storage_protection(self) -> str:
        target = self._settings_store.path
        if os.name != "posix":
            return "Protegido pelo perfil do usuário do sistema operacional"
        try:
            if not target.exists():
                return "Será aplicada ao salvar"
            if target.stat().st_mode & 0o077:
                return "Permissões locais precisam ser restringidas"
        except OSError:
            return "Não foi possível verificar as permissões locais"
        return "Permissões locais restritas ao usuário (0600)"

    # ------------------------------------------------------------------ backtest

    def backtest_load(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar backtest")
        strategies = [item.qualified_name for item in self._discover() if item.valid]
        selected = str(settings.get("backtest_strategy", "")).strip() or _qualified_strategy(
            settings
        )
        if selected not in strategies and strategies:
            selected = strategies[0]
        return {
            "values": _backtest_values(normalize_backtest_settings(settings, {})),
            "strategies": strategies,
            "selected_strategy": selected,
        }

    def backtest_save(self, params: dict[str, Any]) -> dict[str, Any]:
        self._persist_backtest(params)
        return {}

    def backtest_download(self, params: dict[str, Any]) -> dict[str, Any]:
        effective = self._effective_backtest(params)
        try:
            plan = prepare_download_plan(effective, project_root=self.root)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Download inválido", _safe_text(error)) from error
        self._persist_backtest(params)
        return {"command": list(plan.command), "env": {}}

    def backtest_download_done(self, params: dict[str, Any]) -> dict[str, Any]:
        values = _backtest_values(self._effective_backtest(params))
        path = parse_downloaded_path(_lines(params.get("output")), project_root=self.root)
        if path is None:
            return {"path": None, "values": values}
        downloaded = _interval_in_name(path.name)
        primary = str(values.get("backtest_csv_path", ""))
        if not primary or _interval_in_name(primary) == downloaded:
            values["backtest_csv_path"] = str(path)
        elif values.get("use_secondary_csv"):
            values["secondary_csv_path"] = str(path)
        self._persist_backtest({**params, "values": values})
        return {"path": str(path), "values": values}

    def backtest_rules(self, params: dict[str, Any]) -> dict[str, Any]:
        effective = self._effective_backtest(params)
        try:
            rules = fetch_symbol_rules(effective)
        except Exception as error:  # noqa: BLE001 - network and payload failures alike
            raise BridgeError("Falha ao buscar regras", _safe_text(error)) from error
        return {
            "values": {
                target: str(rules[source])
                for source, target in _RULE_KEYS.items()
                if source in rules
            }
        }

    def backtest_run(self, params: dict[str, Any]) -> dict[str, Any]:
        effective = self._effective_backtest(params)
        strategy = str(params.get("strategy", "")).strip()
        try:
            with self._config_lock:
                profiles = self._profiles_store.load()
                portfolio = self._portfolio_store.load()
            plan = prepare_backtest_plan(
                effective,
                profiles,
                portfolio,
                selected_strategy=strategy,
                project_root=self.root,
            )
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Backtest inválido", _safe_text(error)) from error
        self._persist_backtest(params)
        return {
            "command": list(plan.command),
            "env": dict(plan.environment),
            "symbols": list(plan.symbols),
        }

    def backtest_result(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            result = load_backtest_result(_lines(params.get("output")), project_root=self.root)
        except Exception as error:  # noqa: BLE001 - missing/corrupt report files
            raise BridgeError("Backtest sem relatório válido", _safe_text(error)) from error
        artifacts = artifact_map(result.artifacts)
        return {
            "summary": result.summary,
            "text": result.summary_text,
            "artifacts": artifacts,
            "equity": _load_equity_curve(artifacts.get("equityCsv", "")),
        }

    def _effective_backtest(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar backtest")
        patch = _backtest_values(_mapping(params.get("values")), partial=True)
        strategy = str(params.get("strategy", "")).strip()
        if strategy:
            patch["backtest_strategy"] = strategy
        return normalize_backtest_settings(settings, patch)

    def _persist_backtest(self, params: dict[str, Any]) -> None:
        effective = self._effective_backtest(params)
        errors = validate_backtest_settings(effective)
        if errors:
            raise BridgeError("Configuração inválida", errors[0])
        try:
            with self._config_lock:
                latest = self._settings_store.load()
                latest.update(effective)
                self._settings_store.save(latest)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao salvar backtest", _safe_text(error)) from error

    # ------------------------------------------------------------------ AI training

    def training_load(self, _params: dict[str, Any]) -> dict[str, Any]:
        settings = self._load_settings("Falha ao carregar treinamento")
        return {
            "values": normalize_ai_training_settings(settings),
            "model_summary": summarize_selected_model(settings, project_root=self.root),
        }

    def training_save(self, params: dict[str, Any]) -> dict[str, Any]:
        self._persist_training(_mapping(params.get("values")))
        return self.training_load({})

    def training_start(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind", "")).upper().strip()
        if kind not in TRAINING_KINDS:
            raise BridgeError("Treinamento inválido", f"Tipo de treinamento inválido: {kind}")
        settings, profiles = self._load_settings_and_profiles("Falha ao carregar treinamento")
        effective = {
            **settings,
            **normalize_ai_training_settings(settings, _mapping(params.get("values"))),
        }
        errors = validate_ai_training_settings(
            effective, profiles, kind=kind, project_root=self.root
        )
        if errors:
            raise BridgeError("Configuração inválida", errors[0])
        try:
            plan = prepare_ai_training_plan(effective, profiles, kind=kind, project_root=self.root)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Treinamento inválido", _safe_text(error)) from error
        self._persist_training(_mapping(params.get("values")))
        return {"command": list(plan.command), "symbols": list(plan.symbols), "kind": plan.kind}

    def training_done(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            result = parse_ai_training_result(_lines(params.get("output")), project_root=self.root)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Treinamento sem artefatos válidos", _safe_text(error)) from error
        try:
            with self._config_lock:
                settings = self._settings_store.load()
                profiles = self._profiles_store.load()
                updated_settings, updated_profiles = apply_ai_training_result(
                    settings, profiles, result
                )
                self._settings_store.save(updated_settings)
                profiles_changed = updated_profiles != profiles
                if profiles_changed:
                    self._profiles_store.save(updated_profiles)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao registrar modelos", _safe_text(error)) from error
        generated = result.symbol_models()
        if result.global_model_path:
            generated = {"GLOBAL": result.global_model_path, **generated}
        return {
            **self.training_load({}),
            "summary": result.summary,
            "generated": generated,
            "profiles_changed": profiles_changed,
        }

    def _persist_training(self, values: Mapping[str, Any]) -> None:
        try:
            with self._config_lock:
                latest = self._settings_store.load()
                latest.update(normalize_ai_training_settings(latest, values))
                self._settings_store.save(latest)
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError("Falha ao salvar treinamento", _safe_text(error)) from error

    # ------------------------------------------------------------------ observability

    def observability_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Monitor data. ``params["sections"]`` limits the reply to the listed sections.

        The dashboard only shows recent events, so it asks for ``["events"]`` and skips
        the journal, scanner and state tails (hundreds of KB per refresh).
        """
        settings, profiles = self._load_settings_and_profiles("Falha na observabilidade")
        requested = params.get("sections")
        sections = (
            SNAPSHOT_SECTIONS.intersection(str(item) for item in requested)
            if isinstance(requested, list)
            else SNAPSHOT_SECTIONS
        )
        snapshot = build_observability_snapshot(
            settings=settings,
            symbol_profiles=profiles,
            root_path=self.root,
            event_limit=_bounded_int(params.get("event_limit"), 200, 1, 2000),
            journal_tail_lines=_bounded_int(params.get("journal_tail_lines"), 400, 10, 5000),
            scanner_tail_lines=_bounded_int(params.get("scanner_tail_lines"), 400, 10, 5000),
            state_max_bytes=_bounded_int(params.get("state_max_bytes"), 1_000_000, 1024, 4_000_000),
            sections=sections,
        )
        reply: dict[str, Any] = {"updated_at": snapshot.updated_at}
        if "events" in sections:
            reply["events"] = list(snapshot.events)
        if "journals" in sections:
            reply["journal_text"] = snapshot.journal_text
            reply["journal_paths"] = list(snapshot.journal_paths)
        if "scanner" in sections:
            reply["scanner_text"] = snapshot.scanner_text
        if "state" in sections:
            reply["state_text"] = snapshot.state_text
            reply["state_paths"] = list(snapshot.state_paths)
        if "sources" in sections:
            reply["sources"] = [source.as_qml_map() for source in snapshot.sources]
            reply["existing_sources"] = snapshot.existing_sources
        return reply

    def observability_rotate(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind", "journals")).strip().lower()
        if kind not in {"journals", "intelligent"}:
            raise BridgeError("Rotação inválida", "Tipo de rotação inválido.")
        settings, profiles = self._load_settings_and_profiles("Falha ao preparar rotação")
        try:
            summary = rotate_observability_journals(
                settings=settings,
                symbol_profiles=profiles,
                root_path=self.root,
                kind=kind,
            )
        except (OSError, ValueError) as error:
            raise BridgeError("Falha na rotação", _safe_text(error)) from error
        return {
            "message": summary.message,
            "moved_count": summary.moved_count,
            "archive_directory": summary.archive_directory,
        }

    def observability_locate(self, params: dict[str, Any]) -> dict[str, Any]:
        paths = params.get("candidates")
        if isinstance(paths, list):
            try:
                found = first_existing_source(root_path=self.root, paths=[str(p) for p in paths])
            except ValueError as error:
                raise BridgeError("Caminho inválido", _safe_text(error)) from error
            return {"path": str(found) if found else None}
        try:
            resolved = resolve_project_path(self.root, str(params.get("path", "")))
        except ValueError as error:
            raise BridgeError("Caminho inválido", _safe_text(error)) from error
        return {"path": str(resolved) if resolved.exists() else None}

    # ------------------------------------------------------------------ audit

    def audit_task(self, params: dict[str, Any]) -> dict[str, Any]:
        phase = str(params.get("phase", "")).lower()
        if phase == "started":
            observe_event(
                "task_started",
                component="desktop.bridge",
                category="desktop",
                channel=EventChannel.AUDIT,
                task=str(params.get("kind", "")),
                command=[str(item) for item in params.get("command", []) or []],
                cwd=str(self.root),
                pid=params.get("pid"),
            )
        elif phase == "finished":
            status = str(params.get("status", "FINISHED")).upper()
            exit_code = params.get("exit_code")
            severity = "INFO" if status == "FINISHED" and exit_code == 0 else "WARNING"
            if status == "ERROR":
                severity = "ERROR"
            observe_event(
                "task_finished",
                component="desktop.bridge",
                category="desktop",
                channel=EventChannel.AUDIT,
                severity=severity,
                task=str(params.get("kind", "")),
                exit_code=exit_code,
                status=status,
            )
        return {}

    def audit_output(self, params: dict[str, Any]) -> dict[str, Any]:
        for line in _lines(params.get("lines"))[:2000]:
            append_run_log("task_stdout.log", line)
        return {}

    # ------------------------------------------------------------------ helpers

    def _load_settings(self, title: str) -> dict[str, Any]:
        try:
            with self._config_lock:
                return self._settings_store.load()
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError(title, _safe_text(error)) from error

    def _load_settings_and_profiles(
        self, title: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            with self._config_lock:
                return self._settings_store.load(), self._profiles_store.load()
        except (OSError, TypeError, ValueError) as error:
            raise BridgeError(title, _safe_text(error)) from error

    def _validate_model(self, path: str | Path) -> Mapping[str, Any]:
        """Model validation cached by (path, mtime, size): rows refresh every few seconds."""
        from research.ai.registry import validate_ai_model_file  # noqa: PLC0415

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            stat = candidate.stat()
        except OSError:
            return validate_ai_model_file(path)
        key = (str(candidate), stat.st_mtime_ns, stat.st_size)
        with self._model_cache_lock:
            cached = self._model_cache.get(key)
        if cached is not None:
            return deepcopy(cached)
        metadata = dict(validate_ai_model_file(candidate))
        with self._model_cache_lock:
            if len(self._model_cache) >= _MODEL_CACHE_LIMIT:
                self._model_cache.clear()
            self._model_cache[key] = metadata
        return deepcopy(metadata)


# ---------------------------------------------------------------------- serializers


def _message(item: OperationMessage) -> dict[str, Any]:
    return {
        "code": item.code,
        "title": item.title,
        "message": item.message,
        "severity": item.severity.value,
    }


def _confirmation(request: ConfirmationRequest) -> dict[str, Any]:
    return {
        "key": request.key,
        "kind": request.kind.value,
        "title": request.title,
        "message": request.message,
        "severity": request.severity.value,
        "required_tokens": list(request.required_tokens),
        "checklist_items": list(request.checklist_items),
        "accept_label": request.accept_label,
        "reject_label": request.reject_label,
    }


def _response(payload: Any) -> ConfirmationResponse:
    data = _mapping(payload)
    return ConfirmationResponse(
        key=str(data.get("key", "")),
        accepted=bool(data.get("accepted", False)),
        typed_value=str(data.get("typed_value", "") or ""),
        checked_items=tuple(str(item) for item in data.get("checked_items", []) or []),
    )


def _blocked(title: str, message: str) -> dict[str, Any]:
    return {"stage": "blocked", "title": title, "message": message, "messages": []}


def _descriptor(item: StrategyDescriptor) -> dict[str, Any]:
    return {
        "qualified_name": item.qualified_name,
        "module_name": item.module_name,
        "class_name": item.class_name,
        "valid": item.valid,
        "contract": item.contract_label,
        "details": item.details,
    }


def _intelligence_values(settings: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        key: _as_bool(settings.get(key, default))
        for key, default in _INTELLIGENCE_BOOL_DEFAULTS.items()
    }
    for key in _INTELLIGENCE_TEXT_KEYS:
        text = str(settings.get(key, "") or "").strip()
        if key.endswith("_mode") or key == "intelligent_activation_level":
            text = text.upper()
        values[key] = text
    return values


def _effective_module_values(settings: Mapping[str, Any]) -> dict[str, str]:
    try:
        modes = operational_module_modes_for_settings(dict(settings))
    except Exception:  # noqa: BLE001 - fall back to stored values
        return {}
    return {MODULE_MODE_ATTRS[module]: value for module, value in modes.items()}


def _model_profile(entry: Mapping[str, Any], *, timeframe: str) -> dict[str, Any]:
    path = str(entry.get("path", "") or "")
    payload = _load_model_payload(path)
    contract = payload.get("input_contract")
    if not isinstance(contract, Mapping):
        contract = entry.get("input_contract")
    valid = (
        isinstance(contract, Mapping)
        and str(contract.get("timeframe", "")).strip() == str(timeframe).strip()
    )
    required_history = (
        contract.get("required_history")
        if isinstance(contract, Mapping)
        else payload.get("required_history", entry.get("required_history", ""))
    )
    model_id = str(entry.get("model_id", "") or payload.get("model_id", ""))
    return {
        "label": f"IA probabilística · {timeframe} · {model_id or Path(path).stem}",
        "model_id": model_id,
        "path": path,
        "timeframe": str(entry.get("interval", timeframe) or timeframe),
        "required_history": str(required_history or "-"),
        "input_contract_valid": bool(valid),
        "input_contract_status": (
            "input_contract válido" if valid else "input_contract ausente ou incompatível"
        ),
    }


def _load_model_payload(path: str) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    try:
        from research.ai.model import load_ai_model  # noqa: PLC0415

        payload = load_ai_model(Path(path))
    except Exception:  # noqa: BLE001 - unreadable model files simply have no contract
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _backtest_values(settings: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _BACKTEST_TEXT_KEYS:
        if partial and key not in settings:
            continue
        values[key] = str(settings.get(key, "") or "").strip()
    for key in _BACKTEST_BOOL_KEYS:
        if partial and key not in settings:
            continue
        values[key] = _as_bool(settings.get(key, key == "funding_enabled"))
    return values


def _interval_in_name(name: str) -> str | None:
    for timeframe in SUPPORTED_TIMEFRAMES:
        if f"_{timeframe}_" in name or f"_{timeframe}." in name:
            return timeframe
    return None


def _load_equity_curve(path: str) -> list[float]:
    """Read the equity column and keep the curve shape with min/max bucketing."""
    if not path or not Path(path).is_file():
        return []
    values: list[float] = []
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    values.append(float(row.get("equity", "")))
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    if len(values) <= _EQUITY_POINTS:
        return values
    buckets = _EQUITY_POINTS // 2
    size = len(values) / buckets
    reduced: list[float] = []
    for index in range(buckets):
        start = int(index * size)
        chunk = values[start : max(int((index + 1) * size), start + 1)]
        if not chunk:
            continue
        low = min(range(len(chunk)), key=chunk.__getitem__)
        high = max(range(len(chunk)), key=chunk.__getitem__)
        first, second = sorted((low, high))
        reduced.append(chunk[first])
        if second != first:
            reduced.append(chunk[second])
    reduced[0] = values[0]
    reduced[-1] = values[-1]
    return reduced


def _qualified_strategy(settings: Mapping[str, Any]) -> str:
    module_name = str(settings.get("strategy_module", "")).strip()
    class_name = str(settings.get("strategy_class", "")).strip()
    return f"{module_name}.{class_name}" if module_name and class_name else ""


def _choice(value: Any, allowed: Sequence[str], label: str) -> str:
    normalized = str(value or "").upper().strip()
    if normalized not in allowed:
        raise BridgeError(f"{label} inválido", f"{label} não suportado: {value!r}.")
    return normalized


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}


def _safe_text(value: Any) -> str:
    try:
        text = str(sanitize_for_audit(str(value))).strip()
    except Exception:  # noqa: BLE001
        return "[SANITIZATION_ERROR]"
    return text or value.__class__.__name__
