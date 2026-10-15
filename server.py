#!/usr/bin/env python3
"""Sozmarzhan shared word collection. Python standard library only."""
import argparse,base64,hashlib,hmac,json,os,re,secrets,sqlite3,time,threading
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
from http.cookies import SimpleCookie
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('SOZMARZHAN_DATA_DIR',str(ROOT/'data'))).resolve()
DATA.mkdir(parents=True,exist_ok=True)
def trusted_origins():
 # Only explicit deployment configuration and Render's own assigned URL are trusted.
 # Never infer HTTPS origins from client-supplied Host / X-Forwarded-* headers.
 origins=[]
 for key in ('SOZMARZHAN_PUBLIC_ORIGIN','RENDER_EXTERNAL_URL'):
  value=os.environ.get(key,'').strip()
  if not value:continue
  try:
   parsed=urlsplit(value)
   if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:continue
   port=parsed.port
   host=parsed.hostname.encode('idna').decode('ascii').lower()
   if ':' in host:host='['+host+']'
   origin=parsed.scheme+'://'+host+((':'+str(port)) if port and port!={'http':80,'https':443}[parsed.scheme] else '')
  except (ValueError,UnicodeError):continue
  if origin not in origins:origins.append(origin)
 return tuple(origins)
PUBLIC_ORIGINS=trusted_origins()
PUBLIC_ORIGIN=PUBLIC_ORIGINS[0] if PUBLIC_ORIGINS else ''
COOKIE_SECURE=any(origin.startswith('https://') for origin in PUBLIC_ORIGINS)
DB=DATA/'sozmarzhan.sqlite3'
LOCK=threading.Lock();LIMITS={}
def db():
 c=sqlite3.connect(DB,timeout=15);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

