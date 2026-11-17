# Mecanismo de tentativas

Implementação: `src/resilience/retry_engine/`.

`policy.py` configura tentativas, espera, condições de parada e ações em eventos.
`delays/`, `conditions/` e `stop/` contêm as regras combináveis. `executors/` e
`internal/` executam as chamadas síncronas e assíncronas. `budget.py` limita o
consumo de tentativas e `http.py` oferece políticas para respostas HTTP.

O Alphora usa este mecanismo por `src/resilience/polling.py`,
`src/resilience/policies.py` e `src/resilience/budgets.py`. Os testes próprios
ficam em `tests/retry_engine/`; as integrações de negociação são cobertas por
`tests/trading/`. Não há dependência do pacote externo Relinker.
