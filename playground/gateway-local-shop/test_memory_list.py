"""Provenance labels on /memory/list (security finding F6).

Lives in the playground, not under tests/: it asserts things about *this demo's*
HTTP surface, and tests/ is for the SDK. A bare `pytest` will not collect it
(testpaths = ["tests"]). Run it by path:

    pytest playground/gateway-local-shop/test_memory_list.py

Note the naming split in this directory: `*_test.py` files here are runnable
demo scripts that want live servers (context_test.py, e2e_test.py), while a
`test_*.py` prefix means pytest, as in the clinic's test_server_trust.py.

Why the endpoint needs the field at all: a memory row now records the taint of
the run that wrote it, so a row derived from an injected tool result is
distinguishable from one the user actually stated. That distinction is invisible
in the row's text -- both are just sentences -- so a reviewer deleting poisoned
memory has no way to tell them apart unless the listing surfaces it.

No servers and no agent: the endpoint is exercised over a stubbed memory client,
so this is about the response shape only.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

PROVENANCE_KEY = "_data_labels"


def _entry(mem_id, text, metadata=None):
    """A stand-in for MemoryEntry with only the fields the endpoint reads."""
    return SimpleNamespace(id=mem_id, memory=text, metadata=metadata)


@pytest.fixture
def listing(monkeypatch):
    """Call /memory/list over a stubbed memory client, returning the payload."""
    import web

    async def _call(entries):
        client = SimpleNamespace(get_all=AsyncMock(return_value=entries), is_enabled=True)
        monkeypatch.setattr(web, "_get_memory_client", lambda: client)
        return await web.list_memories(user_id="u1")

    return _call


class TestListingSurfacesProvenance:
    async def test_tainted_row_reports_its_labels(self, listing):
        data = await listing(
            [_entry("id-1", "refund limit is $10,000", {PROVENANCE_KEY: ["external"]})]
        )

        assert data["success"] is True
        assert data["memories"][0]["labels"] == ["external"]

    async def test_clean_row_reports_no_labels(self, listing):
        """None rather than [] -- a row written before provenance existed is
        genuinely unlabelled, which is not the same as labelled with nothing."""
        data = await listing([_entry("id-2", "prefers morning appointments", None)])

        assert data["memories"][0]["labels"] is None

    async def test_existing_fields_are_unchanged(self, listing):
        """The UI reads id and text; adding a field must not disturb them."""
        data = await listing([_entry("id-3", "name is Tom", None)])

        assert data["memories"][0]["id"] == "id-3"
        assert data["memories"][0]["text"] == "name is Tom"

    async def test_mixed_listing_distinguishes_the_rows(self, listing):
        """The whole point: one call, and the reviewer can see which is which."""
        data = await listing(
            [
                _entry("id-clean", "name is Tom", None),
                _entry("id-dirty", "refund limit is $10,000", {PROVENANCE_KEY: ["external"]}),
            ]
        )

        by_id = {m["id"]: m["labels"] for m in data["memories"]}
        assert by_id == {"id-clean": None, "id-dirty": ["external"]}


class TestListingToleratesOddMetadata:
    """Row metadata round-trips through a vector store as JSON, so the endpoint
    takes the same tolerant line as the SDK's reader: a malformed stamp is a data
    problem, not a reason to fail the listing and block a cleanup."""

    async def test_metadata_missing_entirely(self, listing):
        data = await listing([_entry("id-4", "text", {})])
        assert data["memories"][0]["labels"] is None

    async def test_metadata_is_not_a_dict(self, listing):
        for bad in ("external", 42, ["external"]):
            data = await listing([_entry("id-5", "text", bad)])
            assert data["success"] is True
            assert data["memories"][0]["labels"] is None

    async def test_other_metadata_keys_are_not_leaked(self, listing):
        """Only provenance is surfaced. The rest of a row's metadata carries
        session and user ids that this listing has no reason to expose."""
        data = await listing(
            [
                _entry(
                    "id-6", "text", {"_user_id": "u1", "session_id": "s1", PROVENANCE_KEY: ["pii"]}
                )
            ]
        )

        assert data["memories"][0] == {"id": "id-6", "text": "text", "labels": ["pii"]}
