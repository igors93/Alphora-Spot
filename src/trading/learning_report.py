from pathlib import Path
from typing import Any

from trading.learning import (
    OUTCOME_EVENT_TYPE,
    TradePlanLearningAnalyzer,
    TradePlanLearningSummary,
)
from trading.learning_recommendations import (
    LearningRecommendationReport,
    TradePlanLearningAdvisor,
)
from utils.jsonl import parse_json_object_line
from utils.trade_journal import TradeJournal

LEARNING_REPORT_EVENT_TYPE = "learning_recommendation_report_created"


class TradePlanLearningReporter:
    def __init__(
        self,
        *,
        source_journal_path: str | Path,
        output_journal: TradeJournal,
        analyzer: TradePlanLearningAnalyzer | None = None,
        advisor: TradePlanLearningAdvisor | None = None,
    ):
        self.source_journal_path = Path(source_journal_path)
        self.output_journal = output_journal
        self.analyzer = analyzer or TradePlanLearningAnalyzer()
        self.advisor = advisor or TradePlanLearningAdvisor()
        self._outcome_cursor_offset = 0
        self._known_total_outcomes = 0

    def build_summary(self) -> TradePlanLearningSummary:
        return self.analyzer.summarize_journal(self.source_journal_path)

    def build_report(self) -> LearningRecommendationReport:
        return self.advisor.build_report(self.build_summary())

    def record_report(self, report: LearningRecommendationReport) -> None:
        self.output_journal.append(
            LEARNING_REPORT_EVENT_TYPE,
            status=report.status,
            total_outcomes=report.total_outcomes,
            can_use_for_execution=report.can_use_for_execution,
            recommendations_count=len(report.recommendations),
            recommendations=report.recommendations,
            report=report,
            diagnostics=report.diagnostics,
        )

    def record_if_changed(
        self,
        *,
        previous_total_outcomes: int,
    ) -> LearningRecommendationReport | None:
        current_total = self._advance_outcome_cursor(previous_total_outcomes)
        if current_total <= 0:
            return None

        if current_total == previous_total_outcomes:
            return None

        report = self.build_report()

        if report.total_outcomes <= 0:
            return None

        if report.total_outcomes == previous_total_outcomes:
            return None

        self.record_report(report)
        return report

    def _advance_outcome_cursor(self, previous_total_outcomes: int) -> int:
        if self._outcome_cursor_offset == 0 and self._known_total_outcomes == 0:
            total, next_offset = count_trade_plan_outcomes_since(
                self.source_journal_path,
                offset=0,
            )
            self._outcome_cursor_offset = next_offset
            self._known_total_outcomes = total
            return total

        new_outcomes = self._count_new_outcomes()
        if new_outcomes <= 0:
            self._known_total_outcomes = max(
                self._known_total_outcomes,
                previous_total_outcomes,
            )
        return self._known_total_outcomes

    def _count_new_outcomes(self) -> int:
        new_outcomes, next_offset = count_trade_plan_outcomes_since(
            self.source_journal_path,
            offset=self._outcome_cursor_offset,
        )
        self._outcome_cursor_offset = next_offset
        self._known_total_outcomes += new_outcomes
        return new_outcomes


def count_trade_plan_outcomes(path: str | Path) -> int:
    journal_path = Path(path)
    if not journal_path.exists():
        return 0

    total = 0
    with journal_path.open("r", encoding="utf-8") as file:
        for line in file:
            event = parse_json_object_line(line.strip())
            if event is None or event.get("event_type") != OUTCOME_EVENT_TYPE:
                continue
            if _has_outcome_payload(event):
                total += 1
    return total


