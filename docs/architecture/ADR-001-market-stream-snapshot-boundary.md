# ADR-001 — Fronteira entre o stream de preço e o Snapshot Engine

- Estado: aceito
- Data: 12/11/2026
- Contexto: `AlphoraCoalestraSnapshotProvider.publish_market_snapshot` permitia publicar o `bookTicker` do WebSocket diretamente no Snapshot Engine, mas não tinha chamador no runtime. Dois testes ligavam o callback manualmente.
- Problema a resolver: manter uma API inativa sugeria que o runtime usava o stream como fonte autoritativa do snapshot, embora o fluxo real continuasse usando as fontes configuradas no provider.
- Alternativas consideradas: ligar o callback no M2; manter o método sem chamador; remover o método e decidir a integração operacional na M5.
- Decisão: remover o método inativo no M2. A eventual publicação do stream será tratada na M5 como mudança explícita de comportamento, com política de frescor, autoridade, fallback e métricas próprias.
- Consequências positivas: a API passa a refletir o fluxo operacional real; o M2 continua sendo uma refatoração sem alteração de fonte de dados; desaparece uma falsa rota de integração.
- Consequências negativas / trade-offs: um experimento que queira publicar preços do WebSocket precisará de um adaptador dedicado ou aguardar a M5.
- Invariantes afetados: nenhum formato persistido ou evento operacional muda; o runtime não chamava o método removido.
- Plano de migração: remover o método, o conversor exclusivo e os testes que montavam a ligação manual. Na M5, reavaliar a integração a partir deste ADR.
- Como validar: busca de chamadores em `src/`, golden masters e suíte de dados/Coalestra inalterados.
- Links para evidências: `docs/MIGRATION_PLAN.md`, fases M2 e M5.
