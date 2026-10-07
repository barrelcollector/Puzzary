"""Reusable collaboration adapter for coordinate-based cell plugins."""
from copy import deepcopy
from types import SimpleNamespace
import re


def row_collaboration():
    """Shared rows use positional spaces so editing a gap cannot shift later letters."""
    def apply(state, action, solution):
        state = deepcopy(state)
        answers = dict(state.get('answers', {}))
        rows = solution['rows']
        expected = {str(i): row['answer'].upper() for i, row in enumerate(rows)}
        kind = action['kind']
        target = action.get('target', '')
        if kind == 'patch':
            for key, value in action.get('changes', {}).items():
                if key not in expected or rows[int(key)].get('gimme'):
                    raise ValueError('Row is fixed or outside the puzzle')
                if len(value) > len(expected[key]) or not re.fullmatch(r'[A-Z0-9 ]*', value):
                    raise ValueError('Invalid row value')
                if value: answers[key] = value
                else: answers.pop(key, None)
        elif kind in ('reveal', 'reveal_entry', 'hint'):
            keys = list(expected) if kind == 'reveal' else [target] if target else [
                next((k for k, v in expected.items() if answers.get(k) != v and not rows[int(k)].get('gimme')), '')]
            for key in keys:
                if key not in expected: raise ValueError('Row not found')
                answers[key] = expected[key]
            state['hints_used'] = state.get('hints_used', 0) + 1
        elif kind == 'clue':
            if target not in expected: raise ValueError('Row not found')
            clues = state.setdefault('clues', {})
            clues[target] = min(clues.get(target, 0) + 1, max(0, len(rows[int(target)].get('clues', [])) - 1))
        elif kind != 'check':
            raise ValueError('Unsupported action')
        for key in expected:
            if rows[int(key)].get('gimme'): answers[key] = expected[key]
        state['answers'] = answers
        if kind == 'check': state['checked'] = True
        state['incorrect'] = [k for k, v in answers.items() if v != expected.get(k)] if state.get('checked') else []
        return state
    return apply


def cell_collaboration(normalizer):
    def apply(state, action, solution):
        state = deepcopy(state)
        grid = solution.get('grid', solution.get('answers', {}))
        expected = ({str(k): str(v).upper() for k, v in grid.items()} if isinstance(grid, dict) else
                    {f'{x},{y}': str(v).upper() for y, row in enumerate(grid) for x, v in enumerate(row)
                     if v is not None and str(v) not in ('', '#')})
        givens = solution.get('givens', solution.get('gimmes', {}).get('cells', {}))
        editable = {k: v for k, v in expected.items() if k not in givens}
        answers = dict(state.get('answers', {}))
        kind = action['kind']
        if kind == 'patch':
            for key, value in action.get('changes', {}).items():
                if key not in editable: raise ValueError('Cell is fixed or outside the puzzle')
                if value and not re.fullmatch(r'[A-Z0-9]+|-?[0-9]+', value): raise ValueError('Invalid cell value')
                if value and editable[key] in ('0', '1') and set(expected.values()) <= {'0', '1'} and value not in ('0', '1'):
                    raise ValueError('Binary cells need 0 or 1')
                if value and isinstance(grid, list) and all(v.isdigit() for v in expected.values()) and value not in {str(i) for i in range(1, len(grid)+1)} and not set(expected.values()) <= {'0', '1'}:
                    raise ValueError('Digit is outside the grid range')
                if value == '': answers.pop(key, None)
                else: answers[key] = value
            normalized = normalizer(SimpleNamespace(answers=answers), solution).get('answers', {})
            normalized = {k: v for k, v in normalized.items() if k in editable}
            if any(normalized.get(k) != v for k, v in answers.items()):
                raise ValueError('Invalid cell value')
            answers = normalized
        elif kind == 'hint':
            key = next((k for k, v in editable.items() if answers.get(k) != v), None)
            if key is not None:
                answers[key] = editable[key]
                state['hints_used'] = state.get('hints_used', 0) + 1
        elif kind == 'reveal':
            answers = editable
            state['revealed'] = True
        elif kind in ('clue', 'reveal_entry', 'check_entry'):
            entry = next((e for e in solution.get('entries', []) if f"{e['number']}:{e['direction']}" == action.get('target')), None)
            if not entry: raise ValueError('Entry not found')
            keys = [f"{entry['x'] + (i if entry['direction'] == 'across' else 0)},{entry['y'] + (i if entry['direction'] == 'down' else 0)}"
                    for i in range(len(entry['answer']))]
            if kind == 'clue':
                clues = dict(state.get('clues', {}))
                clues[action['target']] = min(clues.get(action['target'], 0)+1, max(0, len(entry.get('clues', []))-1))
                state['clues'] = clues
            elif kind == 'reveal_entry':
                answers.update({k: editable[k] for k in keys if k in editable})
                state['hints_used'] = state.get('hints_used', 0)+1
            else:
                state['checked_keys'] = list(set(state.get('checked_keys', [])) | set(keys))
        elif kind != 'check':
            raise ValueError('Unsupported action')
        if kind == 'check' and solution.get('settings', {}).get('check_mode') == 'none':
            raise ValueError('Checking is disabled for this puzzle')
        state['answers'] = answers
        if kind == 'check': state['checked'] = True
        state['incorrect'] = [k for k, v in answers.items() if expected.get(k) != v and (state.get('checked') or k in state.get('checked_keys', []))]
        return state
    return apply
