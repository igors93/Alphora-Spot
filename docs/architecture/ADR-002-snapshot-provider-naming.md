# ADR-002 — Nome do provider de snapshots e wire names herdados

- Estado: aceito
- Data: 13/11/2026
- Contexto: o mecanismo de snapshots foi desenvolvido fora do Alphora com o nome “Coalestra” e depois internalizado
  como `market_data/snapshot_engine`. O nome sobreviveu no provider (`governor/coalestra_provider.py`, 3.526 linhas,
  classes `AlphoraCoalestra*`), nos campos do `CycleContext`, nos argumentos do `MarketDataRouter`, em logs e em
  ~640 ocorrências em `src/`. Parte dessas ocorrências não é código: são valores persistidos — tipos de evento do
  journal e do audit log, códigos de motivo e de estágio exibidos no `governor_state.json`, chaves de payload
  (`"coalestra"` nas métricas de ciclo e nos registros de erro), ids de schema do engine
  (`coalestra.error-diagnostics`) e rótulos de origem. A interface desktop (`interface/src/logs.rs`) e o histórico de
  runs leem esses valores.
- Problema a resolver: renomear o código sem quebrar quem lê os arquivos já gravados e sem esconder onde o nome antigo
  continua de propósito.
- Alternativas consideradas:
  1. renomear tudo, inclusive os valores persistidos — quebra a interface e a leitura do histórico;
  2. manter aliases dos nomes antigos de classes e módulos até o fim da fase — prolonga o nome antigo no código e
     esconde chamadores esquecidos;
  3. renomear o código e isolar os valores persistidos em módulos marcados, contados pela catraca.
- Decisão: alternativa 3.
  - Código (módulos, classes, funções, atributos, argumentos, campos do `CycleContext`, nomes de logger e de thread,
    rótulos `component` da observabilidade e textos de log) usa o nome do conceito: `SnapshotProvider`,
    `SnapshotCycleSession`, `SnapshotReadiness`, `snapshot_session`, `snapshot_provider`. Sem aliases: todos os
    chamadores foram atualizados no mesmo commit.
  - Valores persistidos ficam com a grafia antiga até o schema versionado de eventos da M7, e só podem ser escritos a
    partir de `market_data/snapshot_provider/wire_names.py` (provider) e
    `market_data/snapshot_engine/core/wire_names.py` (engine). As duas docstrings levam o marcador
    `legacy-wire-names (ADR-002)`; uma linha isolada pode levar `legacy-wire-name (ADR-002)`.
  - `tools/architecture_metrics.py` conta as ocorrências fora dos módulos e linhas marcados
    (`legacy_name_refs`), e a catraca de `tests/architecture/baseline.json` impede que voltem a crescer.
  - Variáveis de ambiente: os nomes novos `ALPHORA_SNAPSHOT_*` passam a ser os documentados; os antigos
    `ALPHORA_COALESTRA_*` continuam aceitos, marcados, para não quebrar `.env` existentes.
- Consequências positivas: o código fala a língua do domínio; o que ainda carrega o nome antigo é uma lista curta,
  explícita e verificada no CI; nenhum arquivo em disco ou tela da interface muda.
- Consequências negativas / trade-offs: até a M7, quem lê o journal vê `coalestra_*` em eventos emitidos por classes
  chamadas `Snapshot*`. O mapeamento está em `wire_names.py`, um arquivo por camada.
- Invariantes afetados: nenhum. Tipos de evento, códigos, chaves e schemas persistidos são byte a byte os mesmos; os
  14 cenários do golden replay não mudam.
- Plano de migração: M5 move e divide o provider, troca os nomes de código e roteia todo valor persistido por
  `wire_names`. A M7 versiona o schema de eventos e, com ele, troca os valores (com leitura dos dois formatos na
  interface).
- Como validar: `python tools/architecture_metrics.py` (referências fora de wire names marcados); golden replay;
  `rg -i coalestra src/` mostra apenas `wire_names.py` e as linhas marcadas.
- Links para evidências: `docs/MIGRATION_PLAN.md` (M5), `src/market_data/snapshot_provider/wire_names.py`,
  `tests/architecture/test_architecture_ratchet.py`.