def count_trade_plan_outcomes_since(path: str | Path, *, offset: int) -> tuple[int, int]:
    journal_path = Path(path)
    if not journal_path.exists():
        return (0, 0)

    file_size = journal_path.stat().st_size
    start_offset = offset if 0 <= offset <= file_size else 0
    total = 0
    with journal_path.open("r", encoding="utf-8") as file:
        file.seek(start_offset)
        while True:
            line = file.readline()
            if not line:
                break
            event = parse_json_object_line(line.strip())
            if event is None or event.get("event_type") != OUTCOME_EVENT_TYPE:
                continue
            if _has_outcome_payload(event):
                total += 1
        return (total, file.tell())


def load_learning_reports_from_journal(path: str | Path) -> list[dict[str, Any]]:
    journal_path = Path(path)
    if not journal_path.exists():
        return []

    reports: list[dict[str, Any]] = []
    with journal_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue

            event = parse_json_object_line(line)
            if event is None:
                continue

            if event.get("event_type") != LEARNING_REPORT_EVENT_TYPE:
                continue

            report = _extract_report_payload(event)
            if report is not None:
                reports.append(report)

    return reports


def load_latest_learning_report(path: str | Path) -> dict[str, Any] | None:
    reports = load_learning_reports_from_journal(path)
    if not reports:
        return None

    return reports[-1]


def export_latest_learning_report(
    *,
    source_journal_path: str | Path,
    output_path: str | Path,
) -> str | None:
    latest = load_latest_learning_report(source_journal_path)
    if latest is None:
        return None

    formatted = format_learning_report(latest)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(formatted, encoding="utf-8")
    return formatted


def format_learning_report(report_payload: dict[str, Any]) -> str:
    recommendations = _extract_recommendations(report_payload)
    lines = [
        "Alphora Learning Report",
        "=======================",
        f"Timestamp: {report_payload.get('timestamp', '')}",
        f"Status: {report_payload.get('status', '')}",
        f"Total outcomes: {report_payload.get('total_outcomes', 0)}",
        f"Pode usar para execução: {report_payload.get('can_use_for_execution', False)}",
        f"Quantidade de recomendações: {len(recommendations)}",
        "",
        "Recomendações:",
    ]

    if not recommendations:
        lines.append("- Nenhuma recomendação encontrada.")
        return "\n".join(lines) + "\n"

    for recommendation in recommendations:
        lines.extend(_format_recommendation(recommendation))

    return "\n".join(lines) + "\n"


def _extract_report_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None

    payload = dict(data)
    payload["timestamp"] = event.get("timestamp", "")
    return payload


def _has_outcome_payload(event: dict[str, Any]) -> bool:
    data = event.get("data")
    if not isinstance(data, dict):
        return False

    if isinstance(data.get("outcome"), dict):
        return True

    return data.get("trade_plan_id") is not None or data.get("outcome_status") is not None


def _extract_recommendations(report_payload: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations = report_payload.get("recommendations")
    if isinstance(recommendations, list):
        return [item for item in recommendations if isinstance(item, dict)]

    report = report_payload.get("report")
    if isinstance(report, dict):
        nested_recommendations = report.get("recommendations")
        if isinstance(nested_recommendations, list):
            return [item for item in nested_recommendations if isinstance(item, dict)]

    return []


def _format_recommendation(recommendation: dict[str, Any]) -> list[str]:
    action = recommendation.get("action", "")
    severity = recommendation.get("severity", "")
    scope = recommendation.get("scope", "")
    symbol = recommendation.get("symbol")
    confidence = recommendation.get("confidence", "")
    reason = recommendation.get("reason", "")
    metric_name = recommendation.get("metric_name", "")
    metric_value = recommendation.get("metric_value")
    threshold = recommendation.get("threshold")

    header = f"- [{severity}] {action}"
    if scope:
        header += f" | escopo={scope}"
    if symbol:
        header += f" | símbolo={symbol}"
    if confidence != "":
        header += f" | confiança={confidence}"

    lines = [header]

    if reason:
        lines.append(f"  motivo: {reason}")

    if metric_name:
        lines.append(
            "  métrica: "
            f"{metric_name}={metric_value}"
            f"{f' | limite={threshold}' if threshold is not None else ''}"
        )

    return lines
