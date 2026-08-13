"""Durable page checkpoints for long catalogue scraping runs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pricing_scraper.models import Product

LOGGER = logging.getLogger("pricing_scraper.checkpoint")


def _safe_component(value: Any, *, max_length: int = 80) -> str:
    """Create a Windows-safe, bounded checkpoint filename component."""
    original = str(value)
    safe = "".join(
        character if character.isalnum() else "_"
        for character in original.casefold()
    ).strip("_")
    if len(safe) <= max_length:
        return safe
    digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:12]
    prefix_length = max(1, max_length - len(digest) - 1)
    return f"{safe[:prefix_length].rstrip('_')}_{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_text_durable(path: Path, text: str) -> None:
    """Append one complete UTF-8 payload and flush it to disk."""
    payload = text.encode("utf-8")
    with path.open("ab", buffering=0) as handle:
        written = handle.write(payload)
        if written != len(payload):
            raise OSError(
                f"Incomplete checkpoint append to {path}: "
                f"{written}/{len(payload)} bytes"
            )
        os.fsync(handle.fileno())


def _replace_text_durable(path: Path, text: str) -> None:
    """Atomically replace a small state file, flushed to disk before renaming.

    Writing the temporary file and renaming it is only half of an atomic
    replace. Without an fsync the rename can reach the disk while the contents
    are still in the operating system's cache, and a crash or power loss then
    leaves a file of the right length filled with NUL bytes - which is exactly
    how a checkpoint becomes unreadable.
    """
    temporary = path.with_name(f"{path.name}.tmp")
    payload = text.encode("utf-8")
    with temporary.open("wb", buffering=0) as handle:
        handle.write(payload)
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _quarantine(path: Path, reason: str, label: str) -> None:
    """Move an unusable checkpoint file aside so the next run can rebuild it.

    The damaged file is kept rather than deleted: it is the only evidence of
    what went wrong, and the rebuilt state is derived from the append-only
    files next to it.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    damaged = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, damaged)
    except OSError as exc:
        LOGGER.error(
            "checkpoint_quarantine_failed label=%s path=%s error=%s",
            label,
            path,
            exc,
        )
        return
    LOGGER.warning(
        "checkpoint_state_rebuilt label=%s path=%s reason=%s moved_to=%s",
        label,
        path,
        reason,
        damaged.name,
    )


def _load_jsonl_products(path: Path, label: str) -> list[Product]:
    """Load valid product rows while isolating interrupted final writes."""
    if not path.exists():
        return []
    products: list[Product] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
                if not isinstance(payload, Mapping):
                    raise TypeError("record is not a JSON object")
                products.append(Product(**dict(payload)))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                LOGGER.warning(
                    "checkpoint_record_skipped label=%s path=%s line=%s "
                    "error=%s",
                    label,
                    path,
                    line_number,
                    exc,
                )
    return products


@dataclass(frozen=True, slots=True)
class CheckpointState:
    """Persistent pagination state for one site/category pair."""

    next_page: int
    completed: bool = False
    last_page: int | None = None
    products_saved: int = 0
    updated_at: str = ""
    completed_at: str = ""


