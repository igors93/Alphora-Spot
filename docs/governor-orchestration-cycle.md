# Governor — ciclo de orquestração

O `AlphoraGovernor` (`src/governor/alphora_governor.py`) é a fachada do loop operacional: ele recebe o grafo de
componentes pronto da raiz de composição, controla `start`/`stop` e delega cada ciclo ao `CycleCoordinator`. A
decisão por símbolo é uma sequência de 22 estágios; o envio de ordens é do `ExecutionCoordinator`. Detalhes de cada
peça estão no [mapa do Governor](architecture/governor.md).

---

## Quem faz o quê

| Responsabilidade | Onde |
|---|---|
| Construir o grafo (cliente, engines, journals, auditoria, serviços) | `governor/composition.py` (`build_governor_components`) |
| Loop, pedidos de parada e pausa, encerramento | `AlphoraGovernor.start` |
| Fases do ciclo | `governor/cycle/coordinator.py` (`CycleCoordinator`) |
| Prefetch dos snapshots pesados | `governor/cycle/prefetch.py` |
| Avaliação de um símbolo (executor de estágios) | `governor/evaluation/` (`SymbolEvaluator`, `StageRunner`) |
| Estágios 1–22 | `governor/stages/` |
| Serviços dos estágios (gate de IA, modo defensivo, orçamento, oportunidade…) | `governor/services/` |
| Envio de ordens, ciclo de vida, ledger | `execution/` (`ExecutionCoordinator`, `OrderLifecycle`, `PositionLedger`) |
| Snapshots de mercado e de conta | `market_data/snapshot_provider/` |
| Eventos | `utils/events.py` (ADR-006) e journals em `data/` |

---

## Boot

1. `engine.main` carrega o `AppConfig` (`config.load_config`), inicializa o contexto do run, a observabilidade e a
   auditoria do run, e registra `config_loaded`.
2. `AlphoraGovernor.from_config` monta os componentes pela raiz de composição.
3. `start()` adquire os locks dos símbolos, inicia o WebSocket de mercado e o de user data (se habilitado), carrega o
   `exchangeInfo` pelo provider de snapshots e espera o aquecimento do hub de preços.
4. Para cada símbolo: recupera o estado salvo (`recover_on_boot`) e configura margem e alavancagem quando o modo
   permite mutações.

## Loop

```text
enquanto _running:
    pedido de parada (runtime/governor_stop.request)? → encerra
    pedidos por símbolo (pausar, retomar, parar)
    run_cycle()
    pedido de parada? → encerra
    espera loop_interval_seconds, ou acorda antes por um choque de preço do WebSocket
```

O encerramento grava o estado final, para os streams e o provider, libera os locks e registra `governor_stopped`
(journal) e `run_finished` (auditoria).

---

## Um ciclo (`CycleCoordinator.run`)

| Passo | O que acontece |
|---|---|
| Abertura | Cria o `CycleContext` com o orçamento de checagens pesadas e registra `cycle_started` |
| Sessão | Snapshot base do provider e atualização do hub de candles |
| Posições | Audita as posições abertas |
| Seleção | Drena símbolos com preço novo e choques do WebSocket (fast lane), mede a saúde do mercado, avalia o modo defensivo e ranqueia os símbolos devidos (`PriorityScheduler`); posições abertas e fast lane entram sempre |
| Fase A | Estágios 1–8 para **todos** os selecionados, antes de qualquer leitura privada |
| Fase B | Prefetch paralelo dos snapshots pesados dos que seguiram |
| Fase C | Estágios 9–22, **um símbolo por vez**: uma ordem invalida os snapshots privados dos seguintes |
| Fechamento | Grava `governor_state.json`, recomendações de learning, `governor_cycle_metrics` (com `rest_weight`), `cycle_finished` e a saúde da sessão |

### Os 22 estágios

O `StageRunner` executa a tupla em ordem, mede cada estágio (`stage_timings`) e para no primeiro resultado
terminal (sem ação, bloqueio ou erro).

