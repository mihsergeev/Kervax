"""Bound notification text without changing the detailed event or its routing metadata."""
import html
import re
from html.parser import HTMLParser

MAX_CHARS = 480
MAX_LINES = 4
LINE_CHARS = 150
_TAGS = frozenset({'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'a', 'tg-spoiler'})
_COMMAND = re.compile(
    r"(?:^\s*(?:Что сделать|Команда|Последняя строка лога|Лог)\s*:|"
    r"\bansible-playbook\b|\bsudo\s+\S|\bcurl\s+-|\bwget\s+-|"
    r"\bssh\s+(?:-\w|[\w.-]+@)|\bkubectl\s+(?:apply|exec|delete|rollout|create)\b|"
    r"\bdocker\s+(?:exec|run)\s+-|\bdocker\s+compose\s+(?:-|up\b|down\b|restart\b|build\b))", re.I)


class _Rich(HTMLParser):
    def __init__(self, text, html_mode):
        super().__init__(convert_charrefs=True)
        self.rows = [[]]
        self.stack = []
        self.pre = 0
        self.removed = False
        if html_mode:
            self.feed(text)
            self.close()
        else:
            self.handle_data(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.rows.append([])
        elif tag == 'pre':
            self.pre += 1
            self.removed = True
        elif tag in _TAGS:
            href = dict(attrs).get('href') if tag == 'a' else None
            opening = '<a href="'+html.escape(href, quote=True)+'">' if href is not None else '<'+tag+'>'
            self.stack.append((tag, opening, href))

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.pre = max(self.pre-1, 0)
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.pre:
            return
        for i, part in enumerate(data.replace('\r\n', '\n').replace('\r', '\n').split('\n')):
            if i:
                self.rows.append([])
            part = re.sub(r'[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]', ' ', part)
            if part:
                self.rows[-1].append((part, tuple(self.stack)))


def _plain(row):
    return ''.join(text for text, _ in row)


def _command(row):
    match = _COMMAND.search(_plain(row).lstrip())
    return bool(match and (match.start() == 0 or
                          any(item[0] == 'code' for _, stack in row for item in stack)))


def _render(row, budget, html_mode):
    cut = len(_plain(row)) > budget
    left = max(0, budget-3) if cut else budget
    out = []
    for text, stack in row:
        text = text[:left]
        if not text:
            break
        left -= len(text)
        if html_mode:
            out.append(''.join(item[1] for item in stack)+html.escape(text)+
                       ''.join('</'+item[0]+'>' for item in reversed(stack)))
        else:
            out.append(text)
    return ''.join(out).rstrip()+('...' if cut else '')


def brief(text, html_mode=False, *, max_chars=MAX_CHARS, max_lines=MAX_LINES,
          line_chars=LINE_CHARS, keep_last=False):
    """Visible-character limits preserve complete HTML links and balanced formatting.

    Log blocks and installation commands stay in the panel. Short messages are returned
    unchanged. Button status can reserve the last line when an existing alert is edited.
    """
    text = str(text)
    try:
        rich = _Rich(text, html_mode)
    except (AssertionError, ValueError):
        rich = _Rich(text, False)
        rich.removed = True  # render malformed legacy markup as escaped text
    rows = [row for row in rich.rows if _plain(row).strip() and not _command(row)]
    kept = '\n'.join(_plain(row) for row in rows)
    if (not rich.removed and len(rows) == len(rich.rows) and len(rows) <= max_lines
            and len(kept) <= max_chars and all(len(_plain(row)) <= line_chars for row in rows)):
        return text
    if not rows:
        return 'Подробности в панели'
    keep_last = keep_last or _plain(rows[-1]).lstrip().startswith(('📝', '↪'))
    chosen = rows[:max_lines]
    if keep_last and len(rows) > max_lines:
        chosen = rows[:max_lines-1]+rows[-1:]
    links = [item for row in rows for _, stack in row for item in stack if item[2]]
    selected_links = {item[2] for row in chosen for _, stack in row for item in stack if item[2]}
    # A link following a long custom template must remain reachable after shortening.
    if max_lines > 1 and links and links[-1][2] not in selected_links:
        chosen = chosen[:max_lines-1]+[[('Подробнее в панели', (links[-1],))]]
    elif len(rows) > len(chosen) and not keep_last and not _plain(chosen[-1]).endswith('...'):
        chosen[-1] = chosen[-1]+[('...', ())]
    def build(selected, reserve_last):
        result, left = [], max_chars
        reserve = min(len(_plain(selected[-1])), line_chars)+1 if reserve_last and len(selected) > 1 else 0
        for i, row in enumerate(selected):
            budget = min(line_chars, left-(reserve if i < len(selected)-1 else 0))
            if budget < 4:
                break
            result.append(_render(row, budget, html_mode))
            left -= min(len(_plain(row)), budget)+1
        return '\n'.join(result)

    result = build(chosen, keep_last)
    if html_mode and links and max_lines > 1:
        rendered = _Rich(result, True)
        visible_links = {item[2] for row in rendered.rows for _, stack in row for item in stack if item[2]}
        if links[-1][2] not in visible_links:
            chosen = chosen[:max_lines-1]+[[('Подробнее в панели', (links[-1],))]]
            result = build(chosen, True)
    return result
