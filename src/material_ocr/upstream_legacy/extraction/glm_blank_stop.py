"""Streaming HTML blank-row guard. No model dependencies for parser tests."""
from html.parser import HTMLParser
import re

BLANK_ROW_LIMIT = 5


class BlankRowDetector(HTMLParser):
    def __init__(self, limit=BLANK_ROW_LIMIT):
        super().__init__(convert_charrefs=True)
        if limit < 1:
            raise ValueError("blank-row limit must be positive")
        self.limit = limit
        self.text = ""
        self.table_depth = 0
        self.in_row = self.in_cell = False
        self.cells = 0
        self.nonempty = False
        self.valid = True
        self.spanning_rows = 0
        self.consecutive = 0
        self.triggered = False
        self.stop_char_offset = None

    def feed(self, data):
        self.text += data
        super().feed(data)

    def handle_starttag(self, tag, attrs):
        if self.triggered:
            return
        if tag == "table":
            if self.table_depth:
                self.valid = False
            self.table_depth += 1
            self.consecutive = 0
        elif tag == "tr":
            if self.in_row:
                self.consecutive = 0
            self.in_row, self.in_cell = True, False
            self.cells, self.nonempty = 0, False
            self.valid = self.table_depth == 1
        elif tag in ("td", "th") and self.in_row:
            if self.in_cell:
                self.valid = False
            self.in_cell = True
            self.cells += 1
            try:
                span = int(dict(attrs).get("rowspan", "1"))
            except (TypeError, ValueError):
                span = 0
            if not 1 <= span <= 500:
                self.valid = False
            elif span > 1:
                self.spanning_rows = max(self.spanning_rows, span)
        elif self.in_row and tag not in ("br", "span", "p", "div", "b", "i", "em", "strong"):
            self.nonempty = True  # images, scripts, nested tables are not empty cells

    def handle_data(self, data):
        if not self.triggered and self.in_row and data.strip():
            self.nonempty = True  # "0", "-", underscores are NOT whitespace

    def handle_endtag(self, tag):
        if self.triggered:
            return
        if tag in ("td", "th"):
            if not self.in_cell:
                self.valid = False
            self.in_cell = False
        elif tag == "tr":
            blank = (self.in_row and not self.in_cell and self.valid and
                     self.cells > 0 and not self.nonempty and self.spanning_rows == 0)
            self.consecutive = self.consecutive + 1 if blank else 0
            self.spanning_rows = max(0, self.spanning_rows - 1)
            self.in_row = False
            if self.consecutive >= self.limit:
                line, column = self.getpos()
                start = sum(len(s) + 1 for s in self.text.split("\n")[:line-1]) + column
                closing = re.match(r"</tr\s*>", self.text[start:], re.I)
                if closing:
                    self.stop_char_offset = start + closing.end()
                    self.triggered = True
        elif tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            self.consecutive = self.spanning_rows = 0
            self.in_row = self.in_cell = False

    def metadata(self):
        return dict(triggered=self.triggered,
                    reason="consecutive_blank_rows" if self.triggered else None,
                    blank_row_limit=self.limit, consecutive_blank_rows=self.consecutive,
                    stop_char_offset=self.stop_char_offset,
                    page_completeness_verified=False if self.triggered else None)


class BlankRowsStoppingCriteria:
    """Transformers callable, one page per generate(), checked after each token."""
    def __init__(self, processor, prompt_length):
        self.processor = processor
        self.prompt_length = prompt_length
        self.detector = BlankRowDetector()
        self.decoded = ""

    def __call__(self, input_ids, scores=None, **kwargs):
        import torch
        if input_ids.shape[0] != 1:
            raise ValueError("Blank-row stopping is configured for one page per generation")
        decoded = self.processor.decode(input_ids[0, self.prompt_length:],
            skip_special_tokens=True, clean_up_tokenization_spaces=False)
        # Some tokenizers revise a final partial Unicode character on the next token.
        if not decoded.startswith(self.decoded):
            self.detector = BlankRowDetector()
            self.detector.feed(decoded)
        else:
            self.detector.feed(decoded[len(self.decoded):])
        self.decoded = decoded
        return torch.tensor([self.detector.triggered], dtype=torch.bool, device=input_ids.device)
