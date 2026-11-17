# Replay golden do Governor

Cada cenário usa `config.json`, `snapshots.json`, `candles.csv` e `account.json`. O
Governor e o executor são os reais; o cliente da exchange responde com esses dados,
registra mutações e simula os fills. `expected.json` congela a sequência de estágios,
decisões, plano, comando e chamadas ao cliente. O replay remove durações e substitui
IDs gerados durante a execução por marcadores estáveis. Preços, quantidades e demais
valores financeiros são comparados exatamente.

Execute `pytest tests/golden -q`. O CI repete o replay 20 vezes em Python 3.10,
3.11, 3.12 e 3.13. Para uma mudança **intencional** de comportamento, use
`pytest tests/golden --update-golden` apenas no PR identificado com o rótulo
`behavior-change`; revise o diff de cada `expected.json` antes do merge. A
criação inicial dos arquivos da M0 é a baseline, anterior a qualquer refatoração.
