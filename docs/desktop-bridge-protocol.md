# Contrato da ponte desktop

O processo Python envia uma linha JSON de prontidão antes de receber chamadas:

```json
{"event":"ready","data":{"protocol":1,"root":"...","run_id":"...","options":{}}}
```

A interface Rust exige `data.protocol == 1`. Campo ausente ou versão diferente deixa a ponte indisponível e mostra as duas versões na mensagem de erro. Chamadas e respostas continuam correlacionadas por `id`; os 39 nomes de método usados pela interface são comparados com os handlers Python em `tests/desktop/test_bridge.py`.

As tarefas de treinamento e backtest escrevem eventos JSON por linha com `type` e `version: 1`. Os leitores ignoram versões desconhecidas e linhas inválidas. Os tipos consumidos são:

| Tipo | Campos usados | Consumidor |
|---|---|---|
| `ai_progress` | `message` | Tela de treinamento |
| `ai_model` | `path` | Serviço de treinamento |
| `ai_symbol_model` | `symbol`, `status`, `path` | Serviço de treinamento |
| `backtest_manifest` | `path` | Relatório de backtest |
| `robust_backtest_report` | `path` | Saída do CLI robusto |
| `backtest_request_valid` | — | Saída do CLI de validação |

`ai_warning` e `ai_registry` também usam o mesmo envelope. Os campos adicionais dos modelos permanecem no JSON para diagnóstico. O contrato é definido em `src/domain/subprocess_protocol.py` e não depende de marcadores de texto no stdout.
