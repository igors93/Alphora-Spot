# Decisão, risco e execução

[← Governor](governor.md) · [Índice](README.md) · [Dados de mercado e resiliência →](dados-e-resiliencia.md)

---

## 1. Da estratégia à ordem: a cadeia real

Ordem dos estágios pesados com os objetos que passam de um para o outro. Cada losango pode encerrar a avaliação.

```mermaid
flowchart TD
    A["StrategyCoordinator.score<br/>→ SignalScore"] --> B["StrategyCoordinator.intent<br/>→ TradeIntent / IntentCandidate"]
    B --> C["HotSymbolWorker.evaluate<br/>validação profunda do símbolo"]
    C --> D{"Reconciliação de estado<br/>StateReconciliationEngine"}
    D -->|ok| E{"Guards pós-pesados<br/>modo defensivo + gate de IA"}
    E -->|ok| F["Plano: quantidade + saída planejada<br/>→ PlannedTrade"]
    F --> G{"EconomicTradeGate<br/>ganho líquido, R:R, custo"}
    G -->|aprovado| H["Camadas inteligentes<br/>ordem e saída inteligentes"]
    H --> I["SmartSizingEngine<br/>→ SmartSizingDecision"]
    I --> J{"Guards finais<br/>gate econômico final +<br/>PortfolioRiskEngine.evaluate_trade_intent"}
    J -->|ok| K{"Guards de learning<br/>paper / testnet"}
    K -->|ok| L["SmartExecutionEngine<br/>→ SmartExecutionDecision"]
    L --> M["Planner de execução inteligente"]
    M --> N["Revalidação do snapshot para execução<br/>+ checagem de preço pré-envio"]
    N --> O{"PreExecutionGuard.evaluate"}
    O -->|ok| P["ValidatedTrade → ExecutableTrade"]
    P --> Q["ExecutionEngine.execute<br/>→ ExecutionCommand"]
    Q --> R["FuturesTradingEngine do símbolo<br/>open_position / close_position"]
    R --> S["BinanceFuturesClient<br/>order / algoOrder"]
    S --> T["ExecutionResult → journals,<br/>auditoria, estado"]
    D -->|bloqueia| Z(["resultado terminal"])
    E -->|bloqueia| Z
    G -->|rejeita| Z
    J -->|bloqueia| Z
    K -->|bloqueia| Z
    O -->|bloqueia| Z
```

**Diferença em relação ao Plano Diretor (versão 1):** o risco de portfólio é avaliado **depois** do plano e do sizing
(estágio 18), não antes. O gate econômico roda **duas vezes** (estágios 15 e 18).

---

## 2. Camadas de risco: onde cada limite mora

```mermaid
flowchart LR
    subgraph CONTA["Conta e dia"]
        DM["DefensiveModeEvaluator<br/>governor/defensive_mode.py"]
        DPM["DailyPortfolioMetricsReader"]
    end
    subgraph PORT["Portfólio"]
        PRE["PortfolioRiskEngine<br/>governor/portfolio_risk_engine.py"]
    end
    subgraph OPER["Operação"]
        EG["EconomicTradeGate<br/>futures/planning/economic_gate.py"]
        SZ["SmartSizingEngine"]
        RM["FuturesRiskManager<br/>quantidade, filtros"]
        AIG["Gate de probabilidade da IA<br/>AIGateMixin"]
    end
    subgraph EXECR["Execução"]
        PEG["PreExecutionGuard<br/>348 linhas"]
        FRG["FuturesRiskGuard<br/>limites diários por símbolo"]
        PROT["Proteções stop/take<br/>futures/protection.py"]
        SR["StateReconciliationEngine"]
    end
    CONTA --> PORT --> OPER --> EXECR
```

Os limites existem e estão separados por camada, mas **sem um contrato comum**. Cada um devolve um tipo diferente
(`DefensiveModeEvaluation`, `PortfolioRiskDecision`, `EconomicGateDecision`, `PreExecutionGuardDecision`,
`RiskGuardDecision`…), e o pipeline traduz cada resultado para `_mark_blocked` / `_make_terminal_result`.

---

## 3. Abrindo uma posição até a Binance

```mermaid
sequenceDiagram
    autonumber
    participant G as Estágio 22<br/>_execute_approved_intent
    participant X as ExecutionEngine
    participant F as FuturesTradingEngine<br/>do símbolo
    participant ST as RuntimeStateStore<br/>state_SYMBOL.json
    participant B as BinanceFuturesClient
    participant BX as Binance

    G->>X: execute(intent)
    X->>F: open_position
    F->>F: risk_guard.can_open_position
    F->>F: select_entry_quantity + margem disponível
    F->>F: gera trade_id e client_order_id
    F->>ST: salva estado da posição
    F->>B: _submit_order
    B->>BX: POST fapi/v1/order
    alt resposta incerta
        F->>B: consulta pelo client_order_id
        F->>F: _reconcile_entry_order_unknown
    end
    F->>F: _wait_for_position_after_entry
    F->>B: cria proteções stop e take
    B->>BX: POST fapi/v1/algoOrder
    F->>ST: salva estado protegido
    F-->>X: PositionExecutionResult
    X-->>G: ExecutionResult
    G->>G: invalida snapshots privados
```

