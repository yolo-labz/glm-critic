import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/feed_opml.py"


class OpmlTests(unittest.TestCase):
    def run_converter(self, text):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "urls"
            p.write_text(text)
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(p)], capture_output=True, text=True
            )

    def test_preserves_urls_labels_and_categories(self):
        r = self.run_converter(
            '# comment\nhttps://example.org/?a=1&b=2 "A & B" news\n'
            'https://another.example/feed "Other" ai\n'
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        root = ET.fromstring(r.stdout)
        feeds = [x for x in root.iter("outline") if "xmlUrl" in x.attrib]
        self.assertEqual(len(feeds), 2)
        self.assertEqual(feeds[0].attrib["xmlUrl"], "https://example.org/?a=1&b=2")
        self.assertEqual(feeds[0].attrib["title"], "A & B")

    def test_rejects_duplicates_and_embedded_credentials(self):
        for text in (
            "https://example.org\nhttps://example.org\n",
            "https://user:secret@example.org/feed\n",
            "file:///etc/passwd\n",
        ):
            with self.subTest(text=text):
                r = self.run_converter(text)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
