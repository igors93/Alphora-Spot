# Pesquisa, observabilidade e desktop

[← Dados de mercado e resiliência](dados-e-resiliencia.md) · [Índice](README.md)

---

## 1. Backtest

```mermaid
flowchart TD
    UI["Interface: Executar backtest"] --> BR["Ponte: backtest.run<br/>valida e grava request.json"]
    BR --> RQ[("runtime/backtests/RUN/request.json")]
    RQ --> CLI["python -m backtesting.adapters.cli run --request"]
    CLI --> SVC["BacktestApplicationService"]
    SVC --> RB["run_backtest<br/>backtesting/engine/engine.py"]
    RB --> V["Validação de isolamento treino/teste<br/>timeframes e sizing"]
    V --> FEED["HistoricalDataFeed<br/>CSV de candles + funding"]
    FEED --> LOOP["Para cada instante:<br/>eventos de candle"]
    LOOP --> GS["GovernorSimulator<br/>decision_pipeline.py, 1.969 linhas"]
    GS --> EXS["ExecutionSimulator<br/>fills, custos, slippage"]
    GS --> PS["PortfolioSimulator"]
    GS --> SRT["BacktestStrategyRuntime<br/>mesmas estratégias do live"]
    LOOP --> MET["build_backtest_summary<br/>metrics.py"]
    MET --> REP[("reports/: summary.json, trades.csv,<br/>decisions.jsonl, rejected.jsonl, equity.csv")]
    REP --> MAN["Manifesto → linha BACKTEST_MANIFEST= no stdout"]
    MAN --> BR2["Ponte: backtest.result<br/>lê o relatório e reduz a curva de equity"]
    CLI -.->|"modo robust"| ROB["run_robust_backtest<br/>janelas walk-forward +<br/>perturbação de parâmetros"]
```

**Ponto central para otimizar:** as estratégias são as mesmas do live (`BacktestStrategyRuntime`), mas a **cadeia de
decisão não é**. O `GovernorSimulator` reimplementa sinal → plano → gates → sizing → execução em 1.969 linhas,
separado dos 22 estágios do Governor. Quando uma regra muda no live, o backtest não muda junto.

---

## 2. Pesquisa e treino de IA

```mermaid
flowchart LR
    CLI["research.cli"] --> DK["download-klines<br/>download-funding"]
    DK --> MKT[("data/market/*.csv")]
    CLI --> TR["train-ai-probabilistic-<br/>global · refined · all"]
    MKT --> TR
    TR --> TS["probabilistic_training_service.py"]
    TS --> MDL["probabilistic.py<br/>modelo linear probabilístico"]
    MDL --> REG[("models/ai/global · refined<br/>+ registry")]
    TR -->|"AI_PROGRESS · AI_MODEL ·<br/>AI_SYMBOL_MODEL no stdout"| UI["Interface e ponte<br/>training.done registra o modelo"]
    REG --> STR["strategies/futures/ai/<br/>probabilistic_strategy + resolver"]
    REG --> GATE["Gate de probabilidade<br/>AIGateMixin"]
    REG --> BT["Backtest em modo IA"]
```

O contrato entre treino e uso é feito **por linhas de texto no stdout** (`AI_MODEL:`, `BACKTEST_MANIFEST=`,
`Candles baixados:`), interpretadas pela ponte.

---

## 3. Observabilidade e auditoria

```mermaid
flowchart TB
    subgraph CODE["Código de negócio"]
        OE["observe_event / observe_error<br/>observability"]
        AE["audit_event / audit_error<br/>utils/audit_logger.py"]
        TJ["TradeJournal<br/>utils/trade_journal.py"]
        LOG["logging.getLogger"]
    end
    subgraph PIPE["Pipeline"]
        PRIV["logprivacy<br/>sanitização obrigatória"]
        QL["QueueListener<br/>thread própria"]
        TXT["text_logging"]
        CR["crash_writer"]
        RET["retention"]
    end
    subgraph RUNDIR["runtime/runs/RUN_ID/"]
        EV[("events/CATEGORIA_events.jsonl")]
        LG[("logs/alphora.log<br/>task_stdout.log")]
        HJ[("health · errors · invariants ·<br/>diagnostics · dropped_events")]
        MF[("run_manifest.json · run_summary.json<br/>observability_summary.json")]
    end
    DATA[("data/: governor_events, trades,<br/>trade_audit, scanner_scores")]
    OE --> PRIV --> QL --> EV
    QL --> HJ
    AE --> PRIV
    AE --> EV
    AE --> MF
    LOG --> TXT --> LG
    TJ --> DATA
    CR --> HJ
    RET --> RUNDIR
```

