"""IEEE 1671 TestDescription XML parser (ATML ingest side, task 11).

The codebase only *exported* IEEE 1636.1 TestResults (``atml_exporter.py``);
this module is the missing *import* side for IEEE 1671 TestDescription
documents. It turns a TestDescription XML document into plain dataclasses that
the importer service (``atml_importer.py``) maps to ORM rows.

Design notes:
- Uses stdlib ``xml.etree.ElementTree`` (consistent with the exporter — no new
  dependency).
- **Namespace-tolerant**: elements/attributes are matched by their *local
  name* (the part after ``{...}``), so a document with the real
  ``urn:IEEE-1671:2010:TestDescription`` / ``:Common`` namespaces and a
  namespace-free document both parse.
- **Defensive by contract**: malformed XML or a document whose root is not a
  ``TestDescription`` raises :class:`ATMLParseError` (a controlled error the
  route maps to HTTP 400) — never a raw ``ElementTree`` traceback.
- Optional ``DslMapping`` child on a ``Test`` carries an explicit
  sequence/step traceability hint (our extension; real 1671 documents omit it
  and those cases are simply reported as unmapped by the importer).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


class ATMLParseError(ValueError):
    """Raised when a document is not a parseable IEEE 1671 TestDescription.

    A subtype of :class:`ValueError` so callers can catch it specifically; the
    API layer translates it to HTTP 400.
    """


@dataclass(frozen=True, slots=True)
class ParsedRequirement:
    """A TestRequirement extracted from the document."""

    code: str
    title: str
    description: str | None
    atml_ref: str


@dataclass(frozen=True, slots=True)
class ParsedCase:
    """A test case (1671 ``Test``) extracted from the document.

    ``sequence_id`` / ``step_id`` are the *raw DSL mapping hints* from an
    optional ``DslMapping`` element; the importer validates them against the
    real DSL sequences before linking (``sequence_id`` is ``None`` when the
    document gives no hint).
    """

    case_code: str
    title: str
    requirement_code: str | None
    sequence_id: str | None
    step_id: str
    atml_ref: str


@dataclass(frozen=True, slots=True)
class ParsedTestDescription:
    """The parsed, normalized content of one TestDescription document."""

    product_code: str
    requirements: list[ParsedRequirement] = field(default_factory=list)
    cases: list[ParsedCase] = field(default_factory=list)


def _local(tag: str) -> str:
    """Return the local name of an XML tag/attribute (strip ``{ns}``)."""
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    """All direct children whose local name equals ``name`` (case-insensitive)."""
    target = name.lower()
    return [c for c in node if _local(c.tag).lower() == target]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    """First direct child whose local name equals ``name``."""
    kids = _children(node, name)
    return kids[0] if kids else None


def _attr(node: ET.Element, *names: str) -> str | None:
    """First attribute whose local name matches one of ``names`` (ns-tolerant)."""
    wanted = {n.lower() for n in names}
    for key, value in node.attrib.items():
        if _local(key).lower() in wanted:
            return value.strip() or None
    return None


def _text(node: ET.Element | None) -> str | None:
    """Trimmed element text, or ``None`` when blank/absent."""
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _product_code(root: ET.Element) -> str:
    """Extract the UUT/product identifier from the ``UUT`` subtree.

    Looks for an ``Identifier`` (1671 Common) then a ``Name`` element under
    ``UUT``; raises :class:`ATMLParseError` if none is present because
    ``TestRequirement.product_code`` is mandatory.
    """
    uut = _child(root, "UUT")
    if uut is not None:
        for label in ("Identifier", "Name", "PartNumber", "partNumber"):
            code = _text(_child(uut, label))
            if code:
                return code
    # Fallback: an identifier attribute directly on the root.
    code = _attr(root, "id", "name")
    if code:
        return code
    raise ATMLParseError(
        "TestDescription is missing a UUT product identifier "
        "(expected UUT/Identifier or UUT/Name)"
    )


def _parse_requirement(node: ET.Element) -> ParsedRequirement | None:
    """Parse a ``TestRequirement`` element; returns ``None`` if it has no id."""
    code = _attr(node, "id", "identifier")
    if not code:
        return None
    title = _attr(node, "name", "title") or code
    description = _text(_child(node, "Description")) or _text(_child(node, "Text"))
    return ParsedRequirement(
        code=code,
        title=title,
        description=description,
        atml_ref=f"TestRequirement#{code}",
    )


def _parse_case(node: ET.Element) -> ParsedCase | None:
    """Parse a ``Test`` element (a test case); returns ``None`` if no id."""
    code = _attr(node, "id", "identifier")
    if not code:
        return None
    title = _attr(node, "name", "title") or code
    requirement_code = _attr(node, "requirementId", "requirementID", "requirementIdRef")

    mapping = _child(node, "DslMapping")
    sequence_id = _attr(mapping, "sequenceId", "sequenceID") if mapping is not None else None
    step_id = _attr(mapping, "stepId", "stepID") if mapping is not None else ""
    step_id = step_id or ""

    return ParsedCase(
        case_code=code,
        title=title,
        requirement_code=requirement_code,
        sequence_id=sequence_id,
        step_id=step_id,
        atml_ref=f"Test#{code}",
    )


def parse_test_description(xml: str | bytes) -> ParsedTestDescription:
    """Parse an IEEE 1671 TestDescription XML document.

    Args:
        xml: The XML document as text or bytes.

    Returns:
        A :class:`ParsedTestDescription` with product code, requirements and
        test cases.

    Raises:
        ATMLParseError: If the document is not well-formed XML, is empty, or
            its root element is not a ``TestDescription``.
    """
    if xml is None or not str(xml).strip():
        raise ATMLParseError("Empty TestDescription document")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ATMLParseError(f"Malformed TestDescription XML: {exc}") from exc

    if _local(root.tag).lower() != "testdescription":
        raise ATMLParseError(
            f"Expected a <TestDescription> root element, got <{_local(root.tag)}>"
        )

    product_code = _product_code(root)

    requirements: list[ParsedRequirement] = []
    cases: list[ParsedCase] = []
    for el in root.iter():
        local = _local(el.tag).lower()
        if local == "testrequirement":
            parsed_req = _parse_requirement(el)
            if parsed_req is not None:
                requirements.append(parsed_req)
        elif local == "test":
            parsed_case = _parse_case(el)
            if parsed_case is not None:
                cases.append(parsed_case)

    return ParsedTestDescription(
        product_code=product_code,
        requirements=requirements,
        cases=cases,
    )


__all__ = [
    "ATMLParseError",
    "ParsedCase",
    "ParsedRequirement",
    "ParsedTestDescription",
    "parse_test_description",
]
