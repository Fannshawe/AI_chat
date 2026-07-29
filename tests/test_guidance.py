from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.guidance import GuidanceRetriever
from src.llm_service import parse_json_response


class GuidanceTests(unittest.TestCase):
    def test_markdown_keyword_retrieval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guide.md").write_text("# 冲突处理\n吵架以后先确认感受，不要急着辩解。\n\n# 日常\n保持自然问候。", encoding="utf-8")
            results = GuidanceRetriever(root).search("昨天吵架了，怎么缓和")
            self.assertTrue(results)
            self.assertIn("不要急着辩解", results[0]["content"])

    def test_json_fence_parsing(self):
        result = parse_json_response('```json\n{"candidates":["好呀"]}\n```', {})
        self.assertEqual(result["candidates"], ["好呀"])


if __name__ == "__main__":
    unittest.main()
