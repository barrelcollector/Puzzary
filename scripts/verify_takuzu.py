"""Independent rules, seed, answer, and second-solution checks for Takuzu."""
import itertools
import json
from types import SimpleNamespace
import unittest

from ortools.sat.python import cp_model

import app.puzzles  # Load installed packages before compatibility imports.
from app.puzzle_types.takuzu import generate, build_manual, validate_grid, check_answer, count_progress, normalize_progress


def independent_unique(grid, givens):
    n=len(grid)
    patterns=[line for line in itertools.product((0,1),repeat=n) if sum(line)==n//2 and all(len(set(line[k:k+3]))>1 for k in range(n-2))]
    model=cp_model.CpModel()
    cells=[[model.new_bool_var(f'{x},{y}') for x in range(n)] for y in range(n)]
    for axis in range(2):
        codes=[]
        for i in range(n):
            line=cells[i] if axis==0 else [cells[y][i] for y in range(n)]
            model.add_allowed_assignments(line,patterns)
            code=model.new_int_var(0,2**n-1,f'{axis}:{i}')
            model.add(code==sum(v*2**j for j,v in enumerate(line))); codes.append(code)
        model.add_all_different(codes)
    for key,value in givens.items():
        x,y=map(int,key.split(',')); model.add(cells[y][x]==int(value))
    model.add_bool_or([cells[y][x].Not() if grid[y][x] else cells[y][x] for y in range(n) for x in range(n)])
    solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=5
    return solver.solve(model)==cp_model.INFEASIBLE


class TakuzuTests(unittest.TestCase):
    def test_generation_and_uniqueness(self):
        for n in (4,6,8,10):
            for difficulty in ('easy','medium','hard'):
                p=generate('',difficulty,seed=f'{n}-{difficulty}',generation_options={'grid_size':n})
                grid=p.solution_payload['grid']; validate_grid(grid)
                self.assertTrue(independent_unique(grid,p.solution_payload['givens']))
                self.assertNotIn('grid',p.print_payload)
                self.assertNotIn('grid',p.play_payload)
                self.assertLess(len(p.play_payload['givens']),n*n)
    def test_seed(self):
        p=generate('','medium',seed='repeat')
        self.assertEqual(p,generate('','medium',seed='repeat'))
        self.assertNotEqual(p.solution_payload['grid'],generate('','medium',seed='another').solution_payload['grid'])
        self.assertNotEqual(generate('','medium').metadata['seed'],generate('','medium').metadata['seed'])
    def test_answers(self):
        p=generate('','easy',seed='answers',generation_options={'grid_size':4})
        solution=p.solution_payload
        answers={f'{x},{y}':str(v) for y,row in enumerate(solution['grid']) for x,v in enumerate(row)}
        self.assertTrue(check_answer(json.dumps({'answers':answers}),solution)[0])
        key=next(k for k,v in answers.items() if k not in solution['givens'] and v=='0')
        del answers[key]
        self.assertFalse(check_answer(answers,solution)[0])
        answers[key]='1'; self.assertFalse(check_answer(answers,solution)[0])
        progress=normalize_progress(SimpleNamespace(answers={**answers,'999,999':'1','0,0':True}),solution)
        self.assertNotIn('999,999',progress['answers'])
        self.assertNotIn('0,0',progress['answers'])
        self.assertFalse(set(progress['answers']) & set(solution['givens']))
        self.assertEqual(count_progress({},solution)[0],len(solution['givens']))
    def test_manual_options(self):
        grid=[[0,0,1,1],[0,1,0,1],[1,0,1,0],[1,1,0,0]]
        p=build_manual({'solution_grid':json.dumps(grid),'given_count':0,'auto_confirm_lines':'no'})
        self.assertEqual(p.solution_payload['grid'],grid)
        self.assertFalse(p.play_payload['settings']['auto_confirm_lines'])
        self.assertTrue(independent_unique(grid,p.play_payload['givens']))
        for bad in ('bad','[]','{}','[[2]]',json.dumps([[True]*4]*4),json.dumps([[0,1,0,1]]*4)):
            with self.assertRaises(ValueError): build_manual({'solution_grid':bad})
        for n in (3,5,12,100,6.5):
            with self.assertRaises(ValueError): generate('','easy',generation_options={'grid_size':n})
        for givens in (-1,16,'bad',2.5,True):
            with self.assertRaises(ValueError): build_manual({'solution_grid':grid,'given_count':givens})


if __name__=='__main__': unittest.main(verbosity=2)
