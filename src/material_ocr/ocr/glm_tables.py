"""Read GLM's logical tables without inventing image or word coordinates.

Logical cell indices are references into model output, never pixel boxes. The
caller must retain the original output and page/ROI geometry as its evidence.
"""
from html import unescape
from html.parser import HTMLParser
import re


def clean(value):
    return re.sub(r'[ \t\r\f\v]+', ' ', unescape(str(value))).strip()


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.outside, self.issues = [], [], []
        self.depth, self.table, self.row, self.cell = 0, None, None, None
        self.context_start = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.table = dict(rows=[], context=clean(''.join(self.outside[self.context_start:])[-1200:]), issues=[])
            else:
                self.table['issues'].append('nested_table')
            return
        if self.depth != 1:
            return
        if tag == 'tr':
            if self.row is not None:
                self._finish_row('unclosed_row')
            self.row = []
        elif tag in ('td', 'th'):
            if self.cell is not None:
                self._finish_cell()
                self.table['issues'].append('unclosed_cell')
            values = dict(attrs)
            spans = {}
            for name in ('rowspan', 'colspan'):
                raw = values.get(name, '1')
                spans[name] = int(raw) if str(raw).isdigit() and 1 <= int(raw) <= 100 else 1
                if str(spans[name]) != str(raw):
                    self.table['issues'].append('invalid_' + name)
            self.cell = dict(text='', header=tag == 'th', **spans)
        elif tag in ('br', 'p', 'div') and self.cell is not None:
            self.cell['text'] += '\n'

    def handle_endtag(self, tag):
        if tag == 'table':
            if self.depth == 1:
                self._finish_row()
                self.tables.append(self.table)
                self.table = None
                self.outside.append('\n')
                self.context_start = len(self.outside)
            self.depth = max(0, self.depth - 1)
        elif self.depth == 1 and tag in ('td', 'th'):
            self._finish_cell()
        elif self.depth == 1 and tag == 'tr':
            self._finish_row()
        elif self.depth == 0 and tag in ('p', 'div', 'br', 'h1', 'h2', 'h3'):
            self.outside.append('\n')

    def handle_data(self, data):
        if self.depth == 0:
            self.outside.append(data)
        elif self.depth == 1 and self.cell is not None:
            self.cell['text'] += data

    def _finish_cell(self):
        if self.cell is not None:
            if self.row is None:
                self.row = []
                self.table['issues'].append('cell_without_row')
            self.cell['text'] = clean(self.cell['text'])
            self.row.append(self.cell)
            self.cell = None

    def _finish_row(self, issue=None):
        self._finish_cell()
        if self.row is not None:
            if self.row:
                self.table['rows'].append(self.row)
            self.row = None
        if issue:
            self.table['issues'].append(issue)

    def finish(self):
        if self.depth and self.table is not None:
            self._finish_row()
            self.table['issues'].append('truncated_table')
            self.tables.append(self.table)
            self.depth, self.table = 0, None


def expand_table(table):
    """Expand only explicit spans, retaining each originating logical cell."""
    grid, origins, occupied = [], [], {}
    issues = list(table.get('issues', []))
    for row_index, row in enumerate(table['rows']):
        column = 0
        for cell_index, cell in enumerate(row):
            while (row_index, column) in occupied:
                column += 1
            origin = [row_index, cell_index]
            for dr in range(cell.get('rowspan', 1)):
                for dc in range(cell.get('colspan', 1)):
                    key = row_index + dr, column + dc
                    if key in occupied:
                        issues.append('overlapping_spans')
                    occupied[key] = (cell['text'], origin)
            column += cell.get('colspan', 1)
    height = max((r for r, _ in occupied), default=-1) + 1
    width = max((c for _, c in occupied), default=-1) + 1
    for row in range(height):
        grid.append([occupied.get((row, c), ('', None))[0] for c in range(width)])
        origins.append([occupied.get((row, c), ('', None))[1] for c in range(width)])
    return dict(table, grid=grid, cell_origins=origins, issues=sorted(set(issues)),
                coordinate_system='model_output_logical_cells', polygon_page=None)


def parse_tables(text):
    """Parse HTML or pipe tables; preserve malformed output as review issues."""
    parser = _Tables()
    parser.feed(text)
    parser.finish()
    tables = parser.tables
    if not tables:
        lines = str(text).splitlines()
        group, context = [], []
        for line in lines + ['']:
            stripped = line.strip()
            if stripped.startswith('|') and stripped.endswith('|'):
                cells = [clean(c) for c in stripped[1:-1].split('|')]
                if all(re.fullmatch(r':?-{3,}:?', c.replace(' ', '')) for c in cells):
                    continue
                group.append([dict(text=c, rowspan=1, colspan=1, header=False) for c in cells])
            else:
                if len(group) >= 2:
                    tables.append(dict(rows=group, context='\n'.join(context)[-800:], issues=[]))
                group = []
                context.append(line)
    return dict(tables=[dict(expand_table(table), table_index=i) for i, table in enumerate(tables)],
                non_table_text=clean(''.join(parser.outside)),
                coordinate_note='No word/cell pixel coordinates are supplied by native GLM output.')
