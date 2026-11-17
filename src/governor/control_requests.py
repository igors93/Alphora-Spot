from collections.abc import Iterable
from pathlib import Path
from typing import Any


class GovernorControlRequests:
    def __init__(self, runtime_dir: str | Path, events_journal: Any):
        self.runtime_dir = Path(runtime_dir)
        self.events_journal = events_journal

    def consume_governor_stop(self) -> bool:
        path = self.runtime_dir / "governor_stop.request"

        if not path.exists():
            return False

        path.unlink(missing_ok=True)
        self.events_journal.append("governor_stop_requested", path=str(path))
        return True

    def consume_symbol_requests(
        self,
        symbol_configs: Iterable[Any],
        paused_symbols: set[str],
        stopped_symbols: set[str],
    ) -> None:
        for symbol_config in symbol_configs:
            symbol = symbol_config.symbol
            suffix = symbol.lower()

            # Apply requests in the order the user issued them (file mtime).
            # Cycles can take several seconds, so two request files may
            # coexist; a fixed pause->resume->stop order would let an older
            # resume override a newer pause and leave the symbol running.
            pending: list[tuple[float, str, Path]] = []
            for action in ("pause", "resume", "stop"):
                path = self.runtime_dir / f"{action}_{suffix}.request"
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                pending.append((mtime, action, path))

            for _mtime, action, path in sorted(pending):
                path.unlink(missing_ok=True)
                if action == "pause":
                    paused_symbols.add(symbol)
                    self.events_journal.append("symbol_paused", symbol=symbol)
                elif action == "resume":
                    paused_symbols.discard(symbol)
                    stopped_symbols.discard(symbol)
                    self.events_journal.append("symbol_resumed", symbol=symbol)
                else:
                    stopped_symbols.add(symbol)
                    self.events_journal.append("symbol_stopped", symbol=symbol)
