"""Fixtures de utilidade para testes do pacote utils."""

import logging

import pytest


@pytest.fixture
def isolated_logging(monkeypatch):
    """Salva e restaura completamente o estado global do logging entre testes.

    Cobre: handlers e filtros do root logger, nível, filtros de handlers.
    Previne que um teste que instale LogPrivacyFilter afete os seguintes.
    """
    root = logging.getLogger()

    original_level = root.level
    original_handlers = list(root.handlers)
    original_filters = list(root.filters)
    handler_filters_snapshot = {h: list(h.filters) for h in original_handlers}

    yield

    # Fechar e remover handlers adicionados durante o teste.
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    root.handlers[:] = original_handlers
    root.filters[:] = original_filters
    root.setLevel(original_level)

    # Restaurar filtros dos handlers originais.
    for handler, filters in handler_filters_snapshot.items():
        handler.filters[:] = filters
