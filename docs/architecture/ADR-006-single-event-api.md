# ADR-006 — API única de eventos com *sinks*

- Estado: aceito
- Data: 16/11/2026
- Contexto: até a M7 havia dois sistemas de eventos (OPT-032). `observe_event`/`observe_error`
  (`observability`) enfileiravam eventos para `runtime/runs/<run>/diagnostics/*.jsonl`; `audit_event`/`audit_error`
  (`utils/audit_logger.py`) gravavam de forma síncrona em `runtime/runs/<run>/events/<categoria>_events.jsonl` e
  alimentavam o `run_summary.json`. `engine.py` chamava os dois em sequência com o mesmo conteúdo, e cada
  `TradeJournal.append` era espelhado no audit logger por uma função interna dele (`mirror_journal_event`). Código de
  domínio (`futures`, `strategies`) também emite eventos, então a API não pode morar numa camada acima dele.
- Alternativas consideradas:
  1. mover o audit logger para `observability` e fazer todos importarem de lá: o domínio passaria a importar a
     infraestrutura, contra a regra de camadas;
  2. manter as duas APIs e só documentar quando usar cada uma;
  3. uma API na camada base, com destinos (*sinks*) registrados pela infraestrutura, como o `logging` da biblioteca
     padrão.
- Decisão: alternativa 3. `utils/events.py` (camada 0, só biblioteca padrão) define `EventChannel`, `EventSeverity`,
  `EventRecord`, o protocolo `EventSink` e a API `observe_event`/`observe_error`. Cada registro vai a todos os *sinks*
  que o aceitam; uma falha de *sink* nunca chega a quem emitiu.
  - `utils.audit_logger.AUDIT_RUN_SINK`, instalado no primeiro uso, aceita o canal `AUDIT` e grava o mesmo arquivo e o
    mesmo esquema de antes, com os mesmos contadores do resumo do run. O registro ganha o campo de topo `component`
    (quem emitiu); nomes de eventos, categorias e chaves de payload não mudam.
  - `observability.application.service.SERVICE_EVENT_SINK`, registrado ao importar o serviço, aceita todos os outros
    canais e **todo erro**, e os entrega ao serviço singleton (fila, privacidade, diagnósticos de erro, fsync), como
    `observe_event` fazia antes.
  - O que antes era `audit_event(categoria, tipo, …)` passa a ser
    `observe_event(tipo, component=…, category=categoria, channel=EventChannel.AUDIT, …)`; `audit_error` passa a ser
    `observe_error(…, channel=EventChannel.AUDIT)`, que registra o erro na trilha de auditoria **e** nos
    diagnósticos. Os pares que emitiam o mesmo evento nos dois sistemas viram uma chamada.
  - `observability` reexporta `observe_event`, `observe_error`, `EventChannel` e `EventSeverity`; os enums são os
    mesmos objetos nos dois pacotes.
  - O `TradeJournal` continua dono do seu arquivo em `data/` e publica cada linha na API (`observe_record`, canal
    `AUDIT`, componente `utils.trade_journal`), com a categoria e a severidade que o audit logger inferia antes; o
    audit logger não conhece mais journals (`mirror_journal_event` saiu).
  - O canal `TELEMETRY` não tem *sink* de arquivo: alimenta agregadores em memória. O `StageRunner` emite nele um
    `evaluation_stage` por estágio (G9) e o `StageTelemetry` do Governor resume contagem, erros, terminais e
    durações (p50/p95/máximo) por estágio no próximo `governor_cycle_metrics` (`stage_telemetry`). Um evento por
    estágio gravado em disco cresceria sem limite (22 estágios × símbolos × ciclos de 3 s).
- Consequências positivas: um caminho de emissão para todas as camadas; um teste pode registrar um *sink* de captura em
  vez de trocar funções de módulo; destinos novos (métricas, telemetria por estágio) se registram sem tocar em quem
  emite.
- Consequências negativas: um erro do canal `AUDIT` agora também aparece em `diagnostics/diagnostics.jsonl`; os
  eventos que `engine.py` emitia nos dois sistemas (`config_loaded`, `shutdown_requested`) ficam só na trilha de
  auditoria, sem cópia nos diagnósticos.
- Reversão: reverter os commits da frente de eventos da M7; nenhum formato persistido perdeu campos.
