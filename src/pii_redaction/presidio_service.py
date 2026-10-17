from typing import Iterable

from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_anonymizer import AnonymizerEngine

# Route/frequency abbreviations Presidio misreads as places or names ("SC" -> South Carolina)
CLINICAL_ABBREVIATIONS = ["IV", "PO", "SC", "SQ", "IM", "SL", "PR", "NG", "PO/NG", "PRN", "BID", "TID", "QID", "QD",
                          "ED", "ICU", "MICU", "SICU", "CCU", "PACU"]


class ClinicalPIIRedactor:
    def __init__(self):
        print("⏳ Initializing Microsoft Presidio Zero-Trust Middleware...")
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()
        
        # --- FDE FIX 1: Custom SSN Pattern Recognizer ---
        # Overrides the strict checksum to catch ANY xxx-xx-xxxx format
        ssn_pattern = Pattern(name="catch_all_ssn", regex=r"\d{3}-\d{2}-\d{4}", score=0.9)
        ssn_recognizer = PatternRecognizer(supported_entity="US_SSN", patterns=[ssn_pattern])
        self.analyzer.registry.add_recognizer(ssn_recognizer)
        
        # --- FDE FIX 2: Custom Deny-List for Medical Facilities ---
        # Forces Presidio to recognize specific hospital names as organizations
        hospital_recognizer = PatternRecognizer(
            supported_entity="ORGANIZATION",
            deny_list=["Massachusetts General Hospital", "Mayo Clinic", "Cleveland Clinic"],
            deny_list_score=1.0  # <--- Changed this from 'score'
        )
        self.analyzer.registry.add_recognizer(hospital_recognizer)

        # --- FDE FIX 3: Medical Record Numbers ---
        # Presidio has no built-in MRN recognizer; the redaction eval caught these leaking 100% of the time
        mrn_pattern = Pattern(
            name="mrn_with_label",
            regex=r"(?i)\b(?:MRN|MR#|medical record (?:number|no\.?|#))\s*[:#]?\s*\d{5,10}\b",
            score=0.95,
        )
        mrn_recognizer = PatternRecognizer(supported_entity="MEDICAL_RECORD_NUMBER", patterns=[mrn_pattern])
        self.analyzer.registry.add_recognizer(mrn_recognizer)

        self.target_entities = [
            "MEDICAL_RECORD_NUMBER",
            "PERSON", 
            "PHONE_NUMBER", 
            "EMAIL_ADDRESS", 
            "US_SSN", 
            "LOCATION", 
            "ORGANIZATION", 
            "DATE_TIME"
        ]

        self.allowed_terms = set()
        self.allow_terms(CLINICAL_ABBREVIATIONS)

    def allow_terms(self, terms: Iterable[str]):
        """Clinical vocabulary (drug names, routes, care units...) that is never redacted.
        A detected span is kept only if the whole span matches a term (case-insensitive), so a real
        name next to a drug name is still redacted."""
        self.allowed_terms.update(t.strip().casefold() for t in terms if t and t.strip())

    def redact_clinical_context(self, raw_text: str) -> str:
        if not raw_text:
            return ""

        analyzer_results = [
            r for r in self.analyzer.analyze(
                text=raw_text,
                entities=self.target_entities,
                language='en',
                score_threshold=0.4
            )
            if raw_text[r.start:r.end].strip().casefold() not in self.allowed_terms
        ]

        anonymized_result = self.anonymizer.anonymize(
            text=raw_text,
            analyzer_results=analyzer_results
        )
        
        return anonymized_result.text

if __name__ == "__main__":
    redactor = ClinicalPIIRedactor()
    
    simulated_ehr_note = """
    Patient John Doe (SSN: 234-00-1234) was admitted to Massachusetts General Hospital 
    on March 15th following a severe reaction to Furosemide. 
    Wife Jane Doe can be reached at 415-555-0198 or jane.doe@email.com.
    """
    
    print("\n🚨 RAW PHI FROM DATABASE:")
    print(simulated_ehr_note.strip())
    
    print("\n🛡️ REDACTED OUTPUT (Safe for LLM Prompt):")
    safe_text = redactor.redact_clinical_context(simulated_ehr_note)
    print(safe_text.strip())