# M4: ciclo de vida de ordens e estado local

**Estado em 2026-11-13:** implementação concluída na `main`. O escopo e o critério de saída estão no
[plano de migração](MIGRATION_PLAN.md#m4--execução-e-fonte-única-de-verdade).

## Despacho e ciclo de vida

O estágio 22 aprova um `ExecutionCommand` e monta um `ExecutableTrade`. Quando esse objeto está disponível, o
estágio o envia ao `ExecutionCoordinator`, que escolhe o porto de abertura, fechamento ou nenhuma ação. O
`FuturesTradingEngine` de cada símbolo fica em `execution/`. As aberturas e os
fechamentos são divididos em passos nomeados. [`domain/orders.py`](../src/domain/orders.py) define os estados e
valida toda transição; um resultado incerto só pode ser resolvido por reconciliação, nunca por outro envio com um
novo ID de cliente.

Antes de enviar uma ordem mutável, o engine grava em disco uma intenção com `client_order_id` e histórico do
`OrderLifecycle`. [`OrderIntentStore`](../src/execution/entry_intent_store.py) usa escrita temporária, `fsync` e
substituição atômica. Falha de persistência bloqueia o envio. Intenções de entrada e fechamento ficam em sidecars
distintos junto do arquivo de estado. Na retomada, o engine consulta a ordem pelo mesmo ID e reconcilia a posição
da exchange antes de liberar outra mutação. Um resultado que continua ambíguo pausa o símbolo.

Após fill parcial, o engine cancela a parcela aberta, consulta novamente a ordem e a posição, e só então dimensiona
stop e take para a quantidade final. Se não puder provar que a entrada foi encerrada, mantém a intenção pendente e
pausa o símbolo. O mesmo princípio impede um segundo fechamento `reduceOnly` enquanto o primeiro está incerto.

## Estado de posição e projeções

[`PositionLedger`](../src/execution/ledger.py) é o dono do estado **local** da posição e das intenções pendentes.
Ele persiste a posição pelo `RuntimeStateStore`, com escrita temporária e `fsync`, mantendo o formato anterior de
`runtime/state_*.json` para a recuperação do boot. `FuturesTradingEngine.runtime_state` lê o ledger; não mantém
outro `RuntimeState` próprio.
Binance continua sendo a verdade externa, consultada e reconciliada antes de atualizar esse estado local.

`SymbolStateTracker` registra a trajetória de avaliação do Governor; seus estados não autorizam uma posição.
`governor_state.json` é uma projeção operacional para UI e comandos, alimentada pelo snapshot do Governor. Nele,
quantidade, direção e preço vêm da posição reconciliada da exchange; estado operacional e aviso de proteção vêm
do `RuntimeState` lido do ledger. Nenhum desses leitores persiste uma posição independente.

Os caminhos financeiros padrão incluem símbolo e ambiente em live; `DRY_RUN` e `PAPER_LIVE` acrescentam o modo.
Arquivos legados compartilhados (`runtime/state.json`, `data/trades.jsonl`) exigem migração explícita, conforme
[README](../README.md#configuration).

## Provas de falha

| Falha | Prova |
|---|---|
| Escrita falha ou queda antes do envio | `test_intent_write_failure_blocks_exchange_dispatch`, `test_transition_write_failure_before_send_never_calls_exchange`, `test_crash_before_submission_clears_only_persisted_intent` |
| Timeout antes ou depois do aceite | `test_timeout_before_exchange_acceptance_never_resends_ambiguous_intent`, `test_timeout_after_acceptance_recovers_fill_without_second_submission` |
| Ordem incerta no reinício e fill tardio | `test_unknown_result_stays_pending_after_restart_and_blocks_duplicate`, `test_late_fill_after_reconnection_keeps_original_order_id` |
| Fill parcial e fill concorrente ao cancelamento | `test_partial_fill_cancels_remainder_before_protecting`, `test_fill_during_cancel_protects_final_quantity`, `test_restart_cancels_remaining_partial_entry_before_reprotecting` |
| Divergência entre intenção e exchange | `test_recovery_rejects_unrelated_position_when_entry_never_filled` |
| Fechamento incerto ou parcial | `test_uncertain_close_blocks_second_reduce_only_after_restart`, `test_partial_close_stays_pending_and_pauses_after_restart` |

Esses testes estão em [`test_entry_intent_recovery.py`](../tests/trading/test_entry_intent_recovery.py) e
[`test_close_intent_recovery.py`](../tests/trading/test_close_intent_recovery.py). Os cenários golden
`unknown_entry_result` e `close_with_protection` continuam estáveis.