| Observação | Evidência |
|---|---|
| Dois sistemas paralelos de eventos: `observability` e `utils/audit_logger` | `engine.py` chama `audit_event` e `observe_event` em sequência com o mesmo conteúdo (`config_loaded`, `shutdown_requested`…) |
| Mais um canal de journals em `data/` | `TradeJournal` escreve `governor_events`, `trades`, `scanner_scores`, `trade_audit` |
| `utils` importa `observability` 10 vezes e vice-versa | ciclo `observability ↔ utils` |

---

## 4. Interface desktop (Rust)

```mermaid
flowchart TB
    MAIN["main.rs<br/>janela, ícone, log de crash"]
    APP["app.rs<br/>menus, barra de comando, abas,<br/>status, console, barra de salvar"]
    CMD["commands.rs<br/>todas as ações + atalhos"]
    PAL["palette.rs<br/>Ctrl+K"]
    subgraph PAGES["pages/"]
        OPP["operation · Painel"]
        SYM["symbols"]
        STR["strategy + intelligence"]
        RES["backtest + training"]
        MON["observability · Monitor"]
        CRD["credentials"]
    end
    BRG["bridge.rs<br/>JSON lines, timeouts, restart"]
    PRC["process.rs<br/>Job Objects, saída unificada"]
    LOGS["logs.rs<br/>buffer, classificação, tail"]
    UIK["widgets · theme · icons ·<br/>toast · dialogs"]
    WS["workspace.rs<br/>raiz do projeto, Python, run id"]
    MAIN --> APP
    APP --> CMD
    APP --> PAL
    APP --> PAGES
    PAGES --> BRG
    PAGES --> PRC
    PAGES --> LOGS
    PAGES --> UIK
    BRG --> WS
    PRC --> WS
```

### Iniciar o Alphora pela interface

```mermaid
sequenceDiagram
    autonumber
    participant U as Operador
    participant UI as Interface Rust
    participant BR as Ponte Python
    participant PF as ops.preflight
    participant EN as Engine

    U->>UI: Iniciar (F9)
    UI->>BR: operation.prepare(modo, ambiente)
    BR-->>UI: confirmações exigidas e resumo
    loop cada confirmação
        UI->>U: diálogo: reconhecer, checklist ou digitar token
    end
    UI->>BR: operation.authorize(respostas)
    BR->>BR: grava configuração e perfis
    BR->>PF: subprocess.run --json
    PF-->>BR: pronto, avisos ou parcial
    opt preflight com avisos ou parcial
        BR-->>UI: nova confirmação
        UI->>BR: operation.confirm_preflight
    end
    BR-->>UI: comando + ambiente do engine
    UI->>EN: spawn em Job Object
    EN-->>UI: stdout e alphora.log em tempo real
    U->>UI: Parar (Shift+F9)
    UI->>UI: grava runtime/governor_stop.request
    EN-->>UI: encerra no próximo ciclo
```

| Fato | Valor |
|---|---|
| Timeout padrão de requisição à ponte | 45 s (`authorize` 150 s, preparos de pesquisa 90 s) |
| Reinício automático da ponte | até 3 tentativas com espera crescente |
| Carregamento das telas | todas as páginas carregam em segundo plano ao conectar |
| Parada forçada | 15 s após a parada graciosa, por `TerminateJobObject` |

[← Dados de mercado e resiliência](dados-e-resiliencia.md) · [Índice](README.md)
