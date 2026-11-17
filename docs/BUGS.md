# Registro de bugs

Este arquivo reúne somente defeitos conhecidos. Cada correção deve registrar a
reprodução, o teste de regressão e o commit que a resolveu. Os detalhes de
BUG-01 a BUG-17 estão no
[roadmap de prontidão para mercado real](REAL_MARKET_READINESS_ROADMAP.md).

| ID | Prioridade | Defeito | Estado atual |
|---|---|---|---|
| BUG-01 a BUG-14 | P0–P2 | Histórico de defeitos corrigidos e respectivas evidências no roadmap. | Resolvidos antes da M0 |
| BUG-15 | P0 | Estado e journal compartilhados entre TESTNET_LIVE, REAL_LIVE e DRY_RUN sem perfil de símbolo. | Corrigido em `config.state_paths`; regressão em `tests/config/test_state_paths.py` |
| BUG-16 | P1 | `operational_stop --all` usa resolução de símbolos diferente da usada pelo Governor. | Corrigido na árvore de trabalho; commit pendente |
| BUG-17 | P0 | Emergency flatten deixa ordens de entrada do robô abertas quando o símbolo já está flat. | Corrigido na árvore de trabalho; commit pendente |
| BUG-20 | P0 | `positionRisk` inválido podia ser interpretado como posição zerada no cancelamento de ordens. | Corrigido na árvore de trabalho; commit pendente |
| BUG-21 | P0 | Stop operacional podia parar o robô com ordem de entrada ainda aberta num símbolo flat. | Corrigido na árvore de trabalho; commit pendente |
| BUG-22 | P0 | Stop operacional considerava proteção válida só pela presença dos tipos STOP/TAKE. | Corrigido na árvore de trabalho; commit pendente |

### BUG-16 — Resolução incompleta de `operational_stop --all`

- **Reprodução:** um arquivo de perfis só com entradas desabilitadas causava
  `ValueError`; posições fora dos perfis não eram avaliadas.
- **Correção:** o stop usa `_all_symbol_targets`, que combina o resolvedor do
  Governor com posições e ordens da conta. Erros de varredura bloqueiam a
  solicitação de parada, em vez de produzir aprovação incompleta.
- **Regressão:** `test_operational_stop_all_uses_governor_fallback_when_profiles_disabled`,
  `test_operational_stop_all_checks_positions_outside_profiles` e
  `test_operational_stop_all_failed_account_scan_blocks_stop`.

### BUG-17 — Ordens órfãs após emergency flatten

- **Reprodução:** posição zerada com ordem `ALPHORA_ENTRY_1` aberta retornava
  `Sem posicao aberta` sem solicitar cancelamento.
- **Correção:** o fluxo confirmado cancela ordens normais e algo do robô mesmo
  quando a posição já está zerada. `--all` inclui símbolos fora dos perfis que
  tenham somente ordens do robô. Cancelamento recusado ou estado flat não
  confirmado gera `manual_reconcile_required` e resultado bloqueado.
- **Regressão:** `test_emergency_flatten_cancels_normal_and_algo_orders_when_already_flat`,
  `test_emergency_flatten_all_includes_flat_symbol_with_robot_order` e
  `test_emergency_flatten_reports_failed_cleanup_when_flat`.

### BUG-20 — Snapshot inválido da posição tratado como flat

- **Reprodução:** com `positionAmt="NaN"` ou inválido e ordens de proteção do
  robô, `_has_open_position` ignorava a linha e permitia cancelá-las.
- **Correção:** a leitura compartilhada de pernas recusa resposta vazia,
  `positionAmt` ausente, inválido ou não finito. O cancelamento responde
  `blocked` sem mutação; flatten também recusa o snapshot, e o stop bloqueia
  o símbolo afetado sem deixar de avaliar os demais em `--all`.
- **Regressão:** `test_cancel_robot_orders_blocks_on_unknown_position_amount`,
  `test_emergency_flatten_refuses_invalid_position_snapshot` e
  `test_operational_stop_all_keeps_checking_after_one_symbol_fails`.

### BUG-21 — Stop com ordem de entrada pendente

- **Reprodução:** símbolo flat com `ALPHORA_ENTRY_1` em `openOrders` era
  aprovado por `SAFE_STOP` e `STOP_KEEP_PROTECTIONS`; a ordem podia preencher
  depois que o processo parasse.
- **Correção:** os dois modos bloqueiam o stop enquanto houver ordem normal do
  robô, ou ordem algo órfã num símbolo flat, com instrução para cancelamento
  explícito ou emergency flatten.
- **Regressão:** `test_operational_stop_blocks_flat_symbol_with_pending_robot_entry`.

### BUG-22 — Proteção não auditada no stop

- **Reprodução:** uma posição LONG com STOP_MARKET do lado BUY e um
  TAKE_PROFIT_MARKET era considerada protegida; `STOP_KEEP_PROTECTIONS`
  autorizava a parada.
- **Correção:** o stop usa a mesma auditoria do motor de execução, que valida
  lado, símbolo, `closePosition`, estado, tipo de preço e gatilhos esperados.
- **Regressão:** `test_operational_stop_blocks_wrong_side_protection_order`;
  os testes de proteção válida usam agora ordens com campos e gatilhos reais.

## Defeitos encontrados na M0

### BUG-18 — Filtro global de logging vazava entre testes

- **Sintoma:** a suíte completa falhava em
  `test_with_logging_explicit_includes_error_message`, mas o mesmo teste passava
  isoladamente. A mensagem esperada era `SECRET:traceable-secret`; após o teste
  de observabilidade, aparecia `SECRET:[SECRET]`.
- **Reprodução mínima:**
  `pytest tests/observability/test_text_logging_privacy.py tests/retry_engine/regression/test_safe_redaction_regressions.py::test_with_logging_explicit_includes_error_message -q`.
- **Causa:** a fixture de observabilidade restaurava os singletons, mas deixava
  filtros e handlers de privacidade instalados no logger raiz. Além disso, o
  teste de logging explícito pressupunha que nenhum filtro global de privacidade
  estaria ativo durante a suíte. A proteção de dados funcionava; o defeito era
  o isolamento entre testes.
- **Correção:** a fixture passou a restaurar handlers, filtros e nível do logger
  raiz após cada teste (`b1bfe3f`). O teste de logging explícito passou a usar
  um logger próprio, verificando a saída antes de qualquer filtro global
  (`4299eaf`).
- **Regressão:** a reprodução mínima, `pytest tests/observability -q` e o teste
  de logging explícito com a suíte completa.

### BUG-19 — Mypy do Linux rejeitava API de ctypes do Windows

- **Sintoma:** o job `Lint` no CI falhava com `Module has no attribute
  "windll" [attr-defined]` em `src/futures/process_lock.py`, linha 110.
- **Causa:** `ctypes.windll` só existe em Windows; o verificador em Linux não
  consegue inferir a guarda de execução feita por `os.name == "nt"`.
- **Correção:** a chamada Windows recebeu uma exceção pontual de tipagem,
  sem alterar o código executado. Commit `4299eaf`.
- **Regressão:** `mypy --platform linux src` e
  `mypy --platform win32 src` passam.

Nenhum defeito novo no código de produção foi reproduzido durante a M0. O fake
da exchange usado no replay foi estendido para confirmar fills e permitir a
comparação fiel das mutações; essa extensão não altera o código de produção.
