import base64
import os
import secrets
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from outreach import create_app, delivery, gmail_test
from outreach.db import scope


class GmailDiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.password=secrets.token_urlsafe(32)
        self.mailbox='sender@leed.test'
        self.recipient='owned@recipient.test'
        self.enterContext(patch.dict(os.environ, {
            'LEED_DB':str(Path(directory)/'app.db'),'LEED_ADMIN_PASSWORD':self.password,
            'LEED_SECRET_KEY':secrets.token_urlsafe(40),'GMAIL_CLIENT_ID':'fixture','GMAIL_CLIENT_SECRET':'fixture',
            'GMAIL_REFRESH_TOKEN':'fixture','GMAIL_REDIRECT_URI':'https://testserver/oauth/gmail/callback',
            'SENDER_EMAIL':self.mailbox,'GMAIL_TEST_RECIPIENTS':self.recipient,'OUTREACH_MODE':'disabled',
        }))
        self.app=create_app()
        self.client=self.login()
        self.sent=[]
        self.failure=False
        self.enterContext(patch.object(delivery,'api',side_effect=self.provider))

    def login(self):
        c=self.enterContext(TestClient(self.app,base_url='https://testserver'))
        self.assertEqual(c.post('/login',data={'username':'admin','password':self.password},follow_redirects=False).status_code,303)
        c.headers['X-CSRF-Token']=c.get('/api/gmail/setup').json()['csrf']
        return c

    def provider(self,method,url,token='',body=None,form=False):
        if url=='https://oauth2.googleapis.com/token': return {'access_token':'fixture'}
        if url==delivery.GMAIL+'/profile': return {'emailAddress':self.mailbox}
        if url==delivery.GMAIL+'/messages/send':
            self.sent.append(BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(body['raw'])))
            if self.failure: raise delivery.ProviderError('fixture-secret-must-not-leak')
            return {'id':'aa11','threadId':'bb22'}
        if url==delivery.GMAIL+'/threads/bb22?format=metadata&metadataHeaders=From':
            return {'id':'bb22','messages':[
                {'id':'aa11','labelIds':['SENT']},
                {'id':'cc33','payload':{'headers':[{'name':'From','value':'Owner <'+self.recipient+'>'}]}},
                {'id':'cc33','payload':{'headers':[{'name':'From','value':self.recipient}]}},
                {'id':'dd44','payload':{'headers':[{'name':'From','value':'unrelated@recipient.test'}]}},
            ]}
        raise AssertionError('Unexpected provider endpoint '+url)

    def preview(self):
        r=self.client.post('/api/gmail/test/preview',json={'recipient':self.recipient})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def send(self,preview,client=None,**overrides):
        return (client or self.client).post('/api/gmail/test/send',json={
            'id':preview['id'],'snapshot':preview['snapshot'],'confirm':True,**overrides})

    def test_real_transport_fixed_message_one_recipient_and_no_sales_records(self):
        p=self.preview()
        self.assertFalse(self.sent)
        self.assertEqual(self.send(p).json()['status'],'sent')
        self.assertEqual(len(self.sent),1)
        mail=self.sent[0]
        self.assertEqual(mail['To'],self.recipient)
        self.assertEqual(mail['From'],self.mailbox)
        self.assertEqual(mail['Subject'],gmail_test.SUBJECT)
        self.assertEqual(mail.get_content().strip(),gmail_test.BODY)
        self.assertIsNone(mail['Cc']); self.assertIsNone(mail['Bcc'])
        self.assertEqual(self.client.get('/api/delivery').json()['mode'],'disabled')
        with scope() as db:
            for table in ('companies','campaigns','contacts','jobs','replies'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            self.assertTrue(db.execute("SELECT 1 FROM audit_log WHERE action='gmail.test_sent'").fetchone())

    def test_allowlist_opt_in_approval_csrf_and_login(self):
        self.assertEqual(self.client.post('/api/gmail/test/preview',json={'recipient':'other@recipient.test'}).status_code,403)
        with patch.dict(os.environ,{'GMAIL_TEST_RECIPIENTS':''}):
            self.assertEqual(self.client.get('/api/gmail/test/status').json()['recipients'],[])
            self.assertEqual(self.client.post('/api/gmail/test/preview',json={'recipient':self.recipient}).status_code,403)
        p=self.preview()
        self.assertEqual(self.send(p,confirm=False).status_code,400)
        self.assertEqual(self.send(p,snapshot='tampered').status_code,409)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.send(p).status_code,403)
        with TestClient(self.app,base_url='https://testserver') as guest:
            self.assertEqual(guest.get('/api/gmail/test/status').status_code,401)
            self.assertEqual(guest.post('/api/gmail/test/send',json={}).status_code,401)
        self.assertFalse(self.sent)

    def test_expired_preview_connection_and_sender_changes(self):
        p=self.preview()
        with scope() as db: db.execute('UPDATE gmail_diagnostics SET expires=0')
        self.assertEqual(self.send(p).status_code,409)
        p=self.preview()
        with patch.dict(os.environ,{'GMAIL_REFRESH_TOKEN':'changed'}):
            self.assertEqual(self.send(p).status_code,409)
        with patch.dict(os.environ,{'SENDER_EMAIL':'other@leed.test'}):
            self.assertEqual(self.send(p).status_code,409)
        self.assertFalse(self.sent)

    def test_allowlist_removal_and_suppression_after_preview(self):
        p=self.preview()
        with patch.dict(os.environ,{'GMAIL_TEST_RECIPIENTS':''}):
            self.assertEqual(self.send(p).status_code,403)
        with scope() as db:
            cid=db.execute("INSERT INTO companies(name,normalized_name,domain) VALUES('Fixture','fixture','recipient.test')").lastrowid
            db.execute("INSERT INTO contacts(company_id,kind,value,source) VALUES(?,'email',?,'https://recipient.test')",(cid,self.recipient))
            db.execute("INSERT INTO suppressions(company_id,reason) VALUES(?,'stop')",(cid,))
        self.assertEqual(self.send(p).status_code,409)
        self.assertFalse(self.sent)

    def test_replay_after_success_and_process_restart_never_resends(self):
        p=self.preview()
        self.assertEqual(self.send(p).json()['status'],'sent')
        self.assertEqual(self.send(p).json()['status'],'sent')
        self.app=create_app()
        again=self.login()
        self.assertEqual(self.send(p,again).json()['status'],'sent')
        self.assertEqual(again.post('/api/gmail/test/preview',json={'recipient':self.recipient}).status_code,409)
        self.assertEqual(len(self.sent),1)

    def test_ambiguous_result_and_interrupted_claim_never_resends(self):
        p=self.preview(); self.failure=True
        result=self.send(p)
        self.assertEqual(result.json()['status'],'unknown')
        self.assertNotIn('fixture-secret',result.text)
        self.assertEqual(self.send(p).json()['status'],'unknown')
        with scope() as db: db.execute("UPDATE gmail_diagnostics SET status='sending'")
        self.assertEqual(self.send(p).json()['status'],'sending')
        self.assertEqual(len(self.sent),1)

    def test_two_sessions_concurrently_send_only_one_message(self):
        p=self.preview(); other=self.login(); barrier=threading.Barrier(2)
        def send(client):
            barrier.wait(timeout=10)
            return self.send(p,client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(send,[self.client,other]))
        self.assertTrue(all(r.status_code==200 for r in results),[r.text for r in results])
        self.assertEqual(len(self.sent),1)

    def test_thread_reply_detection_deduplicated_and_wrong_mailbox_blocked(self):
        p=self.preview(); self.send(p)
        for _ in range(2):
            result=self.client.post('/api/gmail/test/replies',json={'id':p['id']})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['reply_count'],1)
        self.mailbox='other@leed.test'
        self.assertEqual(self.client.post('/api/gmail/test/replies',json={'id':p['id']}).status_code,409)
        self.assertEqual(len(self.sent),1)

    def test_template_injection_cannot_change_approved_message_or_recipient(self):
        p=self.preview()
        self.send(p,recipient='other@recipient.test',subject='tampered',body='tampered')
        self.assertEqual(self.sent[0]['To'],self.recipient)
        self.assertEqual(self.sent[0].get_content().strip(),gmail_test.BODY)

    def test_malformed_allowlist_fails_closed(self):
        with patch.dict(os.environ,{'GMAIL_TEST_RECIPIENTS':'a@b.test\r\nBcc: other@b.test'}):
            self.assertEqual(self.client.get('/api/gmail/test/status').status_code,503)
        self.assertFalse(self.sent)
