#!/usr/bin/env python3
"""Explicit no-bank TEST of browser WebAuthn, not real LLM validation/biometrics.

Run from a checkout with the gateway dependencies installed. Start two isolated
servers (never use an existing live account server):

  env -u DCTA_ALLOW_UNVALIDATED -u PARSER_BACKEND VALIDATOR_BACKEND=stub \
    DCTA_NO_BANK=1 DCTA_NO_CLIENT=1 DCTA_RP_ID=localhost \
    DCTA_ORIGIN=http://localhost:8880 python -m uvicorn \
    backend.gateway.server:app --host 127.0.0.1 --port 8880
  env -u DCTA_ALLOW_UNVALIDATED -u PARSER_BACKEND -u VALIDATOR_BACKEND \
    DCTA_NO_BANK=1 DCTA_NO_CLIENT=1 DCTA_RP_ID=localhost \
    DCTA_ORIGIN=http://localhost:8881 python -m uvicorn \
    backend.gateway.server:app --host 127.0.0.1 --port 8881

Start a dedicated installed Chromium/headless-shell process with:
  --remote-debugging-port=9337 --remote-allow-origins=http://localhost:9337
  --user-data-dir=/tmp/dcta-webauthn-browser-test about:blank
Then run this script with Python 3. It creates a CDP virtual CTAP2 authenticator
and invokes actual navigator.credentials.create/get and real gateway routes.
No injected validator pass, hand-forged valid signature, or external bank is
used. The default-freeze server gets a rejection-only execution attempt.
The script closes its dedicated browser and writes a JSON report to /tmp.
"""
import base64, hashlib, json, os, socket, struct, time, urllib.request
from urllib.parse import urlparse
pages=[json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:9337/json/new?about:blank',method='PUT')))]
u=urlparse(next(p['webSocketDebuggerUrl'] for p in pages if p['type']=='page'))
s=socket.create_connection((u.hostname,u.port)); s.settimeout(15)
key=base64.b64encode(os.urandom(16)).decode()
s.sendall((f'GET {u.path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: http://localhost:9337\r\n\r\n').encode())
h=b''
while not h.endswith(b'\r\n\r\n'): h+=s.recv(1)
assert h.startswith(b'HTTP/1.1 101 '),h
seq=0; events=[]
def read(n):
 b=b''
 while len(b)<n: b+=s.recv(n-len(b))
 return b
def receive():
 a,b=read(2); n=b&127
 if n==126:n=struct.unpack('!H',read(2))[0]
 elif n==127:n=struct.unpack('!Q',read(8))[0]
 mask=read(4) if b&128 else None
 data=read(n)
 if mask:data=bytes(c^mask[i%4] for i,c in enumerate(data))
 return json.loads(data)
def call(method,params={}):
 global seq
 seq+=1; data=json.dumps({'id':seq,'method':method,'params':params}).encode(); mask=os.urandom(4)
 header=bytes([129,128|len(data)]) if len(data)<126 else bytes([129,254])+struct.pack('!H',len(data)) if len(data)<65536 else bytes([129,255])+struct.pack('!Q',len(data))
 s.sendall(header+mask+bytes(c^mask[i%4] for i,c in enumerate(data)))
 while True:
  msg=receive()
  if msg.get('id')==seq:
   assert 'error' not in msg,msg
   return msg.get('result',{})
  events.append(msg)
def evaluate(js):
 r=call('Runtime.evaluate',{'expression':js,'awaitPromise':True,'returnByValue':True,'userGesture':True})
 assert 'exceptionDetails' not in r,r
 return r['result'].get('value')
from pathlib import Path
print('TEST ONLY: Chromium virtual authenticator + explicit deterministic StubValidator + no bank; NOT LLM validation, hardware Face ID or a real transfer.',flush=True)
call('Runtime.enable');call('Page.enable');call('Network.enable');call('WebAuthn.enable')
auth=call('WebAuthn.addVirtualAuthenticator',{'options':{'protocol':'ctap2','transport':'internal','hasResidentKey':True,'hasUserVerification':True,'isUserVerified':True,'automaticPresenceSimulation':True}})['authenticatorId']
call('Page.addScriptToEvaluateOnNewDocument',{'source':'''window.__errors=[];window.__requests=[];window.__responses=[];window.__getCount=0;
window.addEventListener('error',e=>__errors.push(e.message));window.addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));
window.b64=function(bytes){return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'')};
const originalGet=navigator.credentials.get.bind(navigator.credentials);navigator.credentials.get=async function(options){__getCount++;const a=await originalGet(options);window.__assertion={credential_id:b64(a.rawId),authenticator_data:b64(a.response.authenticatorData),client_data_json:b64(a.response.clientDataJSON),signature:b64(DctaPasskeys.derToRaw(a.response.signature))};return a};
const originalCreate=navigator.credentials.create.bind(navigator.credentials);navigator.credentials.create=async function(options){const c=await originalCreate(options);window.__createdId=b64(c.rawId);return c};
const originalFetch=window.fetch;window.fetch=async function(url,options){__requests.push({url:String(url),body:options?.body?JSON.parse(options.body):null});const r=await originalFetch.apply(this,arguments);let b=await r.clone().json().catch(()=>null);__responses.push({url:String(url),status:r.status,body:b});if(window.__tamperNext && url==='/api/message' && r.ok){window.__tamperNext=false;b.items[0].draft.amount.value='51.00';return new Response(JSON.stringify(b),{status:200,headers:{'Content-Type':'application/json'}})}return r};'''})
checks=[];report={}
def check(name,value):
 assert value,name
 checks.append(name);print('PASS',name,flush=True)
def wait(expression):
 for _ in range(160):
  if evaluate(expression):return
  time.sleep(.05)
 raise AssertionError(expression+'; page='+str(evaluate('({errors:__errors,passkey:document.querySelector("#passkey-status")?.textContent,card:document.querySelector(".passkey-card-status")?.textContent})')))
def navigate(origin):
 call('Page.navigate',{'url':origin})
 wait("document.readyState==='complete' && !!window.DctaPasskeys && !document.querySelector('#register-passkey').disabled")
def send(text):
 evaluate("document.querySelector('#text-input').value="+json.dumps(text)+";document.querySelector('#send-btn').click()")
 wait("!!document.querySelector('.js-passkey-sign') && !document.querySelector('#send-btn').disabled")
def register():
 evaluate("document.querySelector('#register-passkey').click()")
 wait("__responses.some(r=>r.url==='/api/webauthn/register') && !document.querySelector('#register-passkey').disabled")
 r=evaluate("__responses.find(r=>r.url==='/api/webauthn/register')")
 check('Registration preserves browser rawId',r['status']==200 and r['body']['credential_id_source']=='client' and r['body']['credential_id']==evaluate('__createdId'))
 ids=[c['credentialId'].rstrip('=') for c in call('WebAuthn.getCredentials',{'authenticatorId':auth})['credentials']]
 check('Chromium authenticator owns registered credential',r['body']['credential_id'] in [x.replace('+','-').replace('/','_') for x in ids])
 return r
def post(path,body):
 return evaluate('(async()=>{const r=await fetch('+json.dumps(path)+',{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify('+json.dumps(body)+')});return {status:r.status,body:await r.json()}})()')
def sign():
 evaluate("document.querySelector('.js-passkey-sign').click()")
 wait("!document.querySelector('.js-passkey-execute').disabled")
 return evaluate('__assertion')
# Each entire ceremony is same-origin. Refuse to execute against anything but the isolated test configuration.
navigate('http://localhost:8880/')
health=evaluate("__responses.find(r=>r.url==='/api/health').body")
check('Explicit isolated test validator, no client and no bank',health['validator']=='StubValidator' and health['client'] is None and health['bank']=='none')
check('UI clearly labels deterministic test mode',evaluate("document.querySelector('#validation-mode').textContent.includes('TEST / DEMO')"))
report['registration']=register()
send('Send fifty dollars to John Smith.')
check('Stub validator actually compared draft and allowed policy',evaluate("__responses.filter(r=>r.url==='/api/message').at(-1).body.items[0].validator.verdict==='pass' && __responses.filter(r=>r.url==='/api/message').at(-1).body.items[0].policy.decision==='allow'"))
assertion=sign()
chal=evaluate("__responses.filter(r=>r.url==='/api/webauthn/challenge').at(-1)")
message=evaluate("__responses.filter(r=>r.url==='/api/message').at(-1).body.items[0].draft")
localHash=evaluate('canonical.draftHashAsync('+json.dumps(message)+')')
check('Local canonical hash equals gateway draft hash',localHash==chal['body']['draft_hash'])
check('Challenge offers the browser-owned credential',report['registration']['body']['credential_id'] in chal['body']['allow_credentials'])
check('Signing does not execute',evaluate("!__requests.some(r=>r.url==='/api/execute')"))
check('Assertion belongs to the same origin',json.loads(base64.urlsafe_b64decode(assertion['client_data_json']+'=='))['origin']=='http://localhost:8880')
report['challenge']={'status':chal['status'],'local_hash':localHash,**chal['body']}
# Capture the production request from the deliberate execution click.
evaluate("document.querySelector('.js-passkey-execute').click()")
wait("document.querySelector('.js-passkey-execute').textContent==='Executed'")
execution=evaluate("__responses.filter(r=>r.url==='/api/execute').at(-1)")
body=evaluate("__requests.filter(r=>r.url==='/api/execute').at(-1).body")
report['test_execution']=execution
check('Gateway verified ES256 assertion and recorded no-bank TEST execution',execution['status']==200 and execution['body']['result']=='executed')
retry=post('/api/execute',body)
check('Identical idempotent retry returns original transaction',retry['status']==200 and retry['body']==execution['body'])
# Tampered challenge and signature go through the real HTTP gateway.
tampered=json.loads(json.dumps(body));cdj=json.loads(base64.urlsafe_b64decode(tampered['assertion']['client_data_json']+'=='));cdj['challenge']='bad-challenge';tampered['assertion']['client_data_json']=base64.urlsafe_b64encode(json.dumps(cdj).encode()).decode().rstrip('=')
report['tampered_challenge']=post('/api/execute',tampered)
check('Tampered challenge rejected hash_mismatch',report['tampered_challenge']['status']==422 and report['tampered_challenge']['body']['reason_code']=='hash_mismatch')
tampered=json.loads(json.dumps(body));sig=bytearray(base64.urlsafe_b64decode(tampered['assertion']['signature']+'=='));sig[0]^=1;tampered['assertion']['signature']=base64.urlsafe_b64encode(sig).decode().rstrip('=')
report['tampered_signature']=post('/api/execute',tampered)
check('Tampered signature rejected signature_invalid',report['tampered_signature']['status']==422 and report['tampered_signature']['body']['reason_code']=='signature_invalid')
report['injected_draft']=post('/api/execute',{**body,'draft':message})
check('Client-supplied draft body rejected malformed_request',report['injected_draft']['status']==422 and report['injected_draft']['body']['reason_code']=='malformed_request')
# A second actual assertion for a different real stored draft, then cross-draft key reuse.
send('Send thirty dollars to John Smith.')
second=sign();secondDraft=evaluate("__responses.filter(r=>r.url==='/api/message').at(-1).body.items[0].draft")
report['cross_draft_replay']=post('/api/execute',{'draft_id':secondDraft['id'],'credential_id':second['credential_id'],'assertion':second,'idempotency_key':body['idempotency_key']})
check('Cross-draft reused key rejected replayed',report['cross_draft_replay']['status']==422 and report['cross_draft_replay']['body']['reason_code']=='replayed')
# Replay prevention must bind the signed draft, not only the caller's request key.
report['fresh_key_same_assertion']=post('/api/execute',{**body,'idempotency_key':body['idempotency_key']+'-fresh-key-test'})
check('Same signed draft with fresh key rejected replayed',report['fresh_key_same_assertion']['status']==422 and report['fresh_key_same_assertion']['body']['reason_code']=='replayed')
# Display tampering must stop before navigator.credentials.get, even when server verdict passed.
evaluate('window.__beforeGet=__getCount;window.__tamperNext=true')
send('Send fifty dollars to John Smith.')
evaluate("document.querySelector('.js-passkey-sign').click()")
wait("document.querySelector('.passkey-card-status').textContent.includes('hash_mismatch')")
check('Displayed hash mismatch stops before authenticator',evaluate('__getCount===__beforeGet'))
# Default validator stays fail-closed, including a valid authenticator assertion constructed by the test harness.
navigate('http://localhost:8881/')
check('Default rejection test is isolated and has no bank',evaluate("__responses.find(r=>r.url==='/api/health').body.validator==='UnconfiguredValidator' && __responses.find(r=>r.url==='/api/health').body.bank==='none'"))
report['default_registration']=register()
send('Send fifty dollars to John Smith.')
check('Default freeze disables Sign and Confirm execution',evaluate("document.querySelector('.js-passkey-sign').disabled && document.querySelector('.js-passkey-execute').disabled && document.querySelector('.badge').textContent==='Validation blocked'"))
frozen=evaluate("__responses.filter(r=>r.url==='/api/message').at(-1).body.items[0].draft")
report['frozen_challenge']=post('/api/webauthn/challenge',{'draft_id':frozen['id']})
check('Default server refuses challenge validator_frozen',report['frozen_challenge']['status']==422 and report['frozen_challenge']['body']['reason_code']=='validator_frozen')
frozenId=report['default_registration']['body']['credential_id']
evaluate('(async()=>{const hash=await canonical.draftHashAsync('+json.dumps(frozen)+');await navigator.credentials.get({publicKey:{rpId:"localhost",challenge:Uint8Array.from(hash.match(/../g),x=>parseInt(x,16)),userVerification:"required",allowCredentials:[{type:"public-key",id:Uint8Array.from(atob('+json.dumps(frozenId)+'.replace(/-/g,"+").replace(/_/g,"/")),x=>x.charCodeAt(0))}]}});return true})()')
frozenAssertion=evaluate('__assertion')
report['frozen_execute']=post('/api/execute',{'draft_id':frozen['id'],'credential_id':frozenId,'assertion':frozenAssertion,'idempotency_key':'frozen-rejection-only'})
check('Valid crypto cannot bypass default validator_frozen',report['frozen_execute']['status']==422 and report['frozen_execute']['body']['reason_code']=='validator_frozen')
check('No browser errors',not evaluate('__errors'))
for source in ['frontend/app.js','frontend/js/webauthn.js']:
 check('Browser syntax '+source,evaluate('typeof new Function('+json.dumps((Path(__file__).resolve().parents[1] / source).read_text())+")==='function'"))
report['checks']=checks
Path('/tmp/dcta-webauthn-browser-result.json').write_text(json.dumps(report,indent=2))
print('REPORT',json.dumps(report,indent=2),flush=True)
call('Browser.close')
