"""ATML TestDescription parser — entity handling.

The parser reads documents that arrive from outside: an ATML TestDescription is a
customer-authored test specification uploaded through the API. That makes it
untrusted input, and it is parsed with ``defusedxml`` rather than the stdlib
parser for that reason.

What is asserted here is the rejection path, not the happy path — the happy path
is covered by the importer tests. A rejection test that only checked "it raised
something" would pass just as well against an unhandled traceback escaping the
function, so every case below asserts the specific type the caller maps to HTTP
400, and the positive control asserts the hardening did not break normal
documents.
"""

from __future__ import annotations

import pytest

from ate_cloud.services.atml_td_parser import (
    ATMLParseError,
    parse_test_description,
)

#: Smallest document the parser accepts. Carries a UUT identifier because
#: ``_product_code`` rejects a TestDescription without one.
MINIMAL = (
    '<?xml version="1.0"?>'
    "<TestDescription>"
    "<UUT><Identifier>PA601-D54A</Identifier></UUT>"
    "<TestRequirements/>"
    "</TestDescription>"
)


class TestPositiveControl:
    """The hardening must not reject documents it is supposed to accept."""

    def test_minimal_document_parses(self) -> None:
        parsed = parse_test_description(MINIMAL)
        assert parsed.product_code == "PA601-D54A"

    def test_namespaced_document_parses(self) -> None:
        """Namespaced and namespace-free documents both have to work.

        Worth its own case because defusedxml wraps the same stdlib parser: if
        the wrapping ever changed how namespaces surface, this is the assertion
        that would notice, and it is not covered by the minimal document.
        """
        namespaced = (
            '<?xml version="1.0"?>'
            '<TestDescription xmlns="urn:IEEE-1671:2010:TestDescription">'
            "<UUT><Identifier>PA601-D54A</Identifier></UUT>"
            "<TestRequirements/>"
            "</TestDescription>"
        )
        assert parse_test_description(namespaced).product_code == "PA601-D54A"

    def test_bytes_input_parses(self) -> None:
        assert parse_test_description(MINIMAL.encode()).product_code == "PA601-D54A"


class TestEntityRejection:
    """A document carrying a DTD is refused, and refused as a controlled error.

    Each case must surface :class:`ATMLParseError` specifically. defusedxml's
    ``DTDForbidden`` and ``EntitiesForbidden`` derive from ``DefusedXmlException``
    and *not* from ``ParseError``, so without an explicit clause they would
    escape the parser uncaught and reach the API as a 500 with a traceback
    instead of the documented 400.
    """

    def test_dtd_with_internal_entity(self) -> None:
        doc = (
            "<!DOCTYPE TestDescription [<!ENTITY x 'y'>]>"
            "<TestDescription><UUT><Identifier>&x;</Identifier></UUT>"
            "<TestRequirements/></TestDescription>"
        )
        with pytest.raises(ATMLParseError):
            parse_test_description(doc)

    def test_external_entity_file_read(self) -> None:
        """The classic XXE: an entity that resolves to a local file."""
        doc = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE t [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
            "<TestDescription><UUT><Identifier>&e;</Identifier></UUT>"
            "<TestRequirements/></TestDescription>"
        )
        with pytest.raises(ATMLParseError):
            parse_test_description(doc)

    def test_nested_entity_expansion(self) -> None:
        """Billion laughs.

        The stdlib parser refuses undefined entities, so the classic
        file-disclosure case was already closed — but it still expands internal
        entity definitions, so this document parses and consumes the process's
        memory instead of failing. Rejecting the DTD is what closes it.
        """
        doc = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE t ["
            '<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            "]>"
            "<TestDescription><UUT><Identifier>&b;</Identifier></UUT>"
            "<TestRequirements/></TestDescription>"
        )
        with pytest.raises(ATMLParseError):
            parse_test_description(doc)

    def test_rejection_message_says_why(self) -> None:
        """The error has to name the cause, or an operator reads "malformed".

        A DTD is well-formed XML. Reporting it as malformed would send whoever
        reads the log looking for a truncation or an encoding problem that is not
        there.
        """
        doc = "<!DOCTYPE t [<!ENTITY x 'y'>]>" + MINIMAL.split("?>", 1)[1]
        with pytest.raises(ATMLParseError, match="entities or a DTD"):
            parse_test_description(doc)

    def test_malformed_xml_still_reports_malformed(self) -> None:
        """The pre-existing error path is unchanged, and still distinguishable."""
        with pytest.raises(ATMLParseError, match="[Mm]alformed"):
            parse_test_description("<TestDescription><UUT>")
