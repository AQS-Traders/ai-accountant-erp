"""
Document extractor tests — stubbed vision models, no network, no real keys.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.document_extractor import (
    DocumentValidationError,
    _parse_extraction_json,
    extract_attachments,
    extract_document,
    extraction_to_context_text,
    validate_attachment,
)
from app.models.schemas import AttachmentRef
from app.qwen_client import ProviderError


def _att(name="invoice.png", mime="image/png", data=None):
    return AttachmentRef(
        file_name=name,
        mime_type=mime,
        data_base64=data if data is not None
        else base64.b64encode(b"\x89PNG fakebytes").decode(),
    )


def _stub_orch(models_behaviour):
    """Orchestrator stub whose vision chain is the given model->fn map."""
    class _O:
        def __init__(self):
            self.stubs = {
                m: type("S", (), {"chat_raw": None, "calls": 0})()
                for m in models_behaviour
            }
            for m, fn in models_behaviour.items():
                self.stubs[m].chat_raw = fn

        def _candidate_providers(self, *, requires_vision: bool = False):
            assert requires_vision is True
            return [
                {"provider": "qwen", "model": m, "capability": "vision",
                 "factory": (lambda m=m: self.stubs[m])}
                for m in models_behaviour
            ]

    return _O()


# --- validation ---------------------------------------------------------

def test_validate_rejects_missing_content():
    bare = AttachmentRef(file_name="invoice.png", mime_type="image/png")
    with pytest.raises(DocumentValidationError, match="no uploaded content"):
        validate_attachment(bare)


def test_validate_rejects_disallowed_mime():
    with pytest.raises(DocumentValidationError, match="Only PNG"):
        validate_attachment(_att(mime="application/pdf"))


def test_validate_rejects_corrupt_base64():
    with pytest.raises(DocumentValidationError, match="could not be decoded"):
        validate_attachment(_att(data="!!!not-base64!!!"))


def test_validate_rejects_oversize():
    big = base64.b64encode(b"x" * (9 * 1024 * 1024)).decode()
    with pytest.raises(DocumentValidationError, match="larger than"):
        validate_attachment(_att(data=big))


def test_validate_accepts_valid_image():
    uri = validate_attachment(_att())
    assert uri.startswith("data:image/png;base64,")


# --- JSON contract parsing ----------------------------------------------

def test_parse_plain_json():
    assert _parse_extraction_json('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    assert _parse_extraction_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_prose_prefixed_json():
    assert _parse_extraction_json('Here is the data:\n{"a": 2}') == {"a": 2}


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        _parse_extraction_json("no json here at all")


# --- context rendering ---------------------------------------------------

def test_context_text_renders_fields_and_uncertainties():
    data = {
        "document_type": "invoice",
        "supplier": {"name": "ABC Computers"},
        "invoice_number": "INV-1023",
        "document_date": "2026-09-02",
        "total": 85000,
        "line_items": [
            {"description": "HP Laptop", "quantity": 1, "amount": 85000}
        ],
        "uncertainties": ["tax amount not readable"],
        "confidence": 0.9,
    }
    text = extraction_to_context_text(data)
    assert "Supplier: ABC Computers" in text
    assert "Document number: INV-1023" in text
    assert "Total: 85000" in text
    assert "HP Laptop" in text
    assert "tax amount not readable" in text
    assert "0.9" in text


# --- vision-chain fallback ------------------------------------------------

@pytest.mark.asyncio
async def test_extract_document_skips_model_with_unusable_reply():
    async def bad_json(**kwargs):
        return "I could not read this document."

    async def good_json(**kwargs):
        return json.dumps({
            "document_type": "invoice",
            "supplier": {"name": "XYZ"},
            "total": 85000,
            "confidence": 0.95,
            "uncertainties": [],
        })

    orch = _stub_orch({"qwen3-vl-plus": bad_json, "qwen3-vl-flash": good_json})
    data = await extract_document(
        orchestrator=orch, file_name="i.png", data_uri="data:image/png;base64,AAA"
    )
    assert data["_model"] == "qwen3-vl-flash"
    assert data["supplier"]["name"] == "XYZ"


@pytest.mark.asyncio
async def test_extract_document_all_models_fail():
    async def bad(**kwargs):
        return "garbage"

    orch = _stub_orch({"qwen3-vl-plus": bad, "qwen3-vl-flash": bad})
    with pytest.raises(ProviderError, match="All vision models failed"):
        await extract_document(
            orchestrator=orch, file_name="i.png",
            data_uri="data:image/png;base64,AAA",
        )


@pytest.mark.asyncio
async def test_extract_attachments_enforces_limit():
    orch = _stub_orch({})
    four = [_att(name=f"f{i}.png") for i in range(4)]
    with pytest.raises(DocumentValidationError, match="at most 3"):
        await extract_attachments(four, orchestrator=orch)