class CheckpointStore:
    """Append normalized page data and atomically persist pagination state."""

    def __init__(
        self,
        directory: Path,
        *,
        site: str,
        category_id: str,
        start_page: int,
    ) -> None:
        safe_site = _safe_component(site, max_length=30)
        safe_category = _safe_component(category_id)
        if not safe_site or not safe_category:
            raise ValueError("Checkpoint site and category ID are required.")

        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = f"{safe_site}_{safe_category}"
        self.products_path = self.directory / f"{stem}.products.jsonl"
        self.state_path = self.directory / f"{stem}.state.json"
        self.start_page = max(1, int(start_page))

    def load_state(self) -> CheckpointState:
        """Load state, returning a fresh checkpoint when no state exists.

        A state file damaged by an interrupted write is quarantined and the
        category restarts at its first page. The saved pages themselves live in
        the append-only products file, so nothing collected is lost: already
        seen product IDs are passed back to the client and the export
        deduplicates. Refusing to run instead would leave the retailer
        permanently unscrapeable until somebody deleted the file by hand.
        """
        if not self.state_path.exists():
            return CheckpointState(next_page=self.start_page)
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _quarantine(self.state_path, str(exc), "listing")
            return CheckpointState(next_page=self.start_page)
        if not isinstance(payload, Mapping):
            _quarantine(
                self.state_path, "state is not a JSON object", "listing"
            )
            return CheckpointState(next_page=self.start_page)
        try:
            return self._state_from(payload)
        except (TypeError, ValueError) as exc:
            _quarantine(self.state_path, str(exc), "listing")
            return CheckpointState(next_page=self.start_page)

    def _state_from(self, payload: Mapping[str, Any]) -> CheckpointState:
        return CheckpointState(
            next_page=max(
                self.start_page,
                int(payload.get("next_page", self.start_page)),
            ),
            completed=bool(payload.get("completed", False)),
            last_page=(
                int(payload["last_page"])
                if payload.get("last_page") is not None
                else None
            ),
            products_saved=max(0, int(payload.get("products_saved", 0))),
            updated_at=str(payload.get("updated_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
        )

    def load_products(self) -> list[Product]:
        """Load valid normalized records and skip interrupted writes."""
        return _load_jsonl_products(self.products_path, "listing")

    def reset(self) -> None:
        """Start a new run by removing this category's two checkpoint files."""
        self.products_path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)

    def append_page(
        self,
        page: int,
        products: Sequence[Product],
    ) -> CheckpointState:
        """Append a successful page and advance the durable next-page pointer."""
        current = self.load_state()
        if products:
            payload = "".join(
                json.dumps(
                    product.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
                for product in products
            )
            _append_text_durable(self.products_path, payload)

        state = CheckpointState(
            next_page=int(page) + 1,
            completed=False,
            last_page=int(page),
            products_saved=current.products_saved + len(products),
            updated_at=_utc_now(),
        )
        self._write_state(state)
        return state

    def mark_complete(
        self,
        *,
        empty_page: int,
        products: Iterable[Product],
    ) -> CheckpointState:
        """Persist true completion after the API returns an empty page."""
        now = _utc_now()
        state = CheckpointState(
            next_page=max(self.start_page, int(empty_page)),
            completed=True,
            last_page=max(self.start_page, int(empty_page) - 1),
            products_saved=sum(1 for _ in products),
            updated_at=now,
            completed_at=now,
        )
        self._write_state(state)
        return state

    def _write_state(self, state: CheckpointState) -> None:
        _replace_text_durable(
            self.state_path,
            json.dumps(asdict(state), indent=2, ensure_ascii=False),
        )


@dataclass(frozen=True, slots=True)
class DetailCheckpointState:
    """Persistent product-detail enrichment state."""

    completed: bool = False
    parents_processed: int = 0
    products_saved: int = 0
    updated_at: str = ""
    completed_at: str = ""


class DetailCheckpointStore:
    """Persist enriched SKU rows and completed parent-product IDs."""

    def __init__(
        self,
        directory: Path,
        *,
        site: str,
        category_id: str,
    ) -> None:
        safe_site = _safe_component(site, max_length=30)
        safe_category = _safe_component(category_id)
        if not safe_site or not safe_category:
            raise ValueError("Detail checkpoint site and category ID are required.")
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = f"{safe_site}_{safe_category}.details"
        self.products_path = self.directory / f"{stem}.products.jsonl"
        self.processed_path = self.directory / f"{stem}.processed.txt"
        self.state_path = self.directory / f"{stem}.state.json"

    def load_state(self) -> DetailCheckpointState:
        """Load the enrichment state, rebuilding it when the file is damaged.

        This state is pure bookkeeping: the parents already enriched are listed
        in the append-only processed file and their rows in the products file.
        A state file lost to an interrupted write is therefore quarantined and
        recomputed from those two, so a run resumes exactly where it stopped
        instead of failing before it starts.
        """
        if not self.state_path.exists():
            return DetailCheckpointState()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _quarantine(self.state_path, str(exc), "details")
            return self._rebuilt_state()
        if not isinstance(payload, Mapping):
            _quarantine(
                self.state_path, "state is not a JSON object", "details"
            )
            return self._rebuilt_state()
        try:
            return self._state_from(payload)
        except (TypeError, ValueError) as exc:
            _quarantine(self.state_path, str(exc), "details")
            return self._rebuilt_state()

    def _rebuilt_state(self) -> DetailCheckpointState:
        """Recompute enrichment counters from the append-only checkpoint files.

        ``completed`` is deliberately left false: the products file cannot say
        whether every discovered parent was reached, so the next run rechecks
        and marks completion itself.
        """
        state = DetailCheckpointState(
            completed=False,
            parents_processed=len(self.load_processed_ids()),
            products_saved=len(self.load_products()),
            updated_at=_utc_now(),
        )
        self._write_state(state)
        return state

    def _state_from(self, payload: Mapping[str, Any]) -> DetailCheckpointState:
        return DetailCheckpointState(
            completed=bool(payload.get("completed", False)),
            parents_processed=max(
                0,
                int(payload.get("parents_processed", 0)),
            ),
            products_saved=max(0, int(payload.get("products_saved", 0))),
            updated_at=str(payload.get("updated_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
        )

    def load_processed_ids(self) -> set[str]:
        """Return parent IDs already committed to the enrichment checkpoint."""
        if not self.processed_path.exists():
            return set()
        return {
            line.strip()
            for line in self.processed_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
            and all(ord(character) >= 32 for character in line.strip())
        }

    def load_products(self) -> list[Product]:
        """Load enriched SKU rows, skipping interrupted writes."""
        return _load_jsonl_products(self.products_path, "details")

    def reset(self) -> None:
        """Remove only this category's detail checkpoint files."""
        self.products_path.unlink(missing_ok=True)
        self.processed_path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)

    def append_parent(
        self,
        parent_id: str,
        products: Sequence[Product],
    ) -> DetailCheckpointState:
        """Commit all SKU rows for one successfully enriched parent."""
        identifier = str(parent_id).strip()
        if not identifier:
            raise ValueError("A parent product ID is required.")
        current = self.load_state()
        if products:
            payload = "".join(
                json.dumps(
                    product.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
                for product in products
            )
            _append_text_durable(self.products_path, payload)
        _append_text_durable(self.processed_path, f"{identifier}\n")
        state = DetailCheckpointState(
            completed=False,
            parents_processed=current.parents_processed + 1,
            products_saved=current.products_saved + len(products),
            updated_at=_utc_now(),
        )
        self._write_state(state)
        return state

    def mark_complete(self) -> DetailCheckpointState:
        """Mark enrichment complete after every discovered parent succeeded."""
        current = self.load_state()
        now = _utc_now()
        state = DetailCheckpointState(
            completed=True,
            parents_processed=current.parents_processed,
            products_saved=current.products_saved,
            updated_at=now,
            completed_at=now,
        )
        self._write_state(state)
        return state

    def _write_state(self, state: DetailCheckpointState) -> None:
        _replace_text_durable(
            self.state_path,
            json.dumps(asdict(state), indent=2, ensure_ascii=False),
        )
