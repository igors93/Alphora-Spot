# ADR-004 — Endpoint de candles explícito na configuração

- Estado: aceito
- Data: 13/11/2026
- Contexto: `CandleDataProvider` busca klines em `https://fapi.binance.com/fapi/v1/klines` por padrão, e a raiz de
  composição o criava sem argumento. Em `TESTNET_LIVE` as ordens, a conta e o preço vão para a testnet, mas os candles
  vinham do endpoint de produção, sem que a configuração, o resumo do run ou os logs dissessem isso.
- Problema a resolver: confirmar se a mistura é intencional e torná-la visível e configurável.
- Alternativas consideradas:
  1. derivar o endpoint do ambiente (testnet → `testnet.binancefuture.com`);
  2. manter o endpoint de produção para todos os modos, mas explícito na configuração;
  3. exigir que o operador escolha o endpoint em cada `.env`.
- Decisão: alternativa 2. Klines são dados públicos; os da testnet têm pouco volume, lacunas e preços que não refletem
  o mercado, e as estratégias e o regime calculados sobre eles não representam o que a conta real veria. O padrão
  continua sendo o endpoint de produção em todos os modos, agora como campo `candle_klines_url` do `AppConfig`
  (variável `ALPHORA_CANDLE_KLINES_URL`), validado (HTTPS, caminho `/fapi/v1/klines`), mostrado no resumo de
  configuração e registrado no audit log quando o hub de candles sobe.
- Consequências positivas: o comportamento deixa de ser implícito; quem quiser testar com os candles da testnet aponta
  a variável para `https://testnet.binancefuture.com/fapi/v1/klines` sem mudar código; o resumo do run documenta de
  onde vieram os candles.
- Consequências negativas / trade-offs: em `TESTNET_LIVE` o sinal é calculado com candles de produção e executado na
  testnet, cujo preço pode divergir. O preço de execução continua vindo do stream e do REST da testnet e passa pelas
  mesmas verificações de frescor e de preço do plano; a divergência afeta a qualidade do sinal, não a segurança da
  ordem.
- Invariantes afetados: nenhum; o padrão é o mesmo endpoint de antes.
- Plano de migração: adicionar o campo e a variável, passar o valor ao `CandleDataProvider` na composição, incluir no
  resumo e no audit log de partida do hub.
- Como validar: `tests/config/test_config_symbols.py` (leitura e validação da variável),
  `tests/governor/test_composition_candles.py` (o provider recebe o endpoint configurado) e golden replay.
- Links para evidências: `src/market_data/candle_provider.py`, `src/governor/composition.py`,
  `docs/MIGRATION_PLAN.md` (M5, passo 3).
