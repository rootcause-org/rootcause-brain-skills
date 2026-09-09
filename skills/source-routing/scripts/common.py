"""Provider-neutral source-routing data and Markdown helpers (Python 3.11+, no dependencies)."""
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

STOP = set('de het een en van voor met op in is ik je u we dat die dit aan als om te er mijn zijn was niet ook naar bij kan heb heeft worden maar nog graag beste hallo dank bedankt groet vriendelijke groeten the a an and to of for with on in is i you we that this from are be have please thanks re fw fwd'.split())


def words(value):
    return set(w for w in re.findall(r'[^\W\d_]{3,}', value.lower()) if w not in STOP)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def cell(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\n', ' ').replace('`', '&#96;').replace('[', '&#91;').replace(']', '&#93;')


def link(title, url):
    if urlsplit(url).scheme not in ('http', 'https'):
        return cell(title)
    return f'[{cell(title)}]({quote(url, safe=":/?=&%#@+;")})'


def table(rows):
    lines = ['| Topic | Document | Path | Why | Verdict |', '|---|---|---|---|---|']
    for r in rows:
        lines.append('| ' + ' | '.join([cell(r['topic']), link(r['title'], r.get('url', '')), cell(r['path']), cell(r['why']), r['verdict']]) + ' |')
    return '\n'.join(lines) + '\n'
