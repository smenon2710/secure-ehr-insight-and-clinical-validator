from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_anonymizer import AnonymizerEngine

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

    def redact_clinical_context(self, raw_text: str) -> str:
        if not raw_text:
            return ""

        analyzer_results = self.analyzer.analyze(
            text=raw_text,
            entities=self.target_entities,
            language='en',
            score_threshold=0.4 
        )
        
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