"""The static publication must stay explicit and leave the gateway UI untouched."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_preview_build', ROOT / 'scripts/build_public_preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


def test_preview_is_labelled_without_modifying_gateway_files(tmp_path):
    original = (ROOT / 'frontend/index.html').read_bytes()
    output = tmp_path / 'site'
    preview.build(output)
    html = (output / 'index.html').read_text()
    assert 'Interactive preview — live AI validation unavailable' in html
    assert 'This preview is not the full video demo' in html
    assert html.index('src="public-preview.js"') < html.index('src="app.js"')
    assert (ROOT / 'frontend/index.html').read_bytes() == original
    assert (output / 'app.js').read_bytes() == (ROOT / 'frontend/app.js').read_bytes()


def test_only_frontend_and_explicit_deployment_assets_are_published(tmp_path):
    output = tmp_path / 'site'
    preview.build(output)
    assert (output / '.nojekyll').is_file()
    assert not (output / '.env').exists()
    assert not (output / '.git').exists()
    assert not (output / 'backend').exists()
    metadata = json.loads((output / 'deployment.json').read_text())
    assert metadata['mode'] == 'interactive-preview'
    assert metadata['live_ai_validation'] is False
    assert metadata['backend_available'] is False
    assert metadata['transactions_enabled'] is False
    assert len(metadata['source_commit']) == 40


def test_existing_output_is_preserved(tmp_path):
    marker = tmp_path / 'keep.txt'
    marker.write_text('existing work')
    with pytest.raises(SystemExit, match='existing files are preserved'):
        preview.build(tmp_path)
    assert marker.read_text() == 'existing work'


def test_build_cannot_copy_repository_into_itself():
    with pytest.raises(SystemExit, match='outside the repository'):
        preview.build(ROOT / 'must-not-create-public-output')
    assert not (ROOT / 'must-not-create-public-output').exists()
