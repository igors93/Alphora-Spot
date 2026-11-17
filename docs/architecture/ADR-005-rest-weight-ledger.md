# ADR-005 — Contagem única de peso REST e back-off de 429/418

- Estado: aceito
- Data: 13/11/2026
- Contexto: a Binance limita o peso das requisições REST por IP e por minuto (`REQUEST_WEIGHT`, 2.400 em Futures),
  informa o peso usado em `X-MBX-USED-WEIGHT-1M` e responde 429 quando o limite estoura. Requisições enviadas depois
  de um 429 viram 418, um banimento do IP que cresce de 2 minutos a 3 dias; as duas respostas trazem `Retry-After`.
  Antes da M5 três controles reagiam a essa pressão sem contagem comum (OPT-036):
  - o Retry Engine repetia 418 e 429 nas leituras públicas (candles, downloaders), com teto de 30 s de espera;
  - o transporte REST do Snapshot Engine repetia 418 e 429 dentro do próprio orçamento de ~6 s;
  - o orçamento de checagens pesadas do ciclo contava checagens, não peso.
  Nenhum deles lia o cabeçalho de peso, e o cliente Binance não guardava `Retry-After`: depois de um 429, cada
  chamador continuava enviando, justamente o que transforma um 429 em banimento.
- Problema a resolver: medir o peso realmente usado (OPT-009) e fazer os chamadores obedecerem a uma contagem e a um
  back-off comuns, sem mudar o comportamento normal do ciclo.
- Alternativas consideradas:
  1. um limitador de taxa local que estime o peso de cada endpoint antes de enviar;
  2. um contador compartilhado alimentado pelas respostas (cabeçalho de peso e `Retry-After`), com back-off por host;
  3. manter os três controles e só registrar métricas.
- Decisão: alternativa 2. `binance/rest_weight.py` mantém um `RestWeightLedger` por processo, por host (produção e
  testnet têm limites separados):
  - o `BinanceFuturesClient` e as leituras públicas em `binance/public_rest.py` registram cada resposta: caminho,
    chamador (`rest_source`, por exemplo `snapshot:alphora-rest-private`, `candles`, `research:klines`,
    `research:funding` ou `desktop:backtest_rules`), status e o peso informado pela Binance. Isso inclui os
    downloaders de pesquisa e a consulta de regras do desktop;
  - 429 ou 418 abrem um back-off do host pelo `Retry-After` (sem cabeçalho: 10 s para 429, 120 s para 418). Enquanto
    ele durar, o cliente **não envia** e falha na hora com `BinanceFuturesRateLimited` (código `-1003`,
    `retry_after_seconds`), inclusive para ordens; um banimento em curso nunca é encurtado por um `Retry-After` menor;
  - 418 deixa de ser repetido pelas leituras públicas; 418 e 429 deixam de ser repetidos dentro do orçamento do
    Snapshot Engine (o `Retry-After` excede o orçamento). 429 continua sendo repetido pelas leituras públicas depois do
    `Retry-After`, e o ledger recusa uma repetição antecipada. A classificação de erros de execução não muda: 418/429
    continuam transitórios no nível do ciclo;
  - o orçamento de checagens pesadas lê a mesma pressão (peso usado sobre `ALPHORA_REST_WEIGHT_LIMIT_1M`, 1,0
    durante um back-off). `ALPHORA_REST_WEIGHT_GUARD_MODE=OBSERVE` (padrão) registra `rest_weight_pressure` no audit
    log quando a pressão passa de `ALPHORA_REST_WEIGHT_GUARD_RATIO` (0,8); `ENFORCE` adia checagens opcionais com o
    motivo `REST_WEIGHT_PRESSURE`; `OFF` ignora. Posição aberta e recuperação/proteção nunca são adiadas;
  - o evento `governor_cycle_metrics` ganha `rest_weight`: por host, requisições, por chamador, caminhos mais
    frequentes, 429/418, recusas durante back-off, peso usado; e o que mudou desde o evento anterior.
- Consequências positivas: o peso real passa a ser medido; um 429 não vira banimento por insistência dos próprios
  chamadores; o operador vê no journal quanto peso o ciclo gasta e quem gasta.
- Consequências negativas / trade-offs: durante um back-off o ciclo recebe erros imediatos em vez de esperar; as
  leituras e a revalidação de entrada falham fechadas, como em qualquer indisponibilidade de dados. A pressão só existe
  quando a Binance informa o peso (os testes e o golden não informam, então nada muda neles).
- Invariantes afetados: nenhum evento existente muda; `governor_cycle_metrics` ganha uma chave aditiva.
- Plano de migração: OBSERVE por padrão; depois de medir o peso por ciclo em TESTNET e REAL, decidir se ENFORCE vira
  padrão e se a estimativa de peso por endpoint entra antes do envio.
- Como validar: `tests/binance/test_rest_weight.py` (ledger, `Retry-After`, back-off, cliente que recusa enviar),
  `tests/binance/test_rest_weight_callers.py` (transporte do Snapshot Engine, candles, orçamento de checagens pesadas,
  métricas do ciclo), `tests/binance/test_public_rest.py` (pesquisa e desktop),
  `tests/trading/test_klines_policy.py` (418 não repetido) e golden replay.
- Links para evidências: `src/binance/rest_weight.py`, `src/binance/futures_client.py`,
  `src/binance/public_rest.py`, `src/market_data/candle_provider.py`,
  `src/market_data/snapshot_provider/transport.py`, `src/governor/services/request_budget.py`,
  `docs/MIGRATION_PLAN.md` (M5, passo 4).
