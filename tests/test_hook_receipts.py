import json

import pytest

import memento.hook as hook


@pytest.mark.parametrize(
    ('question', 'title'),
    [(None, 'untitled'), ('', 'untitled'), (' \n\t ', 'untitled'),
     ('  First\nsecond\r\n third\t word  ', 'First second third word'),
     ('word \n' * 20, (' '.join(['word'] * 20))[:57] + '...')],
)
def test_receipt_normalizes_title_before_truncating(question, title):
    assert hook._receipt_line([{'question': question}]) == (
        f'Memento: 1 record (undated {title} (session unknown))'
    )


def _record(identifier, text):
    return dict(id=identifier, text=text, question=identifier, source='source',
                source_type='claude_session', source_id='session', source_hash='hash',
                timestamp='2026-11-12T00:00:00Z')


@pytest.mark.parametrize('budget_kind', ['all', 'skip_first', 'none'])
def test_receipts_only_include_injected_records(tmp_path, monkeypatch, budget_kind):
    records = [_record('large', 'word ' * 100), _record('small', 'short')]
    monkeypatch.setattr(hook, 'search', lambda *args: (records, {}))
    budget = {'all': 1000, 'skip_first': 40, 'none': 0}[budget_kind]
    expected = {'all': records, 'skip_first': records[1:], 'none': []}[budget_kind]
    response = hook.handle_user_prompt(
        {'hook_event_name': 'UserPromptSubmit', 'prompt': 'private prompt sentinel'},
        tmp_path / 'db', tmp_path, embedder=object(),
        config=hook.HookConfig(max_injected_tokens=budget),
    )
    # Nothing extra is persisted: the prompt must not land in any file.
    assert not (tmp_path / 'hook_receipts.jsonl').exists()
    for written in tmp_path.glob('*.jsonl'):
        assert 'private prompt sentinel' not in written.read_text()
    # Retrieval metrics continue to count all search results.
    assert json.loads((tmp_path / 'hook_metrics.jsonl').read_text())['result_count'] == 2
    if expected:
        assert response['systemMessage'] == hook._receipt_line(expected)
        # Preserve original numbering even when the first record is skipped.
        context = '\n\n'.join([hook._INTRO] + [
            hook._format_record(i, r) for i, r in enumerate(records, 1) if r in expected
        ])
        assert response['hookSpecificOutput']['additionalContext'] == context
    else:
        assert response == {}


def test_receipt_is_never_longer_than_its_title_cap():
    line = hook._receipt_line([{'question': 'x' * 200}])
    title = line.split('(undated ', 1)[1].split(' (session', 1)[0]
    assert len(title) == 60 and title.endswith('...')
