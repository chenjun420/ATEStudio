"""A minimal, hand-authored CONFORMING sample data graph for SHACL validation.

Deterministic (no I/O, no LLM). Every required object/datatype property in the
domain ontology is satisfied exactly once, so SHACL validation passes; tests
also derive a violating graph by deleting a required relationship.

Turtle uses the ontology namespace (``ONTOLOGY_BASE_URI``) so Semantica's
generated shapes (``sh:targetClass`` / ``sh:path`` expanded in that namespace)
match the instance IRIs.
"""

from __future__ import annotations

from ate_cloud.services.ontology.core import ONTOLOGY_BASE_URI

_PREFIXES = f"""@prefix ex: <{ONTOLOGY_BASE_URI}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""

# One fully-linked instance of every entity, exercising the required edges:
# Product -> Requirement -> TestCase -> TestStep -> Instrument/Fixture/Station
# and TestStep -> UUTResult ; Fault Symptom -> Cause -> Solution ; FMEA coverage.
_CONFORMING = """
ex:product-1 a ex:Product ;
    ex:hasRequirement ex:req-1 ;
    ex:hasComponent ex:comp-1 .

ex:req-1 a ex:TestRequirement ;
    ex:identifier "REQ-1" ;
    ex:title "3V3 rail within tolerance" ;
    ex:standard "IEEE 1671 TestDescription" ;
    ex:verifiedBy ex:tc-1 .

ex:tc-1 a ex:TestCase ;
    ex:identifier "TC-1" ;
    ex:title "Measure 3V3 output voltage" ;
    ex:standard "IEEE 1671 TestDescription" ;
    ex:hasStep ex:step-1 ;
    ex:coversComponent ex:comp-1 ;
    ex:runsOnStation ex:station-1 .

ex:item-1 a ex:TestItem ;
    ex:identifier "ITEM-1" ;
    ex:title "Power rail tests" ;
    ex:hasStep ex:step-1 .

ex:step-1 a ex:TestStep ;
    ex:identifier "STEP-1" ;
    ex:title "DMM measure VOUT" ;
    ex:standard "IEEE 1671 TestDescription" ;
    ex:partOfItem ex:item-1 ;
    ex:usesInstrument ex:inst-1 ;
    ex:runsOnFixture ex:fixture-1 ;
    ex:runsOnStation ex:station-1 ;
    ex:producedResult ex:result-1 .

ex:inst-1 a ex:Instrument ;
    ex:instrumentKind "digital_multimeter" ;
    ex:mountedOnFixture ex:fixture-1 .

ex:fixture-1 a ex:Fixture .
ex:station-1 a ex:TestStation .
ex:comp-1 a ex:Component .

ex:result-1 a ex:UUTResult ;
    ex:standard "IEEE 1636.1 TestResults" ;
    ex:outcome "Passed" ;
    ex:measuredValue "3.30"^^xsd:decimal ;
    ex:resultFor ex:product-1 .

ex:fault-1 a ex:Fault ;
    ex:faultKind "over_voltage" ;
    ex:faultCategory "power" ;
    ex:hasSymptom ex:symptom-1 ;
    ex:affectsComponent ex:comp-1 ;
    ex:exhibits ex:comp-1 .

ex:symptom-1 a ex:Symptom ;
    ex:hasCause ex:cause-1 ;
    ex:observedOn ex:step-1 .

ex:cause-1 a ex:Cause ;
    ex:hasSolution ex:solution-1 .

ex:solution-1 a ex:Solution .

ex:fmea-1 a ex:FMEA ;
    ex:failureMode "3V3 rail over-voltage" ;
    ex:failureEffect "Board reset / damage" ;
    ex:failureCause "Feedback divider drift" ;
    ex:fmeaSeverity "7"^^xsd:integer ;
    ex:fmeaOccurrence "3"^^xsd:integer ;
    ex:fmeaDetection "4"^^xsd:integer ;
    ex:rpn "84"^^xsd:integer ;
    ex:fmeaCoversFault ex:fault-1 ;
    ex:fmeaCoversComponent ex:comp-1 .
"""


def conforming_sample_graph() -> str:
    """Return a turtle data graph that conforms to the domain SHACL shapes."""
    return _PREFIXES + _CONFORMING


__all__ = ["conforming_sample_graph"]
