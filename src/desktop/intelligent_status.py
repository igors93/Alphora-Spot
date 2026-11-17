from typing import Any

from desktop.settings_store import (
    ARCHITECTURE_INTELLIGENT_FLOW,
    INTELLIGENT_LEVEL_SHADOW_ONLY,
    apply_architecture_preset,
    normalize_environment,
)

LAYER_DEFINITIONS = (
    (
        "trade_planning_enabled",
        "Trade planning",
        "Monta TradePlan e diagnósticos de plano.",
    ),
    (
        "cost_model_enabled",
        "Cost model",
        "Calcula custo, spread, fee, funding e slippage estimados.",
    ),
    (
        "dynamic_sizing_enabled",
        "Dynamic sizing",
        "Permite quantidade inteligente em paper/testnet controlado.",
    ),
    (
        "dynamic_exit_enabled",
        "Dynamic exit",
        "Permite stop/take inteligente e adaptativo nos modos seguros.",
    ),
    (
        "execution_planner_enabled",
        "Execution planner",
        "Avalia mercado antes da execução e pode aguardar estabilidade.",
    ),
    (
        "adaptive_manager_enabled",
        "Adaptive manager",
        "Avalia posição aberta para breakeven, proteção e trailing.",
    ),
    (
        "adaptive_execution_enabled",
        "Adaptive execution",
        "Aplica ajustes adaptativos seguros somente em paper.",
    ),
    (
        "learning_store_enabled",
        "Learning store",
        "Registra aprendizado e resultados para auditoria.",
    ),
    (
        "learning_paper_activation_enabled",
        "Learning paper activation",
        "Permite learning atuar em paper.",
    ),
    (
        "learning_testnet_observation_enabled",
        "Learning testnet observation",
        "Permite learning observar testnet sem agir no real.",
    ),
)


def intelligent_layer_states(settings: dict[str, Any]) -> list[dict[str, Any]]:
    prepared = apply_architecture_preset(settings)

    states = []
    for key, label, description in LAYER_DEFINITIONS:
        enabled = _as_bool(prepared.get(key, False))
        states.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "enabled": enabled,
                "status": "LIGADO" if enabled else "DESLIGADO",
            }
        )

    return states


def format_intelligent_layers_for_display(settings: dict[str, Any]) -> str:
    prepared = apply_architecture_preset(settings)
    states = intelligent_layer_states(prepared)

    lines = [
        f"Arquitetura: {prepared.get('architecture_mode', ARCHITECTURE_INTELLIGENT_FLOW)}",
        f"Nível inteligente: {prepared.get('intelligent_activation_level', INTELLIGENT_LEVEL_SHADOW_ONLY)}",
        f"Execution mode: {prepared.get('execution_mode', 'DRY_RUN')}",
        f"Ambiente efetivo: {normalize_environment(prepared)}",
        "",
        "Camadas efetivas:",
    ]

    for state in states:
        lines.append(f"- [{state['status']}] {state['label']}: {state['description']}")

    lines.extend(
        [
            "",
            "Resumo de segurança:",
            architecture_safety_summary(prepared),
        ]
    )
    return "\n".join(lines)


def architecture_safety_summary(settings: dict[str, Any]) -> str:
    prepared = apply_architecture_preset(settings)
    architecture = str(prepared.get("architecture_mode", ARCHITECTURE_INTELLIGENT_FLOW)).upper()
    level = str(prepared.get("intelligent_activation_level", INTELLIGENT_LEVEL_SHADOW_ONLY)).upper()
    execution_mode = str(prepared.get("execution_mode", "DRY_RUN")).upper()

    if architecture != ARCHITECTURE_INTELLIGENT_FLOW:
        return "Arquitetura inválida: use INTELLIGENT_FLOW."

    if level == "SHADOW_ONLY":
        return "SHADOW_ONLY: calcula e registra, mas não altera execução."

    if level == "PAPER_ACTIVE":
        return (
            "PAPER_ACTIVE: sizing, exit, execution planner e adaptive execution "
            "podem atuar sem enviar ordem real para corretora."
        )

    if level == "TESTNET_OBSERVE":
        return "TESTNET_OBSERVE: observa testnet, sem alterar execução inteligente."

    if level == "TESTNET_REDUCE":
        return "TESTNET_REDUCE: pode aplicar quantidade/redução; stop/take adaptativo permanece desligado."

    if level == "TESTNET_BLOCK":
        return (
            "TESTNET_BLOCK: pode aplicar sizing, stop/take e execution planner em testnet. "
            "Adaptive execution continua desligado."
        )

    if execution_mode == "REAL_LIVE":
        return (
            f"{level}: REAL_LIVE protegido. Camadas automáticas continuam bloqueadas "
            "pelos presets e pelo Governor."
        )

    return f"{level}: modo inteligente observacional/controlado."


def short_layers_label(settings: dict[str, Any]) -> str:
    states = intelligent_layer_states(settings)
    enabled_count = sum(1 for state in states if state["enabled"])
    return f"{enabled_count}/{len(states)} camadas ligadas"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    return normalized in {"true", "1", "yes", "y", "sim"}
