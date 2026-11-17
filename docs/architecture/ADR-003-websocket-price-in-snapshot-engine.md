# ADR-003 — Preço do WebSocket no Snapshot Engine: leitura sob demanda, sem publicação

- Estado: aceito
- Data: 13/11/2026
- Contexto: o ADR-001 removeu `publish_market_snapshot`, um método sem chamador que publicaria cada `bookTicker` do
  WebSocket no cache do Snapshot Engine, e deixou para a M5 decidir se o stream deveria entrar no engine. Ao dividir o
  provider ficou explícito que o preço do stream **já** chega ao engine, por outro caminho: a fonte
  `alphora-market-stream` (`SOURCE_MARKET_STREAM`, prioridade 100, autoridade 200) é uma `CallableBatchSource` que, ao
  resolver `market/state`, lê `MarketDataHub.latest_market_state` no instante da leitura
  (`SourceFetchers.market_stream_batch`). O REST (`alphora-rest-market`, autoridade 40) só responde quando o stream não
  tem estado para o símbolo.
- Problema a resolver: escolher entre manter a leitura sob demanda (pull) ou passar a publicar cada atualização do
  stream no cache (push).
- Alternativas consideradas:
  1. push: publicar cada `bookTicker` no cache do engine;
  2. pull: manter a fonte de stream consultada na resolução;
  3. os dois ao mesmo tempo.
- Decisão: alternativa 2. O preço do WebSocket continua entrando pelo pull da fonte `alphora-market-stream`; a API de
  publicação removida no ADR-001 não volta.
  - O frescor é decidido na leitura: `observed_at` vem do `event_time` da exchange, limitado ao `received_at` local para
    que relógio adiantado da exchange não crie observação no futuro; a política de frescor e a de aceitação do perfil
    (avaliação, entrada, saída) julgam esse valor.
  - A autoridade 200 faz o stream vencer o REST quando os dois têm valor; o REST continua como fallback com circuito
    próprio.
  - O custo é zero quando ninguém lê: o stream do Binance chega a dezenas de mensagens por segundo por símbolo, e o
    ciclo lê cada símbolo poucas vezes.
- Consequências positivas: uma única rota do stream para o engine; nenhuma escrita por tick no cache, nas métricas ou no
  audit log; o buffer de observabilidade do engine não satura com publicações; o comportamento atual (golden) é o
  comportamento decidido.
- Consequências negativas / trade-offs: o engine não guarda o histórico do stream — a janela de preços continua no
  `MarketPriceWindow` compartilhado com o hub. Um consumidor que precise de push (por exemplo, acordar o ciclo num
  choque de preço) usa o `MarketWakeup`/`MarketShockDetector` do hub, não o engine.
- Invariantes afetados: nenhum; o ADR registra o fluxo que já roda.
- Plano de migração: nenhum código muda. `docs/snapshot-engine.md` e este ADR descrevem a fonte de stream.
- Como validar: `tests/market_data/test_snapshot_provider_fetchers.py` (leitura no instante da resolução com o
  relógio do ciclo, `observed_at` limitado ao `received_at`, símbolo sem stream fica para o REST),
  `tests/market_data/test_snapshot_provider_sources.py` (autoridades) e golden replay.
- Links para evidências: [ADR-001](ADR-001-market-stream-snapshot-boundary.md),
  `src/market_data/snapshot_provider/fetchers.py`, `src/market_data/snapshot_provider/sources.py`.
