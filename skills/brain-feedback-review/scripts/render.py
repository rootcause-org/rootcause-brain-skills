# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Validate model-authored review.json against evidence; render an offline questionnaire."""
import argparse
from html import escape as e
import json
from pathlib import Path
from urllib.parse import quote
import re
import subprocess

TYPES = {'rule', 'wording', 'source-prefer-avoid', 'persona'}


def validate(data, evidence):
    refs = {r['run_id']: r for r in evidence['items']}
    for key in ('period', 'owner', 'lang', 'project'):
        assert isinstance(data.get(key), str) and data[key], f'missing {key}'
    seen = set()
    assert data['project'] == evidence['project'] and data.get('tenant', '') == evidence.get('tenant', ''), 'scope mismatch'
    assert data['lang'] in {'nl', 'en'}, 'supported languages: nl, en'
    assert isinstance(data.get('coverage_note'), str) and data['coverage_note'], 'state coverage in owner language'
    used = set()
    for item in data['items']:
        assert item['run_id'] in refs, 'unknown evidence'
        assert item['run_id'] not in used, 'duplicate item'
        used.add(item['run_id'])
        ref = refs[item['run_id']]
        assert item['url'] == ref['url'], 'use canonical evidence link'
        assert item['learning_allowed'] == ref['learning_allowed'], 'preserve learning exclusion'
        assert 1 <= len(item['questions']) <= 3
        for field in ('question', 'proposed', 'sent', 'feedback', 'sources'):
            assert isinstance(item[field], str) and item[field], field
        for q in item['questions']:
            assert q['id'] not in seen and re.fullmatch(r'[a-z0-9-]+', q['id'])
            seen.add(q['id'])
            assert q['type'] in TYPES
            assert 2 <= len(q['options']) <= 5
            values = set()
            for o in q['options']:
                assert o['value'] not in values
                values.add(o['value'])
                assert o['effect'] in {'confirm', 'fine', 'defer'}
                if o['effect'] == 'confirm':
                    assert o['scope'] in {'general', 'customer'} and o['learning'].strip()
    omitted = set(data.get('omitted_run_ids', []))
    assert used | omitted == refs.keys() and not used & omitted, 'account for every collected example'
    if omitted:
        assert data.get('omission_note'), 'explain grouped/omitted examples'


CSS = '''body{font:16px/1.6 system-ui;color:#183b36;background:#f5f4ee;margin:0}main{max-width:880px;margin:auto;padding:40px 20px 130px}h1{font-size:34px;line-height:1.2}h2{font-size:21px}article,.intro{background:white;border:1px solid #d7ded6;border-radius:16px;padding:24px;margin:20px 0}label{display:block;padding:8px;background:#f5f7f3;margin:6px 0;border-radius:7px;cursor:pointer}fieldset{border:0;padding:12px 0}legend{font-weight:650}textarea{box-sizing:border-box;width:100%;min-height:64px;padding:10px;font:inherit}details{margin:12px 0}summary{cursor:pointer;font-weight:600}.excerpt{white-space:pre-wrap;overflow-wrap:anywhere;color:#354943}small,.muted{color:#536861}a{color:#146e63}button{background:#145f54;color:white;border:0;padding:13px 20px;border-radius:8px;font:inherit;cursor:pointer}footer{position:sticky;bottom:0;background:#fffef4;padding:14px;border-top:1px solid #d7ded6}#output{min-height:220px}input{accent-color:#146e63}.badge{font-size:13px;color:#715114}'''


