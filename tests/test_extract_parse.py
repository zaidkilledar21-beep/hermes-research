"""Pure unit tests for extraction response parsing."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("OPENROUTER_API_KEY_ANALYST", "unused")

from pipeline.extract import parse_extraction_response


class ParseExtractionResponseTests(unittest.TestCase):
    def test_clean_json(self):
        parsed = parse_extraction_response(
            '{"text":"Quoted evidence.","answers_question":true,'
            '"facet":"pricing-model","why":"Quoted evidence."}'
        )
        self.assertEqual(
            parsed, ("Quoted evidence.", True, "pricing-model", "Quoted evidence.")
        )

    def test_fenced_json(self):
        parsed = parse_extraction_response(
            '```json\n{"text":"Useful detail.","answers_question":true,'
            '"facet":"use-cases","why":"Useful detail."}\n```'
        )
        self.assertEqual(parsed, ("Useful detail.", True, "use-cases", "Useful detail."))

    def test_prose_wrapped_json(self):
        parsed = parse_extraction_response(
            'Here is the result: {"text":"Unrelated item.","answers_question":false,'
            '"facet":null,"why":"Unrelated item."} Done.'
        )
        self.assertEqual(parsed, ("Unrelated item.", False, None, "Unrelated item."))

    def test_plain_text_falls_back_with_unknown_relevance(self):
        parsed = parse_extraction_response("Cleaned evidence without JSON.")
        self.assertEqual(parsed, ("Cleaned evidence without JSON.", None, None, None))

    def test_empty_response(self):
        self.assertEqual(parse_extraction_response("  \n"), (None, None, None, None))

    def test_malformed_json_falls_back_to_whole_response(self):
        raw = '{"text":"Truncated evidence","answers_question":true'
        self.assertEqual(parse_extraction_response(raw), (raw, None, None, None))


if __name__ == "__main__":
    unittest.main()
