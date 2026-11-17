# Backtesting confiável

O backtesting do Alphora reproduz sinais, risco, sizing e execução sobre candles
históricos. Um resultado lucrativo não é automaticamente aprovado: o relatório só
emite `BACKTEST_OK` quando os critérios econômicos **e** todos os gates de
confiabilidade passam. Caso contrário, o resultado fica como `RESEARCH_ONLY` ou é
rejeitado.

## Camadas de confiabilidade

### P0 — integridade antes do resultado

- valida OHLCV, timestamps, alinhamento do timeframe, duplicidades e gaps de cadência;
- valida também séries multi-timeframe, não apenas o timeframe principal;
- separa aquecimento (`data_start_timestamp`) da janela negociável
  (`evaluation_start_timestamp`);
- executa sinais `ON_OPEN` e `ON_CLOSE` sem expor candles ainda não fechados;
- uma entrada `NEXT_OPEN` participa de stop, take e liquidação no próprio candle;
- gaps de stop são preenchidos na abertura adversa, nunca no preço inalcançável;
- `FAIL_IF_OPEN` realmente interrompe a execução;
- modelos de IA precisam registrar treino/validação/teste purgados, e o backtest deve
  começar depois de todo o dataset usado para construir e avaliar o artefato;
- apenas escrever `evaluation_mode=OUT_OF_SAMPLE` não basta: a janela isolada precisa
  existir e ser verificável.

### P1 — realismo operacional

- taxas maker/taker distintas, spread, slippage, funding histórico e custos de
  liquidação;
- `spread_bps` representa a largura bid/ask completa; cada ordem a mercado paga
  metade do spread, enquanto `slippage_bps` é aplicado por fill a mercado;
- limites maker exigem trade-through de ao menos um tick por padrão;
- entradas respeitam participação máxima no volume e podem receber fill parcial;
- impacto de mercado cresce com a raiz da participação no volume;
- saídas acima do limite de liquidez recebem impacto adicional conservador e reprovam
  o gate de confiabilidade;
- stops e liquidações consideram gaps; take-profit resting usa custo maker;
- preço efetivo de cada fill, participação, quantidade solicitada/preenchida e custos
  ficam registrados no CSV de trades;
- a curva de equity é mark-to-market e inclui custos estimados das posições abertas.

OHLCV não informa a ordem exata dos negócios dentro do candle nem a profundidade do
book. Ambiguidades intrabar são registradas e, por padrão, resolvidas de forma
conservadora. Qualquer ambiguidade observada impede aprovação automática.

### P2 — confiança estatística

- intervalos de confiança por circular block bootstrap para expectancy, profit factor
  e drawdown;
- intervalo de Wilson para win rate;
- Probabilistic Sharpe Ratio e Deflated Sharpe Ratio, corrigido pelo número de testes
  de parâmetros;
- Monte Carlo determinístico para risco de ruína de 50% do capital;
- estabilidade cronológica por folds;
- matriz de stress com multiplicadores de custos e slippage adicional;
- runner robusto com folds walk-forward isolados, purge, embargo, estado novo por fold
  e perturbações locais de parâmetros.

O seed, os limiares e a quantidade de amostras ficam no manifest para repetibilidade.

## Execução

O CLI recebe um arquivo JSON versionado:

```bash
python -m backtesting.adapters.cli validate --request request.json
python -m backtesting.adapters.cli run --request request.json
python -m backtesting.adapters.cli robust --request request.json
```

`run` produz os artefatos de uma execução. `robust` executa baseline OOS, walk-forward
e sensibilidade de parâmetros, gravando `robust_<run_id>.json` no diretório de saída.
Esse arquivo também carrega request/config completos, hashes de entrada, fingerprint do
código-fonte, versões do ambiente e seed.

Exemplo dos campos de confiabilidade mais relevantes:

```json
{
  "symbols": ["BTCUSDT"],
  "data": {
    "csv_by_symbol": {"BTCUSDT": "data/market/BTCUSDT_1m.csv"},
    "timeframe": "1m",
    "funding_csv_by_symbol": {"BTCUSDT": "data/market/BTCUSDT_funding.csv"},
    "data_start_timestamp": "2024-01-01T00:00:00Z",
    "data_end_timestamp": "2025-01-01T00:00:00Z",
    "evaluation_start_timestamp": "2024-07-01T00:00:00Z",
    "evaluation_end_timestamp": "2025-01-01T00:00:00Z",
    "fail_on_quality_warnings": true
  },
  "policies": {
    "evaluation_mode": "OUT_OF_SAMPLE",
    "purge_candles": 5,
    "embargo_candles": 5,
    "walk_forward_folds": 5,
    "min_profitable_walk_forward_folds": 3,
    "bootstrap_samples": 5000,
    "confidence_level": "0.95",
    "random_seed": 1729,
    "parameter_trials": 11,
    "min_probabilistic_sharpe": "0.95",
    "min_deflated_sharpe": "0.95",
    "max_risk_of_ruin": "0.01",
    "risk_of_ruin_horizon_trades": 1000
  },
  "execution": {
    "max_volume_participation": "0.05",
    "reliable_volume_participation": "0.05",
    "allow_partial_fills": true,
    "require_limit_trade_through": true,
    "intrabar_limit_volume_fraction": "0.25",
    "market_impact_bps_at_full_participation": "5"
  }
}
```

Os demais campos do request têm defaults compatíveis e são materializados no
manifest.

Baixe funding histórico para o mesmo símbolo e período dos candles antes de validar:

```bash
python -m research.cli download-funding \
  --symbol ETHUSDT --start 2025-06-01 --end 2026-06-01
```

O desktop encontra automaticamente o arquivo padrão
`data/market/SYMBOL_funding_INICIO_FIM.csv`. Se funding estiver habilitado e a
cobertura estiver ausente, o gate de confiabilidade reprova a execução.

O filtro de regime padrão bloqueia entradas contra uma tendência forte e nunca
bloqueia uma saída. O modo usado fica no manifest e cada decisão registra o motivo
da aprovação ou bloqueio.

Modelos probabilísticos novos usam OHLCV, regressão logística L2, calibração Platt e
valor esperado líquido dos custos. Calibração isolada não promove um modelo: para se
tornar candidato a paper ele também precisa demonstrar amostra, expectancy líquida e
profit factor positivos na janela cronológica de teste. Testnet e real continuam
bloqueados até validações posteriores.

## Artefatos e auditoria

Cada execução grava:

- resumo JSON com métricas, warnings, gates e veredito;
- trades CSV com custos e detalhes de execução;
- decisões e rejeições em JSONL;
- equity CSV mark-to-market;
- manifest schema v2.

O manifest contém configuração integral, hash do request, commit e estado do Git,
hash/mtime/tamanho de candles, funding e modelos, versão do Python, plataforma,
dependências (incluindo Coalestra) e seed estatístico.

## Critério de uso

Considere um resultado candidato somente quando:

1. `reliability_passed` for `true`;
2. `operational_verdict` for `BACKTEST_OK`;
3. o relatório robusto retornar `ROBUST_BACKTEST_OK`;
4. paper trading e testnet confirmarem custos, latência e comportamento operacional.

Um resultado `REJECTED`, mesmo que tenha poucos recortes lucrativos, não deve ser
otimizado no mesmo período até “ficar verde”. Reserve dados novos para confirmação;
caso contrário, o aparente ganho é seleção pós-hoc e não evidência fora da amostra.

Mesmo assim, backtesting mede evidência histórica, não garante desempenho futuro.
