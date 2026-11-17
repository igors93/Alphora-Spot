# Mecanismo de snapshots

Implementação: `src/market_data/snapshot_engine/`.

O mecanismo reúne leituras de múltiplas fontes em snapshots consistentes. `core/`
define chaves, solicitações, políticas e modelos; `orchestration/` coordena fontes,
sessões e construção; `concurrency/` limita o despacho; `cache/` armazena e publica
atualizações; `observability/` emite métricas e eventos; `resilience/` protege as
fontes. `sync.py` oferece a interface síncrona usada pelo Alphora.

Os testes do núcleo ficam em `tests/snapshot_engine/`. Os testes de integração
ficam em `tests/market_data/`. O módulo mantém a versão de capacidade 0.6.4
para validar contratos internos, sem depender de pacote externo.

## Provider do Governor

`src/market_data/snapshot_provider/` liga o engine às fontes do Alphora e às necessidades do Governor:

| Módulo | Responsabilidade |
|---|---|
| `provider.py` | `SnapshotProvider`: fachada que o Governor guarda durante o run; snapshots avulsos, bootstrap e prefetch |
| `session.py` | `SnapshotCycleSession`: uma sessão cumulativa por ciclo e a revalidação antes de cada ordem |
| `factory.py` | `build_snapshot_provider`: sobe o provider sob a política de partida do modo de execução |
| `keys.py`, `sources.py` | Chaves de recurso lidas pelo Governor; nomes e autoridade de cada fonte |
| `builder.py`, `fetchers.py`, `transport.py` | Fontes, frescor e resiliência; leitura do cache de user data, do stream e do REST dentro de um orçamento de tempo |
| `policies.py`, `readiness.py` | Aceitação, consistência e degradação por perfil (baseline, avaliação, entrada, saída) e a prontidão resultante |
| `views.py` | Snapshots leve e pesado do Governor lidos de um snapshot do engine |
| `user_data.py`, `submissions.py`, `reconciliation.py` | Eventos do User Data Stream escritos no cache e reconciliação do que não chegou |
| `health.py`, `diagnostics.py`, `engine_errors.py`, `event_sink.py`, `metrics_sink.py` | Saúde do builder, diagnósticos para journal e para eventos de erro, eventos e métricas do engine no audit log |
| `wire_names.py` | Valores persistidos que mantêm a grafia antiga até a M7 ([ADR-002](architecture/ADR-002-snapshot-provider-naming.md)) |

O preço do WebSocket entra no engine pela fonte `alphora-market-stream`, lida sob demanda
([ADR-003](architecture/ADR-003-websocket-price-in-snapshot-engine.md)).

### Configuração

Lida por `src/config/snapshot_env.py`. Os nomes `ALPHORA_COALESTRA_*` anteriores à M5 continuam aceitos; quando os
dois existem, vale o novo ([ADR-002](architecture/ADR-002-snapshot-provider-naming.md)).

| Variável | Padrão | Uso |
|---|---:|---|
| `ALPHORA_SNAPSHOT_DEADLINE_SECONDS` | 7.0 | Prazo de uma leitura avulsa ou de revalidação |
| `ALPHORA_SNAPSHOT_REST_TIMEOUT_SECONDS` | 6.5 | Timeout de uma fonte REST no engine |
| `ALPHORA_SNAPSHOT_REST_TRANSPORT_TIMEOUT_SECONDS` | 6.0 | Orçamento total da chamada REST, tentativas incluídas (≥ 0,05 s abaixo do anterior) |
| `ALPHORA_SNAPSHOT_REST_CONNECT_TIMEOUT_SECONDS` | 1.0 | Teto de conexão por tentativa |
| `ALPHORA_SNAPSHOT_REST_READ_TIMEOUT_SECONDS` | 4.5 | Teto de leitura por tentativa (conexão + leitura ≥ 0,05 s abaixo do orçamento) |
| `ALPHORA_SNAPSHOT_REST_MAX_ATTEMPTS` | 4 | Tentativas dentro do orçamento |
| `ALPHORA_SNAPSHOT_REST_RETRY_DELAY_SECONDS` | 0.05 | Pausa entre tentativas |
| `ALPHORA_SNAPSHOT_EXECUTION_MAX_AGE_SECONDS` | 6.0 | Idade máxima dos dados numa entrada |
| `ALPHORA_SNAPSHOT_EXECUTION_MAX_SKEW_SECONDS` | 5.0 | Distância máxima entre observações de uma entrada |
| `ALPHORA_SNAPSHOT_EVALUATION_MAX_AGE_SECONDS` | 45.0 | Idade máxima dos dados na avaliação |
| `ALPHORA_SNAPSHOT_FALLBACK_ALLOWED` | — | Permite rodar paper/dry-run sem o provider; os modos live sempre o exigem |
| `ALPHORA_CANDLE_KLINES_URL` | `https://fapi.binance.com/fapi/v1/klines` | Endpoint de candles em todos os modos ([ADR-004](architecture/ADR-004-candle-endpoint.md)) |

O peso REST é contado num ledger compartilhado por todos os chamadores
([ADR-005](architecture/ADR-005-rest-weight-ledger.md)): `ALPHORA_REST_WEIGHT_GUARD_MODE` (`OFF`, `OBSERVE` padrão,
`ENFORCE`), `ALPHORA_REST_WEIGHT_GUARD_RATIO` (0,8) e `ALPHORA_REST_WEIGHT_LIMIT_1M` (2400). O transporte REST do
provider não repete 418/429 dentro do orçamento: o cliente recusa enviar até o `Retry-After` acabar.