def setup_db():
 with db() as c:
  c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS words(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, word TEXT NOT NULL, meaning TEXT NOT NULL, example TEXT NOT NULL, region TEXT NOT NULL, speaker TEXT NOT NULL, audio BLOB, audio_type TEXT, status TEXT NOT NULL DEFAULT 'pending', reviewer TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS words_status_created ON words(status,created);
''')

def password_hash(password,salt):return hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()
def credentials():
 p=DATA/'moderator.json'
 env=os.environ.get('SOZMARZHAN_ADMIN_PASSWORD')
 if env:
  if len(env)<12:raise RuntimeError('SOZMARZHAN_ADMIN_PASSWORD must be at least 12 characters.')
  return env,None
 if not p.exists():raise RuntimeError('Мұғалім құпиясөзі орнатылмаған. Алдымен python server.py --setup орындаңыз.')
 return None,json.loads(p.read_text())
AUTH=None
SECRET_PATH=DATA/'receipt.key'
if not SECRET_PATH.exists():
 try:
  fd=os.open(SECRET_PATH,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32))
 except FileExistsError:pass
SECRET=SECRET_PATH.read_bytes()
def receipt(wid):return hmac.new(SECRET,('submission:'+wid).encode(),hashlib.sha256).hexdigest()
def token_hash(s):return hashlib.sha256(s.encode()).hexdigest()
def limited(key,max_count,period):
 now=time.time()
 with LOCK:
  if len(LIMITS)>10000:
   for k,(start,_) in list(LIMITS.items()):
    if now-start>3600:LIMITS.pop(k,None)
  start,n=LIMITS.get(key,(now,0))
  if now-start>period:start,n=now,0
  LIMITS[key]=(start,n+1)
  return n>=max_count

def clean_text(d,name,maximum,required=True):
 value=d.get(name,'')
 if not isinstance(value,str):raise ValueError(f'{name}: мәтін қажет.')
 value=value.strip()
 if (required and not value) or len(value)>maximum or '\x00' in value:raise ValueError(f'{name}: мәні бос немесе тым ұзын.')
 return value

def audio_bytes(value):
 if value in ('',None):return None,None
 if not isinstance(value,str) or len(value)>1500000:raise ValueError('Аудио көлемі 1 МБ-тан аспауы керек.')
 match=re.fullmatch(r'data:(audio/(?:mpeg|mp3|wav|x-wav|wave|ogg|webm|mp4|m4a|x-m4a|aac|flac));base64,([A-Za-z0-9+/=]+)',value,re.I)
 if not match:raise ValueError('Аудио форматы қолдау таппайды.')
 try:raw=base64.b64decode(match[2],validate=True)
 except Exception:raise ValueError('Аудио дерегі жарамсыз.')
 if not raw or len(raw)>1048576:raise ValueError('Аудио көлемі 1 МБ-тан аспауы керек.')
 mime=match[1].lower()
 valid=(raw.startswith(b'RIFF') and raw[8:12]==b'WAVE') if 'wav' in mime or mime.endswith('wave') else raw.startswith(b'OggS') if mime.endswith('ogg') else raw.startswith(bytes.fromhex('1a45dfa3')) if mime.endswith('webm') else raw[4:8]==b'ftyp' if mime.endswith(('mp4','m4a','x-m4a')) else raw.startswith(b'fLaC') if mime.endswith('flac') else raw.startswith(b'ID3') or (len(raw)>1 and raw[0]==255 and raw[1]&0xe0==0xe0)
 if not valid:raise ValueError('Аудио файлдың мазмұны мен түрі сәйкес емес.')
 return raw,mime

def public_row(row,private=False):
 keys=['id','word','meaning','example','region','speaker','reviewer','source','created','updated']
 if private:keys+=['status','note']
 out={k:row[k] for k in keys};out['audio_url']=f'/api/words/{row["id"]}/audio' if row['audio_type'] else ''
 return out

class Handler(BaseHTTPRequestHandler):
 server_version='Sozmarzhan/1.0'
 def log_message(self,fmt,*args):pass
 def base_headers(self):
  self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin');self.send_header('X-Frame-Options','DENY')
 def send_json(self,status,value,headers=None):
  body=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.base_headers();self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(body)
 def error(self,status,message):self.send_json(status,{'error':message})
 def body(self):
  if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON сұранысы қажет.')
  try:length=int(self.headers.get('Content-Length','0'))
  except ValueError:raise ValueError('Сұраныс көлемі жарамсыз.')
  if not 0<length<=1600000:raise ValueError('Сұраныс көлемі тым үлкен немесе бос.')
  self.connection.settimeout(15)
  try:data=json.loads(self.rfile.read(length))
  except Exception:raise ValueError('JSON дерегі жарамсыз.')
  if not isinstance(data,dict):raise ValueError('JSON объектісі қажет.')
  return data
 def origin_ok(self):
  origin=self.headers.get('Origin')
  expected=PUBLIC_ORIGINS or ('http://'+self.headers.get('Host',''),)
  return not origin or any(hmac.compare_digest(origin.encode(),value.encode()) for value in expected)
 def session(self):
  try:cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));token=cookie['sm_session'].value
  except Exception:return None
  with db() as c:row=c.execute('SELECT expires FROM sessions WHERE token_hash=?',(token_hash(token),)).fetchone()
  return token if row and row['expires']>int(time.time()) else None
 def owner(self,wid):
  auth=self.headers.get('Authorization','');return auth.startswith('Bearer ') and hmac.compare_digest(auth[7:],receipt(wid))
 def do_GET(self):
  try:self.get()
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:self.error(500,'Сервер қатесі. Қайта көріңіз.')
 def get(self):
  url=urlsplit(self.path);path=url.path;q=parse_qs(url.query)
  if path=='/api/health':return self.send_json(200,{'ok':True,'service':'sozmarzhan-shared'})
  if path=='/api/words':
   term=q.get('q',[''])[0].strip()[:120]
   try:limit=max(1,min(24,int(q.get('limit',['24'])[0])));offset=max(0,int(q.get('offset',['0'])[0]))
   except ValueError:return self.error(400,'Бет нөмірі жарамсыз.')
   # instr uses literal matches so user wildcards have no SQL semantics.
   where="status='approved'";params=[]
   if term:where+=' AND (instr(lower(word),lower(?))>0 OR instr(lower(meaning),lower(?))>0 OR instr(lower(region),lower(?))>0)';params=[term]*3
   with db() as c:
    # Python's lower handles Kazakh case; SQLite lower is ASCII-only.
    rows=c.execute('SELECT * FROM words WHERE status=\'approved\' ORDER BY created DESC,id').fetchall()
    if term:rows=[r for r in rows if any(term.casefold() in r[k].casefold() for k in ['word','meaning','region'])]
    total=len(rows);items=[public_row(r) for r in rows[offset:offset+limit]]
   return self.send_json(200,{'items':items,'total':total,'offset':offset,'limit':limit})
  match=re.fullmatch(r'/api/submissions/([a-f0-9]{32})',path)
  if match:
   wid=match[1]
   if not self.owner(wid):return self.error(404,'Жазба табылмады.')
   with db() as c:row=c.execute('SELECT * FROM words WHERE id=?',(wid,)).fetchone()
   if not row:return self.error(404,'Жазба табылмады.')
   return self.send_json(200,{'item':public_row(row,True)})
  match=re.fullmatch(r'/api/words/([a-f0-9]{32})/audio',path)
  if match:
   wid=match[1]
   with db() as c:row=c.execute('SELECT audio,audio_type,status FROM words WHERE id=?',(wid,)).fetchone()
   if not row or not row['audio'] or (row['status']!='approved' and not self.session() and not self.owner(wid)):return self.error(404,'Аудио табылмады.')
   raw=row['audio'];self.send_response(200);self.base_headers();self.send_header('Content-Type',row['audio_type']);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw);return
  if path=='/api/admin/session':return self.send_json(200,{'authenticated':bool(self.session())})
  if path=='/api/admin/words':
   if not self.session():return self.error(401,'Мұғалім ретінде кіріңіз.')
   status=q.get('status',['pending'])[0]
   if status not in ['pending','approved','rejected']:return self.error(400,'Мәртебе жарамсыз.')
   with db() as c:rows=c.execute('SELECT * FROM words WHERE status=? ORDER BY created DESC LIMIT 100',(status,)).fetchall()
   return self.send_json(200,{'items':[public_row(r,True) for r in rows]})
  static={'/':('index.html','text/html; charset=utf-8'),'/index.html':('index.html','text/html; charset=utf-8'),'/shared.js':('shared.js','text/javascript; charset=utf-8')}
  if path in static:
   filename,mime=static[path];raw=(ROOT/'public'/filename).read_bytes();self.send_response(200);self.base_headers();self.send_header('Content-Type',mime);self.send_header('Cache-Control','no-cache');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  self.error(404,'Бет табылмады.')
 def do_POST(self):
  if not self.origin_ok():return self.error(403,'Басқа сайттан жазуға болмайды.')
  try:self.post()
  except ValueError as e:self.error(400,str(e))
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:self.error(500,'Сервер қатесі. Қайта көріңіз.')
 def post(self):
  path=urlsplit(self.path).path
  if path=='/api/submissions':
   data=self.body()
   if data.get('publicConsent') is not True:raise ValueError('Ортақ қорға жариялауға келісім қажет.')
   request_id=clean_text(data,'requestId',100)
   if not re.fullmatch(r'[a-zA-Z0-9_-]{32,100}',request_id):raise ValueError('Ұсыныс коды жарамсыз.')
   with db() as c:existing=c.execute('SELECT id FROM words WHERE request_id=?',(request_id,)).fetchone()
   if existing:return self.send_json(200,{'id':existing['id'],'receipt':receipt(existing['id']),'duplicate':True})
   if limited(('submit',self.client_address[0]),60,3600):return self.error(429,'Ұсыныс лимиті аяқталды. Кейінірек көріңіз.')
   values=[clean_text(data,k,n) for k,n in [('word',80),('meaning',1500),('example',2000),('region',120),('speaker',100)]]
   audio,mime=audio_bytes(data.get('audio',''));wid=secrets.token_hex(16);now=int(time.time())
   try:
    with db() as c:c.execute('INSERT INTO words(id,request_id,word,meaning,example,region,speaker,audio,audio_type,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(wid,request_id,*values,audio,mime,now,now))
   except sqlite3.IntegrityError:
    with db() as c:wid=c.execute('SELECT id FROM words WHERE request_id=?',(request_id,)).fetchone()['id']
   return self.send_json(201,{'id':wid,'receipt':receipt(wid),'status':'pending'})
  if path=='/api/admin/login':
   if limited(('login',self.client_address[0]),10,600):return self.error(429,'Кіру әрекеті тым көп. 10 минуттан кейін көріңіз.')
   data=self.body();password=data.get('password','')
   if not isinstance(password,str) or len(password)>1024:raise ValueError('Құпиясөз жарамсыз.')
   env,stored=AUTH
   valid=hmac.compare_digest(password.encode(),env.encode()) if env else hmac.compare_digest(password_hash(password,bytes.fromhex(stored['salt'])),stored['hash'])
   if not valid:return self.error(401,'Құпиясөз дұрыс емес.')
   token=secrets.token_hex(32);now=int(time.time())
   with db() as c:c.execute('DELETE FROM sessions WHERE expires<?',(now,));c.execute('INSERT INTO sessions VALUES(?,?)',(token_hash(token),now+28800))
   cookie='sm_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'+('; Secure' if COOKIE_SECURE else '')
   return self.send_json(200,{'authenticated':True},{'Set-Cookie':cookie})
  if path=='/api/admin/logout':
   self.body();token=self.session()
   if token:
    with db() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash(token),))
   return self.send_json(200,{'authenticated':False},{'Set-Cookie':'sm_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'+('; Secure' if COOKIE_SECURE else '')})
  match=re.fullmatch(r'/api/admin/words/([a-f0-9]{32})',path)
  if match:
   if not self.session():return self.error(401,'Мұғалім ретінде кіріңіз.')
   data=self.body();status=data.get('status')
   if status not in ['approved','rejected','pending']:raise ValueError('Мәртебе жарамсыз.')
   reviewer=clean_text(data,'reviewer',80);source=clean_text(data,'source',300,status=='approved');note=clean_text(data,'note',500,status=='rejected')
   with db() as c:
    row=c.execute('SELECT * FROM words WHERE id=?',(match[1],)).fetchone()
    if not row:return self.error(404,'Ұсыныс табылмады.')
    values=[clean_text(data,k,n) if k in data else row[k] for k,n in [('word',80),('meaning',1500),('example',2000)]]
    c.execute('UPDATE words SET word=?,meaning=?,example=?,status=?,reviewer=?,source=?,note=?,updated=? WHERE id=?',(*values,status,reviewer,source,note,int(time.time()),match[1]))
   return self.send_json(200,{'ok':True,'status':status})
  self.error(404,'Бет табылмады.')

def main():
 global AUTH
 parser=argparse.ArgumentParser();parser.add_argument('--setup',action='store_true');parser.add_argument('--host',default=os.environ.get('HOST','0.0.0.0'));parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8080')));args=parser.parse_args()
 if args.setup:
  import getpass
  a=getpass.getpass('Мұғалімнің жаңа құпиясөзі (кемінде 12 таңба): ');b=getpass.getpass('Құпиясөзді қайталаңыз: ')
  if a!=b or len(a)<12:raise SystemExit('Құпиясөздер бірдей және кемінде 12 таңба болуы керек.')
  salt=secrets.token_bytes(16);p=DATA/'moderator.json';p.write_text(json.dumps({'salt':salt.hex(),'hash':password_hash(a,salt)}));p.chmod(0o600)
  setup_db()
  with db() as c:c.execute('DELETE FROM sessions')
  print('Құпиясөз сақталды. Іске қосу: python server.py');return
 AUTH=credentials();setup_db();server=ThreadingHTTPServer((args.host,args.port),Handler);server.daemon_threads=True
 print(f'Сөзмаржан іске қосылды: http://localhost:{server.server_port}',flush=True)
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
if __name__=='__main__':main()
