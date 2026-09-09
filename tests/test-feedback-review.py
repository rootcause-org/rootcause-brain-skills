"""Critical contracts: weekly denominators and owner-confirmed, scope-safe exports."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/brain-fleet-report/scripts'))
import collect as fleet
from fr_common import tzinfo


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj

review=module('review_render',ROOT/'skills/brain-feedback-review/scripts/render.py')


class ReviewTests(unittest.TestCase):
    def test_weekly_counts_and_clusters(self):
        days=[date(2026,9,n) for n in range(1,8)]
        runs=[{'run_id':str(n),'created_at':f'2026-09-{n:02d}T10:00:00Z','day':f'2026-09-{n:02d}',
               'has_draft':True,'human_outcome':'edited','axis':'channel','axis_key':'mail'} for n in range(1,9)]
        runs[1]['excluded_reason']='test'
        tz=tzinfo('Europe/Brussels')
        total=fleet.count_period(days,runs,[],[],[],[],tz)
        self.assertEqual((total['runs'],total['counted'],total['drafts'],total['excluded']),(7,6,6,1))
        axes=fleet.per_axis_blocks(runs,[],[],[],[],days,tz,False)
        self.assertEqual(axes[0]['counted'],6)
        events=[{'run_id':str(n),'at':f'2026-09-{n:02d}T10:00:00Z','bucket':'real','cluster':'failure',
                 'command':'tool','stderr':'oops'} for n in range(1,9)]
        # A failed run on Monday belongs in the weekly focus, not only the Sunday totals.
        runs[0]['error']='failure';runs[0]['category']='timeout'
        clusters=fleet.build_clusters(runs,[],[],[],{},set(d.isoformat() for d in days),tz)
        self.assertEqual(next(c for c in clusters if c['kind']=='run_error')['focus_count'],1)

    def test_calendar_window_and_feedback_only(self):
        collector=module('review_collect',ROOT/'skills/brain-feedback-review/scripts/collect.py')
        collector.fetch_run=lambda rc,base,rid: {'base':base}
        class Rc:
            errors=[]
            def json(self,*args):
                if 'evidence' in args:
                    return {'feedback':[{'run_id':'inside','score':2,'score_set_at':'2026-09-08T21:59:00Z'},
                                        {'run_id':'today','score':1,'score_set_at':'2026-09-08T22:00:00Z'}],
                            'deltas':[{'related_run_id':'inside','sent_at':'2026-09-08T12:00:00Z','similarity':0.2}]}
                return {'runs':[]}
        now=datetime(2026,9,9,tzinfo=timezone.utc)
        start=datetime(2026,9,1,22,tzinfo=timezone.utc)
        end=datetime(2026,9,8,22,tzinfo=timezone.utc)
        data=collector.collect(Rc(),['--tenant','one'],7,True,now,start,end)
        self.assertEqual([i['run_id'] for i in data['items']],['inside'])
        self.assertEqual(data['items'][0]['deltas'],[])
        self.assertEqual(data['items'][0]['detail']['base'],['--tenant','one'])
        self.assertEqual(data['metrics']['scored'],1)

    def test_export_requires_confirmation(self):
        q={'id':'rule','text':'Change rule?','type':'rule','options':[
            {'value':'yes','label':'Yes','effect':'confirm','scope':'general','learning':'Use current source'},
            {'value':'customer','label':'Only customer','effect':'confirm','scope':'customer','learning':'Use current source'},
            {'value':'fine','label':'Fine','effect':'fine'}]}
        data={'project':'example','tenant':'','period':'week','items':[{'url':'https://app.replypen.com/runs/example','learning_allowed':True,'questions':[q]}]}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'review.json';a=Path(tmp)/'answers.json';p.write_text(json.dumps(data))
            def export(answers):
                a.write_text(json.dumps(answers))
                return subprocess.check_output(['node',str(ROOT/'skills/brain-feedback-review/scripts/answers.js'),str(p),str(a)],text=True)
            self.assertNotIn('[rule;',export({}))
            self.assertIn('[rule; scope=general]',export({'rule':{'choice':'yes'}}))
            self.assertNotIn('[rule;',export({'rule':{'choice':'customer'}}))
            self.assertNotIn('[rule; scope=customer]',export({'rule':{'choice':'customer','detail':'tenant one'}}))
            data['tenant']='one';p.write_text(json.dumps(data))
            self.assertIn('[rule; scope=customer]',export({'rule':{'choice':'customer','detail':'tenant one'}}))
            fine=export({'rule':{'choice':'fine'}})
            self.assertIn('## Niet wijzigen\n- Change rule?',fine)
            data['items'][0]['learning_allowed']=False;p.write_text(json.dumps(data))
            self.assertNotIn('[rule;',export({'rule':{'choice':'yes'}}))
            data['lang']='en';p.write_text(json.dumps(data))
            self.assertIn('## Confirmed lessons',export({}))
            self.assertNotIn('Bevestigde lessen',export({}))

    def test_missing_details_and_signed_links(self):
        collector=module('review_links',ROOT/'skills/brain-feedback-review/scripts/collect.py')
        url='https://app.replypen.com/runs/example?t=issued-test-token'
        self.assertEqual(collector.conversation_url('example',[{'metadata':{'run_url':url}}]),url)
        self.assertIsNone(collector.conversation_url('other',[{'run_url':url}]))
        self.assertIsNone(collector.conversation_url('example',[{'run_url':url.split('?')[0]}]))
        item={'url':url,'learning_allowed':True,'question':'Question','feedback':'Score 4',
              'proposed':None,'sent':'','availability_note':'Only feedback preserved.','questions':[]}
        data={'project':'example','tenant':'','owner':'Owner','period':'week','lang':'en','coverage_note':'Partial','items':[item]}
        html=review.build(data)
        self.assertNotIn('<details>',html)
        self.assertIn('href="'+url+'"',html)
        item['sent']='Actual human answer'
        self.assertEqual(review.build(data).count('<details>'),1)
        item['questions']=[{'id':'one','text':'Keep this rule?','options':[]}]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'review.json';path.write_text(json.dumps(data))
            md=subprocess.check_output(['node',str(ROOT/'skills/brain-feedback-review/scripts/answers.js'),str(path)],text=True)
            self.assertIn(url,md)
            self.assertIn('Only feedback preserved.',md)
            self.assertNotIn('Actual human answer',md)
        item['url']=None
        self.assertNotIn('>Open conversation',review.build(data))

    def test_unsafe_markup_is_inert(self):
        data={'project':'example','tenant':'','owner':'Owner','period':'week','lang':'nl','coverage_note':'</script><script>alert(1)</script>',
              'items':[]}
        html=review.build(data)
        self.assertNotIn('</script><script>alert(1)',html)
        self.assertIn('&lt;/script&gt;',html)
        data['tenant']='one'
        self.assertIn('/projects/example/tenants/one/brain-changes',review.build(data))

if __name__=='__main__':unittest.main()
