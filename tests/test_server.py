import sys,unittest,threading,tempfile,os,json,urllib.request,urllib.error,http.cookiejar,base64,io,wave,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
temp=tempfile.TemporaryDirectory();os.environ['SOZMARZHAN_DATA_DIR']=temp.name;os.environ['SOZMARZHAN_ADMIN_PASSWORD']='test-only-Құпиясөз-2026'
import server
class Client:
 def __init__(self):self.jar=http.cookiejar.CookieJar();self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
 def call(self,path,data=None,headers=None):
  h={'Content-Type':'application/json'} if data is not None else {};h.update(headers or {});req=urllib.request.Request(URL+path,data=json.dumps(data).encode() if data is not None else None,headers=h)
  try:r=self.opener.open(req)
  except urllib.error.HTTPError as e:r=e
  raw=r.read()
  try:body=json.loads(raw)
  except Exception:body=raw
  return r.status,body,r.headers
class SharedTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  global URL
  server.AUTH=server.credentials();server.setup_db();cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);URL='http://127.0.0.1:'+str(cls.http.server_port);cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
 @classmethod
 def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();temp.cleanup()
 def setUp(self):
  self.a=Client();self.b=Client();self.teacher=Client();server.LIMITS.clear()
 def payload(self):return dict(requestId=server.secrets.token_hex(16),word='Борық',meaning='Қамыс тәрізді өсімдік.',example='Көлден тартты борықты.',region='Ақсеңгір',speaker='Әжем',publicConsent=True)
 def login(self):self.assertEqual(self.teacher.call('/api/admin/login',{'password':os.environ['SOZMARZHAN_ADMIN_PASSWORD']})[0],200)
 def test_end_to_end_two_visitors_moderation_audio_and_retraction(self):
  stream=io.BytesIO()
  with wave.open(stream,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*100)
  data=self.payload();data['audio']='data:audio/wav;base64,'+base64.b64encode(stream.getvalue()).decode()
  status,created,_=self.a.call('/api/submissions',data);self.assertEqual(status,201);wid=created['id']
  self.assertFalse(any(x['id']==wid for x in self.b.call('/api/words')[1]['items']))
  self.assertEqual(self.b.call('/api/words/'+wid+'/audio')[0],404)
  self.assertEqual(self.b.call('/api/admin/words')[0],401)
  self.assertEqual(self.b.call('/api/submissions/'+wid)[0],404)
  auth={'Authorization':'Bearer '+created['receipt']}
  self.assertEqual(self.a.call('/api/submissions/'+wid,headers=auth)[1]['item']['status'],'pending')
  self.assertEqual(self.a.call('/api/words/'+wid+'/audio',headers=auth)[1],stream.getvalue())
  self.login();approve={'status':'approved','reviewer':'Мұғалім','source':'7-сынып, 14-б.','note':''}
  self.assertEqual(self.teacher.call('/api/admin/words/'+wid,approve)[0],200)
  published=self.b.call('/api/words?q=%D0%91%D0%9E%D0%A0%D0%AB%D2%9A')[1]['items'];item=next(x for x in published if x['id']==wid)
  self.assertEqual(item['source'],'7-сынып, 14-б.');self.assertNotIn('note',item);self.assertNotIn('request_id',item)
  self.assertEqual(self.b.call(item['audio_url'])[1],stream.getvalue())
  self.assertEqual(self.a.call('/api/submissions/'+wid,headers=auth)[1]['item']['status'],'approved')
  with sqlite3.connect(server.DB) as c:self.assertEqual(c.execute('SELECT word FROM words WHERE id=?',(wid,)).fetchone()[0],'Борық')
  self.assertEqual(self.teacher.call('/api/admin/words/'+wid,{**approve,'status':'pending'})[0],200)
  self.assertFalse(any(x['id']==wid for x in self.b.call('/api/words')[1]['items']));self.assertEqual(self.b.call(item['audio_url'])[0],404)
  self.teacher.call('/api/admin/logout',{});self.assertEqual(self.teacher.call('/api/admin/words')[0],401)
 def test_consent_auth_validation_csrf_and_idempotency(self):
  data=self.payload();self.assertEqual(self.a.call('/api/submissions',{**data,'publicConsent':False})[0],400)
  self.assertEqual(self.a.call('/api/submissions',data,{'Origin':'https://other-site.example'})[0],403)
  self.assertEqual(self.a.call('/api/submissions',{**data,'audio':'data:audio/wav;base64,PGh0bWw+'})[0],400)
  first=self.a.call('/api/submissions',data)[1];again=self.a.call('/api/submissions',data)[1];self.assertEqual(first['id'],again['id']);self.assertEqual(first['receipt'],again['receipt'])
  self.assertEqual(self.b.call('/api/admin/words/'+first['id'],{'status':'approved'})[0],401)
  self.login();self.assertEqual(self.teacher.call('/api/admin/words/'+first['id'],{'status':'approved','reviewer':'Teacher','source':''})[0],400)
  self.assertEqual(self.teacher.call('/api/admin/words/'+first['id'],{'status':'rejected','reviewer':'Teacher','note':'Мағынасын нақтылау керек.'})[0],200)
  self.assertEqual(self.a.call('/api/submissions/'+first['id'],headers={'Authorization':'Bearer '+first['receipt']})[1]['item']['status'],'rejected')
 def test_static_routes_and_health(self):
  self.assertTrue(self.b.call('/api/health')[1]['ok']);status,html,_=self.b.call('/');self.assertEqual(status,200);self.assertIn('sharedCollection'.encode(),html);self.assertEqual(self.b.call('/shared.js')[0],200);self.assertEqual(self.b.call('/data/moderator.json')[0],404);self.assertEqual(self.b.call('/../server.py')[0],404)
if __name__=='__main__':unittest.main()
