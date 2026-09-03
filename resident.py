#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, secrets, shutil, tempfile, time
from pathlib import Path

SCHEMA = 1

class Lock:
    def __init__(self, path: Path): self.path=path; self.f=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f=open(self.path,'a+b')
        if os.name=='nt':
            import msvcrt
            try: msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise SystemExit('resident already running')
        else:
            import fcntl
            try: fcntl.flock(self.f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError: raise SystemExit('resident already running')
        return self
    def __exit__(self,*_):
        if self.f:
            try:
                if os.name=='nt':
                    import msvcrt; self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK,1)
                else:
                    import fcntl; fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            finally: self.f.close()

class Resident:
    def __init__(self, root: Path):
        self.root=root
        self.inbox=root/'inbox'; self.receipts=root/'receipts'; self.archive=root/'archive'
        for p in (root,self.inbox,self.receipts,self.archive): p.mkdir(parents=True,exist_ok=True)
        self.identity_path=root/'identity.json'; self.snapshot_path=root/'state.json'; self.journal_path=root/'journal.jsonl'
        self.identity=self._load_identity(); self.boot_id=secrets.token_hex(16)
        self.state={'schema':SCHEMA,'generation':0,'observations':0,'last_event':None,'processed':{}}
        self._recover()

    def _load_identity(self):
        if self.identity_path.exists(): return json.loads(self.identity_path.read_text())
        ident={'schema':SCHEMA,'node_id':secrets.token_hex(16),'created_ns':time.time_ns()}
        self._atomic_json(self.identity_path, ident); return ident

    def _atomic_json(self,path,obj):
        fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=str(path.parent))
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(obj,f,separators=(',',':'),sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def _recover(self):
        if self.snapshot_path.exists():
            try: self.state=json.loads(self.snapshot_path.read_text())
            except Exception: pass
        if self.journal_path.exists():
            for line in self.journal_path.read_text(errors='replace').splitlines():
                try:
                    rec=json.loads(line)
                    if rec.get('kind')=='commit' and rec.get('generation',0)>self.state.get('generation',0):
                        self.state=rec['state']
                except Exception: continue

    def status(self):
        return {'ok':True,'schema':SCHEMA,'node_id':self.identity['node_id'],'boot_id':self.boot_id,
                'generation':self.state['generation'],'observations':self.state['observations']}

    def _commit(self, command_id, event):
        self.state['generation'] += 1
        self.state['processed'][command_id]=self.state['generation']
        if event['op']=='observe':
            payload=event.get('payload')
            self.state['observations'] += 1
            self.state['last_event']={'sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
                                      'at_ns':time.time_ns()}
        rec={'kind':'commit','generation':self.state['generation'],'command_id':command_id,'state':self.state}
        with open(self.journal_path,'a',encoding='utf-8') as f:
            f.write(json.dumps(rec,separators=(',',':'),sort_keys=True)+'\n'); f.flush(); os.fsync(f.fileno())
        self._atomic_json(self.snapshot_path,self.state)

    def handle(self,event):
        if not isinstance(event,dict) or event.get('schema')!=SCHEMA: return {'ok':False,'error':'bad_schema'}
        cid=event.get('id'); op=event.get('op')
        if not isinstance(cid,str) or not cid or op not in {'observe','checkpoint','status','shutdown'}:
            return {'ok':False,'error':'bad_command'}
        if cid in self.state['processed']:
            return {'ok':True,'duplicate':True,'generation':self.state['processed'][cid],'node_id':self.identity['node_id']}
        if op=='status':
            out=self.status(); out['command_id']=cid; return out
        if op=='checkpoint':
            self._atomic_json(self.snapshot_path,self.state)
        elif op=='observe':
            self._commit(cid,event)
        elif op=='shutdown':
            self._commit(cid,event); return {'ok':True,'shutdown':True,'generation':self.state['generation'],'node_id':self.identity['node_id']}
        if op!='observe':
            self._commit(cid,event)
        return {'ok':True,'generation':self.state['generation'],'node_id':self.identity['node_id']}

    def serve(self,poll=0.05):
        print(json.dumps(self.status(),sort_keys=True),flush=True)
        while True:
            files=sorted(self.inbox.glob('*.json'))
            if not files: time.sleep(poll); continue
            for path in files:
                processing=self.archive/(path.name+'.processing')
                try: os.replace(path,processing)
                except FileNotFoundError: continue
                try:
                    evt=json.loads(processing.read_text())
                    receipt=self.handle(evt)
                except Exception as e:
                    receipt={'ok':False,'error':'invalid_json','detail':type(e).__name__}
                self._atomic_json(self.receipts/(path.stem+'.json'),receipt)
                done=self.archive/(path.name+'.done')
                os.replace(processing,done)
                if receipt.get('shutdown'): return

def submit(root:Path, op:str, payload=None, cid=None):
    root.mkdir(parents=True,exist_ok=True); (root/'inbox').mkdir(exist_ok=True)
    cid=cid or secrets.token_hex(12)
    evt={'schema':SCHEMA,'id':cid,'op':op}
    if payload is not None: evt['payload']=payload
    tmp=root/'inbox'/(cid+'.tmp'); dst=root/'inbox'/(cid+'.json')
    tmp.write_text(json.dumps(evt,separators=(',',':'),sort_keys=True)); os.replace(tmp,dst)
    print(cid)

def self_test():
    root=Path(tempfile.mkdtemp(prefix='archie-zero-'))
    try:
        r=Resident(root)
        node=r.identity['node_id']
        a=r.handle({'schema':1,'id':'a','op':'observe','payload':{'x':1}})
        d=r.handle({'schema':1,'id':'a','op':'observe','payload':{'x':999}})
        assert a['generation']==1 and d['duplicate'] and r.state['observations']==1
        r2=Resident(root)
        assert r2.identity['node_id']==node and r2.state['generation']==1 and r2.state['observations']==1
        b=r2.handle({'schema':1,'id':'b','op':'observe','payload':{'x':2}})
        assert b['generation']==2
        snap=json.loads((root/'state.json').read_text()); assert snap['generation']==2
        print(json.dumps({'ok':True,'node_stable':True,'restart_recovered':True,'dedupe':True,'generation':2}))
    finally: shutil.rmtree(root,ignore_errors=True)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=str(Path.home()/'.archie-zero'))
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('serve'); sub.add_parser('status'); sub.add_parser('self-test')
    p=sub.add_parser('submit'); p.add_argument('op',choices=['observe','checkpoint','status','shutdown']); p.add_argument('--payload'); p.add_argument('--id')
    a=ap.parse_args(); root=Path(a.root)
    if a.cmd=='self-test': return self_test()
    if a.cmd=='serve':
        with Lock(root/'resident.lock'): Resident(root).serve()
    elif a.cmd=='status': print(json.dumps(Resident(root).status(),sort_keys=True))
    else:
        payload=json.loads(a.payload) if a.payload else None; submit(root,a.op,payload,a.id)
if __name__=='__main__': main()