<details>
<summary>▶ O que já existe e o Plano Diretor v1 tratava como novo</summary>

| Capacidade | Onde já existe |
|---|---|
| ID de cliente por ordem | `_new_client_order_id` em `futures/engine_position_mixin.py` |
| Estado salvo em torno do envio | `_save_state_for_position` |
| Reconciliação de resultado incerto | `_reconcile_entry_order_unknown`, `_reconcile_close_order_unknown` |
| Proteções revalidadas | `_refresh_position_for_protection_operation`, `_assert_protection_triggers_not_breached` |
| Pausa do engine em falha | `_pause_engine` |

O que falta não é a capacidade em si, e sim **um modelo único e explícito** do ciclo de vida da ordem: hoje essa lógica
está espalhada por funções de 250 a 480 linhas.

</details>

---

## 4. Modos de execução e permissão de mutação

```mermaid
flowchart LR
    MODE["execution_mode"] --> DRY["DRY_RUN<br/>lê a conta, não envia"]
    MODE --> PAPER["PAPER_LIVE<br/>fills simulados"]
    MODE --> TEST["TESTNET_LIVE<br/>ordens na testnet"]
    MODE --> REAL["REAL_LIVE<br/>ordens reais"]
    DRY --> MUT0["mutations_enabled = false"]
    PAPER --> MUT0
    TEST --> MUT1["mutations_enabled = true"]
    REAL --> MUT1
    MUT0 --> CL0["cliente recusa mutações"]
    MUT1 --> CL1["cliente envia ordens"]
    PAPER --> SB["SimulatedBroker<br/>research/simulated_broker.py"]
    REAL --> G1["CLI bloqueada sem ALPHORA_LAUNCHED_BY_GUI<br/>ou ALPHORA_ALLOW_CLI_RUN"]
    TEST --> G1
```

A barreira é aplicada em **mais de um lugar**: na configuração, no cliente Binance e no ponto de entrada.
O broker de paper trading fica em `futures/simulated_broker.py`; a antiga reexportação em `research/` foi removida na
M1.

---

## 5. Componentes compartilhados

A raiz de composição da M2 define um dono e injeta as instâncias compartilhadas:

```mermaid
flowchart LR
    C["GovernorComponents"] --> J["TradeJournal<br/>governor_events.jsonl"]
    C --> A["TradeAuditRecorder"]
    C --> R["StateReconciliationEngine"]
    C --> X["ExecutionEngine"]
    X --> E["FuturesTradingEngine × N"]
    J --> X
    A --> X
    A --> E
    R --> X
    R --> E
    X --> G["FuturesRiskGuard × símbolo"]
    G --> E
    X --> P["paper_journal × símbolo<br/>caminho final"]
    P --> E
```

| Componente | Cardinalidade e dono |
|---|---|
| `data/governor_events.jsonl` | 1 `TradeJournal`, criado pela raiz e injetado no `ExecutionEngine` |
| `TradeAuditRecorder` | 1 por Governor, compartilhado com o executor e todas as engines por símbolo |
| `StateReconciliationEngine` | 1 por Governor, usado pelo estágio de avaliação e por todas as engines |
| `FuturesRiskGuard` | 1 por símbolo, armazenado no cache do `ExecutionEngine` |
| `paper_journal` | 1 por engine de símbolo, criado diretamente com o caminho final |

---

## 6. Onde está a “verdade” sobre uma posição

```mermaid
flowchart LR
    BX["Binance<br/>positionRisk, openOrders,<br/>openAlgoOrders"]
    UDS["User Data Stream<br/>eventos de conta e ordem"]
    UDC["UserDataShadowCache<br/>+ audit, usage, stale guard,<br/>reconciliation"]
    RS["RuntimeStateStore<br/>runtime/state_SYMBOL_ENV.json"]
    FE["FuturesTradingEngine<br/>was_position_open, active_trade_id"]
    SST["SymbolStateTracker<br/>23 estados, observacional"]
    GS["governor_state.json<br/>para UI e ops"]
    BX --> FE
    UDS --> UDC
    UDC -.->|"shadow / assistido"| FE
    FE <--> RS
    FE --> GS
    FE -.-> SST
    SST --> GS
```

Esse diagrama registra a arquitetura medida antes da M4. A implementação atual centraliza a posição local e as
intenções de ordem no [`PositionLedger`](../../src/execution/ledger.py). `RuntimeStateStore` persiste o mesmo estado
local no formato existente; `FuturesTradingEngine` o lê pelo ledger. A Binance é a autoridade externa e alimenta a
reconciliação. `SymbolStateTracker` descreve apenas a trajetória de avaliação, enquanto `governor_state.json` é
uma projeção para UI e operações. Os detalhes de recuperação e falhas estão em
[M4: ciclo de vida de ordens](../m4-execution-lifecycle.md).

[← Governor](governor.md) · [Índice](README.md) · [Dados de mercado e resiliência →](dados-e-resiliencia.md)
