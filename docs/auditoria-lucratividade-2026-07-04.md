# Auditoria de lucratividade — 2026-07-04

## Veredito

O Alphora ficou mais conservador, auditável e coerente com custos reais, mas a
estratégia atual **não está aprovada para capital real**. O teste anual completo
melhorou, porém a metade cronológica isolada permaneceu negativa e a janela realmente
nova ainda não produziu amostra.

## Evidência ETHUSDT 15m/1h

| Execução | Trades | Win rate | PnL líquido | Profit factor | Expectancy |
|---|---:|---:|---:|---:|---:|
| Baseline anterior, anual | 113 | 34,51% | -21,59 | 0,617 | -0,191 |
| Regime + custos + funding | 55 | 36,36% | -8,41 | 0,726 | -0,153 |
| Filtros finais, anual in-sample | 47 | 40,43% | +1,40 | 1,065 | +0,030 |
| Segunda metade cronológica OOS | 24 | 29,17% | -4,06 | 0,680 | -0,169 |
| Dados novos 2026-06-08 a 2026-07-03 | 0 | — | 0 | — | — |

Na segunda metade, apenas 2 de 5 folds foram lucrativos. O intervalo de confiança da
expectancy atravessou zero e todos os cenários de stress de custo ficaram negativos.
Funding, OHLCV e consistência entre timeframes passaram nos gates de qualidade.

## Mudanças mantidas

- spread como largura bid/ask completa e slippage por fill, iguais no planejamento e
  no backtest;
- funding histórico baixável e descoberto automaticamente pelo desktop;
- bloqueio de entradas contra tendência forte, sem bloquear saídas;
- rejeição de choques de volume extremos e confirmação de entrada opcional;
- modelo probabilístico OHLCV com regressão logística L2, calibração Platt e valor
  esperado líquido de custos;
- modelos sem edge OOS permanecem `RESEARCH_ONLY`; testnet/real não são aprovados;
- consistência OHLCV entre timeframes e sensibilidade sobre parâmetros reais da
  estratégia no runner robusto.

## Critério para avançar

Manter em testnet. Só considerar paper após pelo menos 30–100 trades realmente novos,
profit factor acima de 1,20, expectancy e limite inferior do intervalo de confiança
positivos, maioria dos folds lucrativa e stress de custo positivo. Capital real exige,
além disso, confirmação prolongada em paper e testnet com slippage observado.
