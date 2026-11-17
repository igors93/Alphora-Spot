import argparse
from pathlib import Path

from trading.learning_activation_criteria import (
    LearningActivationCriteriaEvaluator,
    format_learning_activation_criteria,
)
from trading.learning_paper_activation import (
    LearningPaperActivationGate,
    LearningPaperActivationSettings,
    format_learning_paper_activation,
)
from trading.learning_report import (
    export_latest_learning_report,
    format_learning_report,
    load_latest_learning_report,
)
from trading.learning_shadow_confidence import (
    LearningShadowConfidenceEvaluator,
    format_learning_shadow_confidence,
)
from trading.learning_shadow_impact import (
    LearningShadowImpactAnalyzer,
    format_learning_shadow_impact_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exibe ou exporta relatórios de aprendizado do Alphora.",
    )
    parser.add_argument(
        "--events",
        default="data/governor_events.jsonl",
        help="Caminho do journal de eventos do governor.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Caminho opcional para salvar o relatório em texto.",
    )
    parser.add_argument(
        "--impact",
        action="store_true",
        help="Exibe a análise de impacto do learning shadow.",
    )
    parser.add_argument(
        "--execution-mode",
        default="PAPER_LIVE",
        help="Modo usado para simular flags de ativação em paper no relatório.",
    )
    parser.add_argument(
        "--paper-activation-enabled",
        action="store_true",
        help="Simula ativacao paper derivada de ALPHORA_LEARNING_ADVISORY_MODE.",
    )
    parser.add_argument(
        "--paper-reduction-enabled",
        action="store_true",
        help="Simula reducao paper derivada de ALPHORA_LEARNING_ADVISORY_MODE.",
    )
    parser.add_argument(
        "--paper-block-enabled",
        action="store_true",
        help="Simula bloqueio paper derivado de ALPHORA_LEARNING_ADVISORY_MODE.",
    )
    args = parser.parse_args(argv)

    events_path = Path(args.events)

    if args.impact:
        return _handle_impact_report(
            events_path=events_path,
            output_path=args.output,
            paper_settings=LearningPaperActivationSettings(
                execution_mode=args.execution_mode,
                learning_store_enabled=True,
                paper_activation_enabled=args.paper_activation_enabled,
                paper_reduction_enabled=args.paper_reduction_enabled,
                paper_block_enabled=args.paper_block_enabled,
            ),
        )

    return _handle_latest_learning_report(
        events_path=events_path,
        output_path=args.output,
    )


def _handle_latest_learning_report(*, events_path: Path, output_path: str) -> int:
    if output_path:
        exported = export_latest_learning_report(
            source_journal_path=events_path,
            output_path=output_path,
        )
        if exported is None:
            print("Nenhum relatório de aprendizado encontrado.")
            return 0

        print(exported)
        print(f"Relatório salvo em: {output_path}")
        return 0

    latest = load_latest_learning_report(events_path)
    if latest is None:
        print("Nenhum relatório de aprendizado encontrado.")
        return 0

    print(format_learning_report(latest))
    return 0


def _handle_impact_report(
    *,
    events_path: Path,
    output_path: str,
    paper_settings: LearningPaperActivationSettings,
) -> int:
    report = _build_impact_report_text(
        events_path=events_path,
        paper_settings=paper_settings,
    )

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(report)
        print(f"Relatório de impacto salvo em: {output_path}")
        return 0

    print(report)
    return 0


def _build_impact_report_text(
    *,
    events_path: Path,
    paper_settings: LearningPaperActivationSettings,
) -> str:
    summary = LearningShadowImpactAnalyzer().analyze_journal(events_path)
    confidence = LearningShadowConfidenceEvaluator().evaluate(summary)
    activation = LearningActivationCriteriaEvaluator().evaluate(
        summary=summary,
        confidence=confidence,
    )
    paper_activation = LearningPaperActivationGate(
        settings=paper_settings,
    ).evaluate(activation)

    return (
        format_learning_shadow_impact_summary(summary)
        + "\n"
        + format_learning_shadow_confidence(confidence)
        + "\n"
        + format_learning_activation_criteria(activation)
        + "\n"
        + format_learning_paper_activation(paper_activation)
    )


if __name__ == "__main__":
    raise SystemExit(main())
