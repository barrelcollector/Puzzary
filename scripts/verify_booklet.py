"""Booklet page order, folded reading order, dimensions, and PDF rendering checks."""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import os
import unittest

from pypdf import PdfReader
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from app.booklet import booklet_layout, booklet_pairs, impose_booklet
from app.print_pdf import render_pdf

APP_DIR = Path(__file__).resolve().parents[1] / "app"


class BookletTests(unittest.TestCase):
    def test_order(self):
        self.assertEqual(booklet_pairs(8), [(7,0),(1,6),(5,2),(3,4)])
        self.assertEqual(booklet_pairs(5), booklet_pairs(8))
        self.assertEqual(booklet_pairs(1), [(3,0),(1,2)])
        for count in range(1,81):
            pairs = booklet_pairs(count)
            padded = ((count+3)//4)*4
            self.assertEqual(sorted(n for pair in pairs for n in pair), list(range(padded)))
            # Read fronts and backs on the nested folded sheets, outside to inside.
            left, right = [], []
            for front, back in zip(pairs[::2],pairs[1::2]):
                left.extend([front[1],back[0]])
                right[0:0] = [back[1],front[0]]
            self.assertEqual(left+right,list(range(padded)))
        with self.assertRaises(ValueError): booklet_pairs(0)

    def test_roll_rejected(self):
        with self.assertRaises(ValueError):
            booklet_layout({'page_width_mm':80,'page_height_mm':None,'page_margin_mm':4})

    def test_render(self):
        templates = Jinja2Templates(APP_DIR / "templates")
        output = Path(os.environ.get('QA_OUTPUT','/tmp/booklet-qa'))
        output.mkdir(parents=True,exist_ok=True)
        css = (APP_DIR / "static/styles.css").read_text()
        for name,width,height,count in [('letter',215.9,279.4,8),('a4',210,297,5),('custom',180,260,3)]:
            sheet = {'page_width_mm':width,'page_height_mm':height,'page_margin_mm':8,'page_size':name,'page_size_presets':[]}
            logical = booklet_layout(sheet)
            modules = [{'html':f'<h2>PAGE-{n:02}</h2><p>Folded booklet test content.</p>'} for n in range(1,count+1)]
            # Include a real boxed grid and embedded image to catch clipping/merge errors.
            cells = ''.join(f'<span>{letter}</span>' for letter in 'ABCDEFGHIJKLMNOP')
            modules[0]['html'] += f'<div class="print-word-grid" style="--grid-size:4">{cells}</div>'
            request = Request({'type':'http','method':'GET','path':'/','headers':[],'query_string':b''})
            html = templates.env.get_template('issue_print.html').render(
                request=request,issue=SimpleNamespace(id='qa',title='Booklet Test',published_at=None,description=''),
                snapshot={'modules':modules},print_layout=logical,sheet_layout=sheet,print_mode='booklet',
                print_pdf_css=css,hide_app_chrome=True,show_print_toolbar=False,
                appearance=SimpleNamespace(slug='dark',scheme='dark',style=''),current_user=None)
            sequential = render_pdf(html,logical)
            reader = PdfReader(BytesIO(sequential))
            self.assertEqual(len(reader.pages),count)
            imposed = impose_booklet(sequential,sheet)
            result = PdfReader(BytesIO(imposed))
            self.assertEqual(len(result.pages),((count+3)//4)*2)
            for side,pair in zip(result.pages,booklet_pairs(count)):
                self.assertAlmostEqual(float(side.mediabox.width),max(width,height)*72/25.4,places=2)
                self.assertAlmostEqual(float(side.mediabox.height),min(width,height)*72/25.4,places=2)
                expected = [f'PAGE-{index+1:02}' for index in pair if index < count]
                text = side.extract_text()
                actual = [word for word in text.split() if word.startswith('PAGE-')]
                self.assertEqual(actual,expected)
            self.assertEqual(result.trailer['/Root']['/ViewerPreferences']['/Duplex'],'/DuplexFlipShortEdge')
            (output/f'booklet-{name}.pdf').write_bytes(imposed)


if __name__ == '__main__': unittest.main(verbosity=2)
