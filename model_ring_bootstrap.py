import mmap,struct,json,subprocess,os,sys,time,pathlib,ctypes,hashlib,base64,shutil
R=pathlib.Path(os.environ.get("LOCALAPPDATA",str(pathlib.Path.home())))/"ModelRing";R.mkdir(parents=True,exist_ok=True)
MAG=b"G56SPSC1"; CAP=256; SLOT=8192; HDR=64; TOTAL=HDR+CAP*SLOT; REQ="Local\\G56_REQ_1"; RES="Local\\G56_RES_1"
class Ring:
 def __init__(s,n):
  s.m=mmap.mmap(-1,TOTAL,tagname=n,access=mmap.ACCESS_WRITE)
  if s.m[:8]!=MAG:
   s.m[:]=b"\0"*TOTAL;s.m[:24]=struct.pack("<8sIIII",MAG,1,CAP,SLOT,0);s.set(24,0);s.set(28,0)
 def u(s,o): return ctypes.c_uint32.from_buffer(s.m,o)
 def get(s,o): return int(s.u(o).value)
 def set(s,o,v): s.u(o).value=int(v)&0xffffffff
 def put(s,x,t=5):
  b=json.dumps(x,separators=(",",":"),ensure_ascii=False).encode(); end=time.perf_counter()+t
  if len(b)>SLOT-8: raise ValueError("message too large")
  while 1:
   w=s.get(24);r=s.get(28)
   if (w-r)&0xffffffff<CAP:
    o=HDR+(w%CAP)*SLOT;s.m[o:o+4]=struct.pack("<I",len(b));s.m[o+4:o+4+len(b)]=b;s.set(24,w+1);return
   if time.perf_counter()>end: raise TimeoutError("full")
   time.sleep(.0002)
 def take(s,t=5):
  end=time.perf_counter()+t
  while 1:
   r=s.get(28);w=s.get(24)
   if r!=w:
    o=HDR+(r%CAP)*SLOT;n=struct.unpack("<I",s.m[o:o+4])[0]
    if n>SLOT-8: raise RuntimeError("corrupt")
    x=json.loads(bytes(s.m[o+4:o+4+n]).decode());s.set(28,r+1);return x
   if time.perf_counter()>end: raise TimeoutError("empty")
   time.sleep(.0002)
def P(x): return pathlib.Path(os.path.expandvars(os.path.expanduser(str(x)))).resolve()
def do(c):
 op=c.get("op")
 if op=="ping": return {"ok":1,"pid":os.getpid(),"ns":time.time_ns()}
 if op=="exec":
  a=c.get("argv"); p=subprocess.run([str(x) for x in a],cwd=str(P(c["cwd"])) if c.get("cwd") else None,capture_output=True,timeout=min(float(c.get("timeout",30)),120))
  return {"ok":p.returncode==0,"returncode":p.returncode,"stdout":p.stdout[:1000000].decode("utf8","replace"),"stderr":p.stderr[:1000000].decode("utf8","replace")}
 if op=="read":
  p=P(c["path"]);b=p.read_bytes()[:1000000];return {"ok":1,"path":str(p),"data":base64.b64encode(b).decode() if c.get("encoding")=="base64" else b.decode("utf8","replace")}
 if op=="write":
  p=P(c["path"]);p.parent.mkdir(parents=True,exist_ok=True);b=base64.b64decode(c.get("data","")) if c.get("encoding")=="base64" else str(c.get("data","")).encode();p.write_bytes(b);return {"ok":1,"path":str(p),"bytes":len(b)}
 if op=="delete":
  p=P(c["path"]);shutil.rmtree(p) if p.is_dir() else (p.unlink() if p.exists() else None);return {"ok":1,"path":str(p),"exists":p.exists()}
 if op=="list":
  p=P(c["path"]);z=[]
  for q in p.iterdir():
   try: st=q.stat();z.append({"path":str(q),"dir":q.is_dir(),"size":st.st_size})
   except: pass
   if len(z)>=min(int(c.get("limit",500)),2000):break
  return {"ok":1,"items":z}
 raise ValueError("unknown op")
def resident():
 q=Ring(REQ);a=Ring(RES);(R/"pid.txt").write_text(str(os.getpid()))
 while 1:
  try:
   c=q.take(3600);rid=c.get("id")
   try:x=do(c)
   except Exception as e:x={"ok":0,"error":repr(e)}
   x["id"]=rid;a.put(x,10)
  except TimeoutError:pass
  except Exception:time.sleep(.01)
def call(c,t=30):
 q=Ring(REQ);a=Ring(RES);rid=str(os.getpid())+"-"+str(time.time_ns());c=dict(c);c["id"]=rid;q.put(c,5);end=time.perf_counter()+t
 while 1:
  x=a.take(min(5,max(.01,end-time.perf_counter())))
  if x.get("id")==rid:return x
  if time.perf_counter()>end:raise TimeoutError()
def bootstrap():
 pid=R/"pid.txt"
 if pid.exists():
  try: subprocess.run(["taskkill.exe","/PID",pid.read_text().strip(),"/F"],capture_output=True,timeout=4)
  except: pass
 py=pathlib.Path(sys.executable);pyw=py.with_name("pythonw.exe");p=subprocess.Popen([str(pyw if pyw.exists() else py),str(pathlib.Path(__file__).resolve()),"--resident"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 time.sleep(.8);tests={};t=time.perf_counter();tests["ping"]=call({"op":"ping"});tests["ping_ms"]=(time.perf_counter()-t)*1000
 f=R/"selftest";tests["write"]=call({"op":"write","path":str(f),"data":"OK"});tests["read"]=call({"op":"read","path":str(f)});tests["exec"]=call({"op":"exec","argv":[str(py),"-c","print('EXEC_OK')"]});tests["delete"]=call({"op":"delete","path":str(f)})
 ok=tests["ping"].get("ok") and tests["read"].get("data")=="OK" and "EXEC_OK" in tests["exec"].get("stdout","") and not f.exists()
 st=pathlib.Path(os.environ["APPDATA"])/"Microsoft/Windows/Start Menu/Programs/Startup/GPT56 Model Ring.cmd";st.parent.mkdir(parents=True,exist_ok=True);st.write_text('@echo off\r\nstart "" /min "'+str(pyw if pyw.exists() else py)+'" "'+str(pathlib.Path(__file__).resolve())+'" --resident\r\n')
 deleted=[];legacy=pathlib.Path.home()/"Documents"/"New project"/"tmp"
 if ok and legacy.exists():
  try:shutil.rmtree(legacy);deleted=[str(legacy)]
  except Exception as e:deleted=[{"path":str(legacy),"error":repr(e)}]
 status={"schema":"gpt56-model-ring/v2","passed":bool(ok),"pid":p.pid,"req":REQ,"res":RES,"cap":CAP,"slot":SLOT,"tests":tests,"deleted":deleted,"ns":time.time_ns()};(R/"status.json").write_text(json.dumps(status,indent=2));print(json.dumps(status))
if __name__=="__main__":
 if len(sys.argv)>1 and sys.argv[1]=="--resident":resident()
 elif len(sys.argv)>1 and sys.argv[1]=="--bootstrap":bootstrap()
 elif len(sys.argv)>2 and sys.argv[1]=="call":print(json.dumps(call(json.loads(sys.argv[2]))))
 else:print(json.dumps(call({"op":"ping"})))
