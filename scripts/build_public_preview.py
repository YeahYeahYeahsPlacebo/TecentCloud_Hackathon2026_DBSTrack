#!/usr/bin/env python3
"""Build a frontend-only submission preview; never package repository secrets."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise SystemExit('Use a new, empty output path; existing files are preserved.')
    if destination == ROOT or ROOT in destination.parents:
        raise SystemExit('Build outside the repository to avoid publishing repository files.')
    shutil.copytree(ROOT / 'frontend', destination)
    html = (destination / 'index.html').read_text()
    html = html.replace('<title>DCTA Demo — Voice to Reviewed Transaction Drafts</title>',
                        '<title>Confirm Plus Chop — Interactive preview</title>')
    html = html.replace('<h1>Transaction Drafts</h1>', '<h1>Confirm Plus Chop</h1>')
    html = html.replace('  <!-- API status banner -->', '''  <section class="preview-banner" role="note" aria-label="Public preview limitations">
    <strong>Interactive preview — live AI validation unavailable</strong>
    <p>This is the merged demo frontend, hosted without a gateway. Try voice-to-text,
    edit your words, and explore the explicitly labelled sample cards below.</p>
    <p>Send, live two-transfer parsing, server clarification, passkey registration,
    signing and execution are unavailable here. Samples are not live AI results.
    This preview is not the full video demo and does not move money.</p>
  </section>
  <!-- API status banner -->''')
    html = html.replace('<script src="app.js"></script>',
                        '<script src="public-preview.js"></script>\n<script src="app.js"></script>')
    (destination / 'index.html').write_text(html)
    shutil.copyfile(ROOT / 'scripts/public_preview.js', destination / 'public-preview.js')
    (destination / '.nojekyll').touch()
    commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    (destination / 'deployment.json').write_text(json.dumps({
        'project': 'Confirm Plus Chop', 'mode': 'interactive-preview',
        'live_ai_validation': False, 'backend_available': False,
        'transactions_enabled': False, 'source_commit': commit,
    }, indent=2) + '\n')
    print('Built frontend-only preview at ' + str(destination))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='New output directory outside the repository')
    build(parser.parse_args().destination)