| # | Estágio | Nome na telemetria |
|---:|---|---|
| 1 | Contexto leve: snapshot, tendência, posição, score da estratégia | `light_snapshot_score` |
| 2 | Controle manual e status de proteção | `handle_manual_or_protection_status` |
| 3 | Warmup da estratégia | `handle_warmup` |
| 4 | Warmup do símbolo, antes do pesado | `run_symbol_warmup_pre_heavy` |
| 5 | Quarentena do símbolo, antes do pesado | `run_symbol_quarantine_pre_heavy` |
| 6 | Guards antes do pesado: modo defensivo e gate de IA | `run_pre_heavy_guards` |
| 7 | Só avaliação leve? | `handle_light_only` |
| 8 | Orçamento de checagens pesadas (e pressão de peso REST) | `run_request_budget_guard` |
| 9 | Contexto pesado: snapshot privado | `heavy_snapshot` |
| 10 | Decisão do símbolo: estratégia → intenção | `intent_creation_hot_guard` |
| 11 | Reconciliação de estado | `run_state_reconciliation_guard` |
| 12 | Warmup do símbolo, depois do pesado | `run_symbol_warmup_post_heavy` |
| 13 | Quarentena do símbolo, depois do pesado | `run_symbol_quarantine_post_heavy` |
| 14 | Guards depois do pesado: gate de IA e modo defensivo | `run_post_heavy_guards` |
| 15 | Plano e gate econômico | `planning_economic_gate_initial` |
| 16 | Camadas inteligentes | `apply_intelligent_layers` |
| 17 | Smart sizing | `run_smart_sizing` |
| 18 | Economia final e risco de portfólio | `economic_portfolio_pre_execution_inputs` |
| 19 | Guards de learning | `run_learning_execution_guards` |
| 20 | Smart execution | `run_smart_execution` |
| 21 | Planner de execução | `run_execution_planner` |
| 22 | Revalidação, checagem de preço e despacho pelo `ExecutionCoordinator` | `pre_execution_execution` |

Os estágios 17 e 20 são os mesmos objetos no backtest (`build_decision_kernel_stages`, migração M6).

---

## Estados

Há dois vocabulários, e nenhum deles é `WARMUP/ACTIVE/PAUSED` como uma máquina única:

- **Estado operacional do símbolo** (`SymbolOperationalState`, `governor/symbol_state_machine.py`): o progresso da
  avaliação e da posição, de `FLAT` e `LIGHT_EVALUATED` até `ENTRY_SENT`, `PROTECTED`, `CLOSE_SENT` e
  `FLAT_CONFIRMED`, além de `MANUAL_RECONCILE_REQUIRED`, `BLOCKED` e `ERROR`. Cada transição é registrada pelos
  estágios.
- **Status exibido** (`SymbolRuntimeStatus`, `governor/models.py`): o que o painel mostra, como `WARMUP`, `COLD`,
  `HOT_CHECK`, `IN_POSITION`, `POSITION_OPEN_PROTECTED`, `PAUSED` e `STOPPED`. Pausar e parar vêm de
  `ops/pause_symbol.py`, `ops/resume_symbol.py` e `ops/stop_symbol.py` (ou da interface), lidos a cada ciclo.

## Modo defensivo

`DefensiveModeService` avalia o estresse do mercado em todo ciclo: choques de preço, símbolos sem preço recente e
pressão do orçamento de requisições. Por padrão ele só observa (`ALPHORA_GOVERNOR_DEFENSIVE_MODE_SHADOW_ONLY=true`,
`…_ENFORCEMENT_ENABLED=false`). Quando aplicado, bloqueia **novas entradas** nos guards dos estágios 6 e 14;
posições abertas continuam sendo geridas.

## Eventos e logs

- **API única de eventos** (`utils/events.py`, [ADR-006](architecture/ADR-006-single-event-api.md)):
  `observe_event`/`observe_error`. O canal `AUDIT` vai para `runtime/runs/<run_id>/events/<categoria>_events.jsonl`
  e para o resumo do run; os outros canais e todos os erros vão para `runtime/runs/<run_id>/diagnostics/`.
- **Journals** em `data/` (`TradeJournal`): `governor_events.jsonl`, `trades_<símbolo>_<ambiente>.jsonl`,
  `trade_audit.jsonl`, `scanner_scores.jsonl`. Cada linha também é publicada no canal `AUDIT`.
- **Logs de texto**: `runtime/runs/<run_id>/logs/alphora.log`, pelo `logging` da biblioteca padrão com o filtro de
  privacidade (`utils/logprivacy_adapter.py`).

Os eventos carregam `run_id` e, dentro de um ciclo, `cycle_id` e `symbol_eval_id`.

---

## Como rodar

| Como | Comando |
|---|---|
| Interface desktop | `cargo run --manifest-path interface/Cargo.toml` |
| Linha de comando | `python main.py` (ou `alphora-engine`) |
| Estado do Governor | `python -m ops.governor_status` |
