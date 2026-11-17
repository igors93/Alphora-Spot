# Governor por dentro

[← Pacotes e dependências](pacotes-e-dependencias.md) · [Índice](README.md) · [Decisão, risco e execução →](decisao-e-execucao.md)

> O `AlphoraGovernor` (`src/governor/alphora_governor.py`) é o coração do engine: um objeto que herda **7 mixins**,
> cria cerca de **85 atributos** no construtor e roda o loop de decisão. Esta página mostra como ele é montado,
> como roda e onde está o acoplamento.

> **Histórico:** esta página descreve o Governor **antes da M3**. Desde a M3 ele não herda mixins: estágios em
> `governor/stages/`, serviços em `governor/services/`, ciclo em `governor/cycle/` e avaliador em
> `governor/evaluation/`. O mapa do que saiu de cada mixin está no
> [Plano de Migração, M3.1](../MIGRATION_PLAN.md#m31-implementação-realizada).

---

## 1. Composição

```mermaid
classDiagram
    direction LR
    class AlphoraGovernor {
        +from_config(config, client)
        +start()
        +run_cycle()
        -85 atributos
    }
    class UserDataCacheMixin { 31 métodos }
    class DefensiveModeMixin { 11 métodos }
    class CycleMixin { 44 métodos }
    class OpportunityMixin { 19 métodos }
    class SymbolEvalMixin { 50 métodos }
    class TradePipelineMixin { 62 métodos }
    class AIGateMixin { 6 métodos }
    UserDataCacheMixin <|-- AlphoraGovernor
    DefensiveModeMixin <|-- AlphoraGovernor
    CycleMixin <|-- AlphoraGovernor
    OpportunityMixin <|-- AlphoraGovernor
    SymbolEvalMixin <|-- AlphoraGovernor
    TradePipelineMixin <|-- AlphoraGovernor
    AIGateMixin <|-- AlphoraGovernor
```

Componentes que o construtor cria e guarda em `self` (agrupados por função):

```mermaid
flowchart LR
    GOV(["AlphoraGovernor"])
    subgraph DADOS["Dados"]
        MDH["MarketDataHub<br/>WS bookTicker"]
        CDH["CandleDataHub"]
        UDS["UserDataStreamHub"]
        UDC["UserDataShadowCache"]
        MDR["MarketDataRouter<br/>+ provider de snapshots"]
        SCH["PriorityScheduler"]
    end
    subgraph DECISAO["Decisão"]
        STC["StrategyCoordinator"]
        HSW["HotSymbolWorker"]
        ORK["OpportunityRanker"]
        LRN["LearningAdvisory<br/>LearningPlanGuard"]
    end
    subgraph RISCO["Risco e gates"]
        EG["EconomicTradeGate"]
        TPV["TradePlanExecutionValidator"]
        PRE["PortfolioRiskEngine"]
        PEG["PreExecutionGuard"]
        SSZ["SmartSizingEngine"]
        SEX["SmartExecutionEngine"]
        SRE["StateReconciliationEngine"]
    end
    subgraph EXEC["Execução"]
        EXE["ExecutionEngine<br/>um FuturesTradingEngine por símbolo"]
    end
    subgraph ESTADO["Estado e telemetria"]
        SW["GovernorStateWriter"]
        EVJ["events_journal"]
        SCJ["score_journal"]
        TAR["TradeAuditRecorder"]
        SST["SymbolStateTracker"]
        CTL["GovernorControlRequests"]
        DPM["DailyPortfolioMetricsReader<br/>PortfolioSnapshotProvider"]
    end
    GOV --- DADOS
    GOV --- DECISAO
    GOV --- RISCO
    GOV --- EXEC
    GOV --- ESTADO
```

`governor/composition.py` é a raiz de composição. Ela devolve um `GovernorComponents` congelado, com os atributos
agrupados em dados, decisão, risco, execução e estado. O construtor do Governor apenas valida a entrada, instala esse
grafo e mantém a referência em `governor.components`. Os componentes compartilhados e seus donos estão descritos em
[Decisão, risco e execução](decisao-e-execucao.md#5-componentes-compartilhados).

---

## 2. Boot

```mermaid
sequenceDiagram
    autonumber
    participant M as engine.main
    participant C as load_config
    participant O as Observabilidade e auditoria
    participant B as BinanceFuturesClient
    participant G as AlphoraGovernor
    participant X as ExecutionEngine
    participant H as Hubs de dados

    M->>C: carrega .env e ambiente
    C-->>M: AppConfig
    M->>O: initialize_run_context, observability, run_audit
    M->>M: _enforce_live_entrypoint
    M->>B: cria cliente com mutações conforme o modo
    M->>G: from_config(config, client)
    M->>G: start()
    G->>X: acquire_symbol_locks
    G->>H: inicia WS de mercado, thread própria
    G->>H: inicia WS de usuário, se habilitado
    G->>H: bootstrap de exchangeInfo pelo provider de snapshots
    G->>H: warmup do hub
    loop para cada símbolo
        G->>X: recover, se recover_on_boot
        G->>X: configura margem e alavancagem, se mutations_enabled
    end
    G->>G: entra no loop principal
```

---

## 3. Loop principal

```mermaid
flowchart TD
    S["start()"] --> L{"_running?"}
    L -->|sim| C1{"pedido de parada?<br/>runtime/governor_stop.request"}
    C1 -->|sim| SD
    C1 -->|não| RC["run_cycle()"]
    RC --> C2{"pedido de parada?"}
    C2 -->|sim| SD
    C2 -->|não| W["_wait_until_next_cycle<br/>espera o intervalo OU o evento<br/>de wakeup do WS de mercado"]
    W --> L
    L -->|não| SD["encerramento: estado final,<br/>para os streams e o provider,<br/>libera locks, journal governor_stopped"]
```

O ciclo é **disparado por tempo** (`loop_interval_seconds`, padrão 3 s) **ou por evento**: um choque de preço no
WebSocket acorda o loop antes do prazo (`_market_wakeup_event`).

---

## 4. Anatomia de um ciclo (`run_cycle`)

`governor/cycle_mixin.py:723`, uma função de **266 linhas**.

```mermaid
flowchart TD
    A["Abre sessão de snapshot do ciclo<br/>request budget do ciclo"] --> B["Pedidos de pausa/retomada/parada por símbolo"]
    B --> C["Snapshot base do provider"]
    C --> D["Audita posições abertas"]
    D --> E["Drena símbolos 'sujos' e choques do WS<br/>fast lane"]
    E --> F["Saúde do mercado + modo defensivo (shadow)"]
    F --> G["Ranqueia símbolos devidos<br/>PriorityScheduler + fast lane"]
    G --> H["Snapshot dos símbolos selecionados"]
    H --> I["FASE A · avaliação leve de TODOS os selecionados<br/>8 estágios leves"]
    I --> J["FASE B · prefetch pesado em paralelo<br/>ThreadPoolExecutor ou provider particionado"]
    J --> K["FASE C · avaliação pesada, um símbolo por vez<br/>14 estágios pesados, pode executar ordens"]
    K --> M["Grava governor_state.json"]
    M --> N["Learning: recomendações e relatório de impacto"]
    N --> P["Métricas do ciclo, alerta de ciclo lento,<br/>saúde do provider, fecha a sessão"]
```

**Ponto-chave para otimizar:** as leituras da Fase B são paralelas, mas a Fase C é sequencial de propósito, porque
uma ordem invalida os snapshots privados dos símbolos seguintes. Toda otimização do ciclo precisa respeitar isso.

---

## 5. Os 22 estágios de avaliação por símbolo

O executor de estágios já existe (`_run_symbol_evaluation_stages`, `symbol_eval_mixin.py:207`): ele roda as funções em
ordem, mede o tempo de cada uma (`stage_timings`) e para no primeiro resultado terminal. **Este pipeline é a costura
natural para a migração** (ver [Plano de Migração](../MIGRATION_PLAN.md)).

```mermaid
flowchart LR
    subgraph LEVE["Avaliação leve · todos os símbolos selecionados"]
        L1["1 contexto leve"] --> L2["2 status manual/proteção"] --> L3["3 warmup"] --> L4["4 warmup pré-pesado"]
        L4 --> L5["5 quarentena pré-pesada"] --> L6["6 guards pré-pesados<br/>defensivo + IA"] --> L7["7 só leve?"] --> L8["8 budget de requisições"]
    end
    subgraph PESADA["Avaliação pesada · um por vez"]
        H1["9 contexto pesado"] --> H2["10 decisão hot symbol<br/>estratégia → intenção"] --> H3["11 reconciliação de estado"]
        H3 --> H4["12 warmup pós"] --> H5["13 quarentena pós"] --> H6["14 guards pós-pesados"]
        H6 --> H7["15 plano + gate econômico"] --> H8["16 camadas inteligentes"] --> H9["17 smart sizing"]
        H9 --> H10["18 guards finais<br/>economia + portfólio"] --> H11["19 guards de learning"] --> H12["20 smart execution"]
        H12 --> H13["21 planner de execução"] --> H14["22 executa a intenção"]
    end
    L8 --> H1
    T(["resultado terminal:<br/>sem ação, bloqueado ou erro"])
    L3 -.-> T
    L7 -.-> T
    H6 -.-> T
    H10 -.-> T
    H14 --> X(["ordem enviada"])
```

<details>
<summary>▶ Onde está cada estágio e quanto ele mede</summary>

| # | Estágio | Arquivo:linha | Linhas |
|---:|---|---|---:|
| 1 | `_build_light_evaluation_context` | `symbol_eval_mixin.py:507` | 169 |
| 2 | `_handle_manual_or_protection_status` | `symbol_eval_mixin.py:677` | 35 |
| 3 | `_handle_warmup` | `symbol_eval_mixin.py:713` | 12 |
| 4 | `_run_symbol_warmup_pre_heavy` | `symbol_eval_mixin.py:726` | 14 |
| 5 | `_run_symbol_quarantine_pre_heavy` | `symbol_eval_mixin.py:838` | 17 |
| 6 | `_run_pre_heavy_guards` | `symbol_eval_mixin.py:1155` | 55 |
| 7 | `_handle_light_only` | `symbol_eval_mixin.py:1211` | 36 |
| 8 | `_run_request_budget_guard` | `opportunity_mixin.py:372` | 22 |
| 9 | `_build_heavy_evaluation_context` | `symbol_eval_mixin.py:1248` | 201 |
| 10 | `_run_hot_symbol_decision` | `symbol_eval_mixin.py:1450` | 120 |
| 11 | `_run_state_reconciliation_guard` | `symbol_eval_mixin.py:1671` | 68 |
| 12 | `_run_symbol_warmup_post_heavy` | `symbol_eval_mixin.py:741` | 13 |
| 13 | `_run_symbol_quarantine_post_heavy` | `symbol_eval_mixin.py:856` | 19 |
| 14 | `_run_post_heavy_guards` | `symbol_eval_mixin.py:1591` | 79 |
| 15 | `_build_and_validate_trade_plan` | `trade_pipeline_mixin.py:254` | 132 |
| 16 | `_apply_intelligent_layers` | `trade_pipeline_mixin.py:387` | 134 |
| 17 | `_run_smart_sizing` | `trade_pipeline_mixin.py:522` | 105 |
| 18 | `_run_final_execution_guards` | `trade_pipeline_mixin.py:841` | 184 |
| 19 | `_run_learning_execution_guards` | `trade_pipeline_mixin.py:1026` | 289 |
| 20 | `_run_smart_execution` | `trade_pipeline_mixin.py:669` | 93 |
| 21 | `_run_execution_planner` | `trade_pipeline_mixin.py:1316` | 78 |
| 22 | `_execute_approved_intent` | `trade_pipeline_mixin.py:1983` | **449** |

O estágio 22 ainda chama `_revalidate_evaluation_for_execution` (419 linhas) e o `PreExecutionGuard.evaluate` (348).

</details>

---

## 6. O contexto compartilhado entre os estágios

Os estágios não trocam dados por parâmetros: todos leem e escrevem no mesmo `SymbolEvaluationContext`
(`governor/symbol_evaluation_pipeline.py:36`). Ele funciona como um **quadro-negro** com 58 campos,
**35 deles tipados como `Any`**.

```mermaid
flowchart LR
    subgraph CTX["SymbolEvaluationContext · 58 campos, 35 Any"]
        F1["light · heavy · router_heavy<br/>position · runtime_state"]
        F2["score · trend_context · scheduled<br/>trade_decision · hot_result"]
        F3["economic_gate_decision · planned_quantity<br/>planned_exit · final_economic_validation"]
        F4["smart_sizing_decision · smart_execution_decision<br/>portfolio_decision · state_reconciliation_decision"]
        F5["intelligent_order_plan · intelligent_exit_plan<br/>intelligent_execution_plan · learning_advisory"]
    end
    S1["estágios 1 e 9"] -->|escrevem| F1
    S2["estágio 10"] -->|escreve| F2
    S3["estágio 15"] -->|escreve| F3
    S4["estágios 17, 18 e 20"] -->|escrevem| F4
    S5["estágios 16, 19 e 21"] -->|escrevem| F5
    F1 -->|lido por quase todos| R["estágios seguintes"]
    F2 --> R
    F3 --> R
    F4 --> R
    F5 --> R
```

Consequência: não dá para testar um estágio sozinho sem montar o contexto inteiro e o Governor que o cerca.

---

## 7. Rastreador de estados

`SymbolStateTracker` (`governor/symbol_state_machine.py`) registra **23 estados** por símbolo. A docstring diz que ele
é **apenas observacional**: a posição real continua sendo de `RuntimeState`, `ExecutionEngine`, Binance/User Data
Stream e `FuturesTradingEngine`. Transições inválidas são registradas, não impedidas.

```mermaid
stateDiagram-v2
    [*] --> FLAT
    FLAT --> LIGHT_EVALUATED
    LIGHT_EVALUATED --> HEAVY_REQUIRED
    HEAVY_REQUIRED --> HEAVY_EVALUATED
    HEAVY_EVALUATED --> SIGNAL_CANDIDATE
    SIGNAL_CANDIDATE --> TRADE_PLANNED
    TRADE_PLANNED --> ECONOMIC_APPROVED
    ECONOMIC_APPROVED --> INTELLIGENT_LAYERS_APPLIED
    INTELLIGENT_LAYERS_APPLIED --> FINAL_ECONOMIC_APPROVED
    FINAL_ECONOMIC_APPROVED --> PORTFOLIO_APPROVED
    PORTFOLIO_APPROVED --> LEARNING_APPLIED
    LEARNING_APPLIED --> EXECUTION_WAITING
    EXECUTION_WAITING --> EXECUTION_APPROVED
    EXECUTION_APPROVED --> ENTRY_SENT
    ENTRY_SENT --> POSITION_OPEN
    POSITION_OPEN --> PROTECTIONS_PENDING
    PROTECTIONS_PENDING --> PROTECTED
    PROTECTED --> CLOSE_REQUESTED
    CLOSE_REQUESTED --> CLOSE_SENT
    CLOSE_SENT --> FLAT_CONFIRMED
    FLAT_CONFIRMED --> FLAT
    LIGHT_EVALUATED --> BLOCKED
    HEAVY_EVALUATED --> BLOCKED
    TRADE_PLANNED --> BLOCKED
    PORTFOLIO_APPROVED --> BLOCKED
    ENTRY_SENT --> MANUAL_RECONCILE_REQUIRED
    POSITION_OPEN --> ERROR
    BLOCKED --> LIGHT_EVALUATED
```

O diagrama mostra o caminho principal e as saídas típicas para `BLOCKED`, `ERROR` e `MANUAL_RECONCILE_REQUIRED`;
qualquer estado pode ir para esses três. Na M7, `docs/governor-orchestration-cycle.md` foi reescrito a partir deste
código e separa este rastreador do status exibido no painel (`SymbolRuntimeStatus`).

---

## 8. Threads e concorrência

```mermaid
flowchart TB
    subgraph PROC["Processo do engine"]
        MT["Thread principal<br/>loop + run_cycle + estágios + ordens"]
        PF["Pool de prefetch pesado<br/>alphora-heavy-prefetch, por ciclo"]
        WM["Thread WS de mercado<br/>MarketDataHub, ping 20 s"]
        WU["Thread WS de usuário<br/>UserDataStreamHub"]
        KA["Thread de keepalive<br/>listenKey"]
        AL["Thread do loop asyncio<br/>snapshot_engine/sync.py"]
        BO["Thread de observabilidade<br/>do Snapshot Engine, buffered"]
        QL["Thread QueueListener<br/>observabilidade"]
    end
    WM -->|"PriceSnapshot, símbolos sujos,<br/>choques, wakeup event"| MT
    WU -->|"eventos de conta/ordem → cache shadow"| MT
    MT -->|"futures para leituras"| AL
    PF -->|"leituras pesadas"| AL
    MT -->|"eventos"| QL
    AL --> BO
```

| Recurso compartilhado | Quem escreve | Quem lê | Sincronização |
|---|---|---|---|
| Preços e janela de preços | thread do WS de mercado | thread principal | estruturas do `MarketDataHub` + evento de wakeup |
| Cache de user data | thread do WS de usuário | thread principal | `UserDataShadowCache` |
| Snapshots | loop asyncio | thread principal e pool | futures do `concurrent.futures` |
| Ordens e proteções | thread principal | — | sequencial + `RLock` de proteção no `FuturesTradingEngine` |

---

## 9. Acoplamento entre os mixins

Chamadas a métodos definidos em **outro** mixin ou na classe base, sempre através de `self`:

```mermaid
flowchart LR
    TP["TradePipelineMixin<br/>4.317 linhas"]
    SE["SymbolEvalMixin<br/>1.732"]
    CY["CycleMixin<br/>1.400"]
    OP["OpportunityMixin"]
    AI["AIGateMixin"]
    DF["DefensiveModeMixin"]
    UD["UserDataCacheMixin"]
    GV["AlphoraGovernor<br/>classe base"]
    TP -->|66| SE
    TP -->|41| GV
    TP -->|23| CY
    SE -->|23| GV
    SE -->|11| CY
    SE -->|9| UD
    SE -->|6| DF
    SE -->|5| AI
    CY -->|6| GV
    CY -->|5| OP
    CY -->|5| DF
    CY -->|4| SE
    OP -->|2| GV
```

| Mixin | Métodos próprios | Atributos de `self` lidos | `getattr/hasattr(self, …)` |
|---|---:|---:|---:|
| `TradePipelineMixin` | 62 | 12 | 24 |
| `SymbolEvalMixin` | 50 | 12 | 17 |
| `CycleMixin` | 44 | 26 | 21 |
| `UserDataCacheMixin` | 31 | 4 | 29 |
| `OpportunityMixin` | 19 | 5 | 10 |
| `DefensiveModeMixin` | 11 | 4 | 5 |
| `AIGateMixin` | 6 | 3 | 0 |

**Leitura:** os mixins não são módulos independentes. São pedaços de **uma única classe** espalhados em arquivos.
As **106 consultas dinâmicas** com `getattr(self, …)` são contratos implícitos: dependências que nenhum tipo declara
e que o mypy não verifica. O `TradePipelineMixin` sozinho faz 130 chamadas a métodos definidos fora dele
(os números nas setas são pontos de chamada, não métodos distintos).

[← Pacotes e dependências](pacotes-e-dependencias.md) · [Índice](README.md) · [Decisão, risco e execução →](decisao-e-execucao.md)
