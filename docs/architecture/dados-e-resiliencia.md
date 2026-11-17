# Dados de mercado e resiliência

[← Decisão, risco e execução](decisao-e-execucao.md) · [Índice](README.md) · [Pesquisa, observabilidade e desktop →](pesquisa-observabilidade-desktop.md)

---

## 1. As quatro vias de dados

```mermaid
flowchart LR
    subgraph BIN["Binance"]
        WSM["WS de mercado<br/>bookTicker"]
        WSU["WS de usuário<br/>listenKey"]
        RPUB["REST público<br/>exchangeInfo, ticker,<br/>premiumIndex, klines"]
        RPRI["REST privado<br/>account, positionRisk,<br/>openOrders, openAlgoOrders"]
    end
    subgraph V1["Via 1 · preço em tempo real"]
        MDH["MarketDataHub<br/>thread própria"]
        PW["MarketPriceWindow<br/>MarketShockDetector"]
    end
    subgraph V2["Via 2 · snapshots consistentes"]
        PROV["SnapshotProvider<br/>market_data/snapshot_provider"]
        SE["Snapshot Engine<br/>market_data/snapshot_engine"]
    end
    subgraph V3["Via 3 · candles"]
        CDP["CandleDataProvider<br/>REST klines + retry"]
        CDH["CandleDataHub"]
        CDS["CandleDataStore"]
    end
    subgraph V4["Via 4 · eventos privados"]
        UDS["UserDataStreamHub<br/>+ keepalive"]
        UDC["UserDataShadowCache<br/>audit · usage · stale guard ·<br/>reconciliation · assisted plan"]
    end
    MDR["MarketDataRouter<br/>snapshots leve e pesado"]
    G(["Governor"])
    WSM --> MDH --> PW
    MDH -->|"lido sob demanda<br/>alphora-market-stream"| PROV
    RPUB --> SE
    RPRI --> SE
    SE <--> PROV
    RPUB --> CDP --> CDH --> CDS
    WSU --> UDS --> UDC
    UDC -->|"publica atualizações"| PROV
    PROV --> MDR
    CDH --> MDR
    MDH --> MDR
    MDR --> G
    PW -->|"símbolos sujos, choques, wakeup"| G
```

| Observação | Evidência |
|---|---|
| O preço do WebSocket entra no Snapshot Engine **por leitura sob demanda**: a fonte `alphora-market-stream` (autoridade 200) consulta o `MarketDataHub` na resolução e o REST (autoridade 40) só responde sem stream; não há publicação por tick | [ADR-001](ADR-001-market-stream-snapshot-boundary.md), [ADR-003](ADR-003-websocket-price-in-snapshot-engine.md) |
| Candles vêm do endpoint **real** da Binance em todos os modos, inclusive `TESTNET`, por decisão explícita: `ALPHORA_CANDLE_KLINES_URL` (campo `candle_klines_url`) define o endpoint, validado, mostrado no resumo de configuração e no evento `candle_hub_started` | [ADR-004](ADR-004-candle-endpoint.md), `governor/composition.py` |

---

## 2. Como o Governor pede dados ao provider

Cada ciclo abre uma **sessão** (`SnapshotCycleSession`) e pede conjuntos de recursos cada vez mais específicos:

```mermaid
sequenceDiagram
    participant G as run_cycle / estágios
    participant S as Sessão do ciclo
    participant E as Snapshot Engine
    G->>S: open_cycle_session
    G->>S: resolve_baseline
    G->>S: resolve_light_selected(símbolos)
    G->>S: resolve_selected(símbolos devidos)
    par prefetch paralelo
        G->>S: try_prefetch_symbol_evaluation_snapshots
    end
    loop por símbolo, na avaliação pesada
        G->>S: resolve_symbol_evaluation(símbolo)
        G->>S: revalidate_for_execution(símbolo)
    end
    S->>E: chaves de recurso + autoridade + frescor
    E-->>S: Snapshot
    G->>S: close
```

Desde a M5 o provider é o pacote `market_data/snapshot_provider/`, dividido por responsabilidade (nenhum módulo acima
de 800 linhas, medido pela catraca): `provider.py` (fachada), `session.py` (sessão do ciclo e revalidação para
execução), `policies.py` e `readiness.py` (aceitação, consistência, degradação e prontidão por perfil), `builder.py`,
`fetchers.py` e `transport.py` (fontes, frescor e orçamento REST), `user_data.py`, `submissions.py` e
`reconciliation.py` (eventos privados), `health.py`, `diagnostics.py` e `engine_errors.py` (observabilidade) e
`wire_names.py` (nomes persistidos herdados, [ADR-002](ADR-002-snapshot-provider-naming.md)).

---

## 3. Dentro do Snapshot Engine (11.299 linhas)

