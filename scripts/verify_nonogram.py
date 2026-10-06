"""Run with PYTHONPATH=/app; isolated module tests, no live database writes."""
import itertools
import json
import unittest
from types import SimpleNamespace

from app.puzzle_types.nonogram import (
    runs, line_patterns, generate, build_manual, check_answer, normalize_progress,
)


class NonogramChecks(unittest.TestCase):
    def test_line_patterns_exhaustive(self):
        for size in range(1, 9):
            expected = {}
            for line in itertools.product((0, 1), repeat=size):
                expected.setdefault(tuple(runs(line)), set()).add(line)
            for clues, patterns in expected.items():
                self.assertEqual(set(line_patterns(size, clues)), patterns)

    def test_generation_and_uniqueness(self):
        from ortools.sat.python import cp_model
        for size in (5, 10, 15):
            for symmetry in ("mirror", "rotational", "random"):
                p = generate('', 'medium', seed=f'test-{size}-{symmetry}',
                             generation_options={'grid_size':size, 'symmetry':symmetry})
                self.assertTrue(p.metadata['uniqueness_verified'])
                grid = p.solution_payload['grid']
                self.assertEqual(p.play_payload['row_clues'], [runs(row) for row in grid])
                self.assertNotIn('grid', p.play_payload)
                model = cp_model.CpModel()
                cells = [[model.new_bool_var(f'{x},{y}') for x in range(size)] for y in range(size)]
                for y in range(size): model.add_allowed_assignments(cells[y], line_patterns(size, tuple(p.play_payload['row_clues'][y])))
                for x in range(size): model.add_allowed_assignments([cells[y][x] for y in range(size)], line_patterns(size, tuple(p.play_payload['column_clues'][x])))
                for key, value in p.solution_payload['givens'].items():
                    x,y=map(int,key.split(',')); model.add(cells[y][x] == int(value))
                model.add_bool_or([cells[y][x].Not() if grid[y][x] else cells[y][x] for y in range(size) for x in range(size)])
                solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=5
                self.assertEqual(solver.solve(model), cp_model.INFEASIBLE)

    def test_seed_and_answers(self):
        p = generate('', 'easy', seed='repeat', generation_options={'grid_size':5})
        self.assertEqual(p, generate('', 'easy', seed='repeat', generation_options={'grid_size':5}))
        other = generate('', 'easy', seed='different', generation_options={'grid_size':5})
        self.assertNotEqual(p.solution_payload['grid'], other.solution_payload['grid'])
        answer = {f'{x},{y}':str(v) for y,row in enumerate(p.solution_payload['grid']) for x,v in enumerate(row)}
        self.assertTrue(check_answer(json.dumps(answer), p.solution_payload)[0])
        key = next(k for k in answer if k not in p.solution_payload['givens'])
        answer[key] = str(1-int(answer[key]))
        self.assertFalse(check_answer(answer, p.solution_payload)[0])
        normalized = normalize_progress(SimpleNamespace(answers={**answer,'999,999':'1','0,0':'garbage'}), p.solution_payload)
        self.assertNotIn('999,999', normalized['answers'])
        self.assertNotIn('0,0', normalized['answers'])
        self.assertFalse(set(normalized['answers']) & set(p.solution_payload['givens']))

    def test_manual_and_invalid(self):
        grid = [[int(x==y) for x in range(5)] for y in range(5)]
        p = build_manual({'solution_grid':json.dumps(grid)})
        self.assertEqual(p.solution_payload['grid'], grid)
        for raw in ('bad', '[]', '[[2]]', '{}'):
            with self.assertRaises(ValueError): build_manual({'solution_grid':raw})
        with self.assertRaises(ValueError): generate('', 'easy', generation_options={'grid_size':100})


if __name__ == '__main__': unittest.main(verbosity=2)