def build(data):
    nl = data['lang'] == 'nl'
    t = lambda a,b: a if nl else b
    cards = []
    for i,item in enumerate(data['items'], 1):
        qs = []
        for q in item['questions']:
            opts = ''.join(f'<label><input type="radio" name="{e(q["id"])}" value="{e(o["value"])}"> {e(o["label"])}</label>' for o in q['options'])
            qs.append(f'<fieldset><legend>{e(q["text"])}</legend>{opts}<textarea data-detail="{e(q["id"])}" aria-label="{e(q["text"])} — {t("Toelichting", "Details")}" placeholder="{t("Toelichting (optioneel; verplicht bij alleen deze klant)", "Details (optional; required for this customer only)")}"></textarea></fieldset>')
        details = ''.join(f'<details><summary>{label}</summary><div class="excerpt">{e(item[key])}</div></details>' for key,label in [('proposed',t('Voorstel ReplyPen','ReplyPen proposal')),('sent',t('Wat de mens verstuurde','Human sent')),('sources',t('Geraadpleegde bronnen','Consulted sources'))])
        badge = '' if item['learning_allowed'] else f'<p class="badge">{t("Alleen beoordelen: dit voorbeeld is niet beschikbaar als leermateriaal. Antwoorden worden niet als wijziging geëxporteerd.", "Review only: this example is excluded from learning. Answers will not authorize changes.")}</p>'
        cards.append(f'<article><small>{i} / {len(data["items"])}</small><h2>{e(item["question"])}</h2><p class="excerpt">{e(item["feedback"])}</p>{badge}{details}<a href="{e(item["url"])}" target="_blank" rel="noreferrer">{t("Open gesprek", "Open conversation")} ↗</a>{"".join(qs)}</article>')
    payload = json.dumps(data,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    exporter = Path(__file__).with_name('answers.js').read_text()
    js = '''const data=JSON.parse(document.getElementById('data').textContent);
const key='feedback-review:'+data.project+':'+data.tenant+':'+data.period+':'+data.items.flatMap(i=>i.questions.map(q=>q.id)).join(',');
let answers={};try{answers=JSON.parse(localStorage.getItem(key)||'{}')}catch{}
for(const el of document.querySelectorAll('input[type=radio]')){el.checked=answers[el.name]?.choice===el.value;el.onchange=()=>{answers[el.name]={...answers[el.name],choice:el.value};update()}}
for(const el of document.querySelectorAll('[data-detail]')){el.value=answers[el.dataset.detail]?.detail||'';el.oninput=()=>{const id=el.dataset.detail;answers[id]={...answers[id],detail:el.value};update()}}
function update(){document.getElementById('output').value=markdown(data,answers);document.getElementById('progress').textContent=Object.values(answers).filter(a=>a.choice).length+' / '+data.items.reduce((n,i)=>n+i.questions.length,0);try{localStorage.setItem(key,JSON.stringify(answers))}catch{}}
document.getElementById('copy').onclick=async()=>{const out=document.getElementById('output');try{await navigator.clipboard.writeText(out.value)}catch{out.focus();out.select();if(!document.execCommand('copy')){document.getElementById('status').textContent=data.lang==='nl'?'Selecteer en kopieer de tekst hieronder.':'Select and copy the text below.';return}}document.getElementById('status').textContent=data.lang==='nl'?'Gekopieerd':'Copied'};
update();'''
    target = f'https://app.replypen.com/projects/{quote(data["project"], safe="")}'
    if data.get('tenant'):
        target += '/tenants/' + quote(data['tenant'], safe='')
    target += '/brain-changes'
    return f'''<!doctype html><html lang="{data['lang']}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{t('Feedbackreview','Feedback review')} {e(data['project'])}</title><style>{CSS}</style><main><small>{e(data['owner'])} · {e(data['period'])}</small><h1>{t('Samen betere antwoorden','Better answers together')}</h1><div class="intro"><p>{t('Ongeveer 10 minuten. Lees de feedback, kies wat we mogen onthouden en kopieer je antwoorden. Niets wordt automatisch verstuurd of gewijzigd.', 'About 10 minutes. Read the feedback, choose what we may learn, then copy your answers. Nothing is sent or changed automatically.')}</p><p>{e(data['coverage_note'])}</p><p>{e(data.get('comparison_note',''))}</p><p>{e(data.get('omission_note',''))}</p></div>{''.join(cards)}<h2>{t('Je antwoorden','Your answers')}</h2><p>{t('Plak in','Paste into')} <a href="{e(target)}">Brain-changes</a>. {t('Daar zie je de herkomst van de wijzigingen. Gebruik de setup-chat als je eerst iets wilt uitklaren. Controleer eventuele instellingsvoorstellen afzonderlijk.', 'It records change provenance. Use setup chat for clarification first. Check any settings proposals separately.')}</p><textarea id="output" aria-label="Markdown"></textarea></main><footer><span id="progress"></span> <button id="copy">Copy as markdown</button> <span id="status" role="status"></span></footer><script id="data" type="application/json">{payload}</script><script>{exporter}\n{js}</script></html>'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('review',type=Path)
    a=p.parse_args()
    data=json.loads(a.review.read_text())
    evidence=json.loads(a.review.with_name('evidence.json').read_text())
    validate(data,evidence)
    a.review.with_name('report.html').write_text(build(data))
    result=subprocess.run(['node', str(Path(__file__).with_name('answers.js')),str(a.review)],check=True,capture_output=True,text=True)
    a.review.with_name('report.md').write_text(result.stdout)
    print(a.review.with_name('report.html').resolve())

if __name__=='__main__':
    main()
