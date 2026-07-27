"""Pure unit tests for synthesis response classification."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("OPENROUTER_API_KEY_ANALYST", "unused")

from pipeline.synthesize import classify_response


class ClassifyResponseTests(unittest.TestCase):
    def classify(self, content: str, finish_reason: str | None = None):
        return classify_response({"content": content}, finish_reason)

    def test_clean_object_is_ok(self):
        findings, state, _ = self.classify(
            '{"findings":[{"claim":"A","label":"observed","evidence_ids":[1]}]}'
        )
        self.assertEqual(state, "ok")
        self.assertEqual(len(findings), 1)

    def test_json_fence_is_ok(self):
        findings, state, _ = self.classify(
            '```json\n{"findings":[{"claim":"A","evidence_ids":[1]}]}\n```'
        )
        self.assertEqual(state, "ok")
        self.assertEqual(len(findings), 1)

    def test_prose_around_object_is_ok(self):
        findings, state, _ = self.classify(
            'Here is the result. {"findings":[{"claim":"A","evidence_ids":[1]}]} Done.'
        )
        self.assertEqual(state, "ok")
        self.assertEqual(len(findings), 1)

    def test_bare_top_level_array_is_ok(self):
        findings, state, _ = self.classify(
            '[{"claim":"A","label":"observed","evidence_ids":[1]}]'
        )
        self.assertEqual(state, "ok")
        self.assertEqual(len(findings), 1)

    def test_nested_result_findings_is_ok(self):
        findings, state, _ = self.classify(
            '{"result":{"findings":[{"claim":"A","evidence_ids":[1]}]}}'
        )
        self.assertEqual(state, "ok")
        self.assertEqual(len(findings), 1)

    def test_empty_findings_is_valid_empty(self):
        findings, state, _ = self.classify('{"findings":[]}')
        self.assertEqual(state, "valid_empty")
        self.assertEqual(findings, [])

    def test_non_json_prose_is_parse_failed(self):
        findings, state, meta = self.classify("There were no useful results.")
        self.assertEqual(state, "parse_failed")
        self.assertEqual(findings, [])
        self.assertTrue(meta["validation_errors"])

    def test_length_finish_reason_is_truncated(self):
        findings, state, _ = self.classify('{"findings":[{"claim":"cut off"}', "length")
        self.assertEqual(state, "truncated")
        self.assertEqual(findings, [])

    def test_wrong_findings_type_is_schema_invalid(self):
        findings, state, meta = self.classify('{"findings":{}}')
        self.assertEqual(state, "schema_invalid")
        self.assertEqual(findings, [])
        self.assertTrue(meta["validation_errors"])

    def test_unrelated_nested_array_is_schema_invalid(self):
        findings, state, _ = self.classify('{"other":[{"claim":"A"}]}')
        self.assertEqual(state, "schema_invalid")
        self.assertEqual(findings, [])

    def test_integer_evidence_ids_are_preserved_without_membership_filtering(self):
        findings, state, _ = self.classify(
            '{"findings":[{"claim":"A","evidence_ids":[1,"2",999,"junk",2.5,true,null]}]}'
        )
        self.assertEqual(state, "ok")
        self.assertEqual(findings[0]["evidence_ids"], [1, 2, 999])

    def test_reasoning_is_used_when_content_is_empty(self):
        findings, state, meta = classify_response(
            {"content": "", "reasoning": '{"findings":[{"claim":"A"}]}'}, None
        )
        self.assertEqual(state, "ok")
        self.assertEqual(len(findings), 1)
        self.assertGreater(meta["reasoning_len"], 0)


if __name__ == "__main__":
    unittest.main()