```mermaid
flowchart TB
    SYNC["sync.py<br/>fachada síncrona sobre um loop asyncio em thread"]
    subgraph ORQ["orchestration/"]
        BLD["builder.py<br/>1.253 linhas"]
        SES["session.py"]
        CAT["source_catalog · source_calls · source_executor"]
        SF["singleflight · refresh · cache_access · lifecycle"]
    end
    subgraph CORE["core/"]
        KEYS["keys · request · models · protocols"]
        AUTH["authority · consistency · acceptance · quality"]
        HL["health.py · deadline · source_timeout"]
    end
    CON["concurrency/<br/>capacity · dispatch"]
    CACHE["cache/<br/>memory · publisher"]
    RES["resilience/"]
    OBS["observability/<br/>buffered · events · metrics"]
    ADP["adapters/<br/>batch · callable · derived"]
    SYNC --> ORQ
    ORQ --> CORE
    ORQ --> CON
    ORQ --> CACHE
    ORQ --> RES
    ORQ --> OBS
    ORQ --> ADP
```

---

## 4. Endpoints da Binance usados

<details open>
<summary>▶ REST (cliente em <code>src/binance/futures_client.py</code>)</summary>

| Endpoint | Tipo | Uso |
|---|---|---|
| `fapi/v1/ping`, `fapi/v1/time` | público | conectividade e sincronização de relógio |
| `fapi/v1/exchangeInfo` | público | regras dos símbolos, *bootstrap* no início |
| `fapi/v2/ticker/price`, `fapi/v1/premiumIndex` | público | preço e *mark price* |
| `fapi/v1/klines` | público | candles (via `CandleDataProvider` e downloaders) |
| `fapi/v2/account`, `fapi/v1/income` | privado | saldo, margem, funding/PnL |
| `fapi/v2/positionRisk`, `fapi/v1/positionSide/dual` | privado | posições e modo de posição |
| `fapi/v1/openOrders`, `fapi/v1/openAlgoOrders` | privado | ordens abertas e proteções |
| `fapi/v1/listenKey` | privado | User Data Stream |
| `fapi/v1/order` (POST, DELETE, GET), `fapi/v1/order/test` | mutação | entrada, saída, consulta |
| `fapi/v1/algoOrder`, `fapi/v1/algoOpenOrders`, `fapi/v1/allOpenOrders` | mutação | proteções e cancelamentos |
| `fapi/v1/marginType`, `fapi/v1/leverage` | mutação | configuração do símbolo no boot |

</details>

---

## 5. Resiliência e orçamento de requisições

```mermaid
flowchart LR
    CALL["Chamada REST"] --> POL["resilience/policies.py<br/>políticas Binance: leitura pública,<br/>resync de timestamp…"]
    POL --> RE["Retry Engine<br/>resilience/retry_engine, 5.360 linhas"]
    RE --> BUD["budget.py<br/>limita tentativas"]
    RE --> HTTP["http.py<br/>respostas HTTP"]
    CYC["Ciclo do Governor"] --> RB["governor/request_budget.py<br/>budget por ciclo"]
    RB -->|"estágio 8 bloqueia avaliação pesada<br/>sem budget"| CYC
    SEN["Snapshot Engine"] --> SR["snapshot_engine/resilience"]
    BC["BinanceFuturesClient<br/>CandleDataProvider"] -->|"cada resposta:<br/>peso usado, 429/418"| LED["binance/rest_weight.py<br/>RestWeightLedger por host"]
    LED -->|"back-off por Retry-After:<br/>não envia"| BC
    LED -->|"pressão de peso"| RB
    LED -->|"rest_weight"| MET["governor_cycle_metrics"]
```

Existem três mecanismos de controle de pressão: o Retry Engine com budgets, o budget de requisições do ciclo e a
resiliência própria do Snapshot Engine. Desde a M5 eles compartilham **uma contagem**
([ADR-005](ADR-005-rest-weight-ledger.md)): o `RestWeightLedger` registra cada resposta REST com o peso informado pela
Binance, abre um back-off do host num 429/418 que todos os chamadores obedecem (o cliente falha na hora em vez de
estender um banimento), e o budget de checagens pesadas lê a mesma pressão (`OBSERVE` por padrão, `ENFORCE` adia
checagens opcionais). O consumo aparece em `rest_weight` nas métricas do ciclo.

---

## 6. Cadências conhecidas

| Parâmetro | Valor | Onde |
|---|---|---|
| Intervalo do loop | 3 s (padrão da interface) | `loop_interval_seconds` |
| Intervalo máximo entre avaliações de um símbolo | 300 s | `PriorityScheduler(max_interval_seconds=300)` |
| Idade máxima dos dados no scheduler | 30 s | `PriorityScheduler(max_data_age_seconds=30)` |
| Ping dos WebSockets | 20 s (timeout 10 s) | `market_data_hub.py`, `user_data_stream.py` |
| Keepalive do listenKey | 1800 s (padrão da interface) | `user_data_stream_keepalive_seconds` |
| Atualização da tabela de símbolos na interface | 3 s, só com a tela visível | `interface/src/pages/symbols.rs`, `operation.rs` |
| Atualização do Monitor na interface | 5 s, só com a tela visível | `interface/src/pages/observability.rs` |

[← Decisão, risco e execução](decisao-e-execucao.md) · [Índice](README.md) · [Pesquisa, observabilidade e desktop →](pesquisa-observabilidade-desktop.md)
