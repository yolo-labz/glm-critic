"""Convert a newsboat urls file to OPML, without fetching or dropping feeds."""
import argparse
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit


def convert(text: str) -> str:
    root = ET.Element('opml', version='2.0')
    ET.SubElement(ET.SubElement(root, 'head'), 'title').text = 'Primary intake'
    body = ET.SubElement(root, 'body')
    groups = {}
    seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        url, *tags = fields
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
            raise ValueError(f'line {number}: expected a public feed URL without credentials')
        if url in seen:
            raise ValueError(f'line {number}: duplicate feed URL')
        seen.add(url)
        title = tags[0] if tags else parsed.hostname
        category = tags[1] if len(tags) > 1 else 'Feeds'
        if category not in groups:
            groups[category] = ET.SubElement(body, 'outline', text=category, title=category)
        ET.SubElement(groups[category], 'outline', type='rss', text=title, title=title, xmlUrl=url)
    ET.indent(root)
    return ET.tostring(root, encoding='unicode', xml_declaration=True) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('urls', type=Path)
    args = parser.parse_args()
    try:
        sys.stdout.write(convert(args.urls.read_text()))
    except ValueError as exc:
        parser.error(str(exc))
