#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, hashlib, json, os, shlex, tempfile, time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = 1

class CompileError(Exception): pass
class VMFault(Exception): pass

def literal(s: str) -> Any:
    try: return ast.literal_eval(s)
    except Exception:
        if s.lower() == 'true': return True
        if s.lower() == 'false': return False
        if s.lower() == 'null': return None
        try: return int(s)
        except Exception:
            try: return float(s)
            except Exception: return s

@dataclass
class Program:
    name: str
    code: list[list[Any]]
    source_hash: str

    @classmethod
    def compile(cls, name: str, text: str) -> 'Program':
        labels: dict[str,int] = {}
        raw: list[tuple[int,list[str]]] = []
        pc = 0
        for ln, line in enumerate(text.splitlines(), 1):
            line = line.split('#',1)[0].strip()
            if not line: continue
            if line.endswith(':'):
                label = line[:-1].strip()
                if not label or label in labels: raise CompileError(f'{name}:{ln}: bad/duplicate label {label!r}')
                labels[label] = pc; continue
            try: parts = shlex.split(line, posix=True)
            except Exception as e: raise CompileError(f'{name}:{ln}: {e}')
            if not parts: continue
            raw.append((ln, parts)); pc += 1
        code: list[list[Any]] = []
        noarg = {'POP','DUP','SWAP','ADD','SUB','MUL','DIV','MOD','EQ','NE','LT','LE','GT','GE','NOT',
                 'RET','PRINT','RECV','YIELD','HALT','OBJPUT','OBJGET','CHECKPOINT'}
        onearg = {'PUSH','LOAD','STORE','JMP','JZ','JNZ','CALL','SPAWN','SLEEP','EMIT','NAMESET','NAMEGET'}
        for ln, p in raw:
            op = p[0].upper(); args = p[1:]
            if op in noarg and args: raise CompileError(f'{name}:{ln}: {op} takes no args')
            if op in noarg: code.append([op]); continue
            if op in onearg:
                if len(args) != 1: raise CompileError(f'{name}:{ln}: {op} takes one arg')
                a: Any = args[0]
                if op == 'PUSH': a = literal(a)
                elif op in {'JMP','JZ','JNZ','CALL'}:
                    if a not in labels: raise CompileError(f'{name}:{ln}: unknown label {a!r}')
                    a = labels[a]
                elif op == 'SLEEP': a = int(a)
                code.append([op,a]); continue
            if op == 'SEND':
                if args: raise CompileError(f'{name}:{ln}: SEND takes pid and message from stack')
                code.append([op]); continue
            if op == 'CAP':
                if len(args) != 2: raise CompileError(f'{name}:{ln}: CAP <name> <argc>')
                code.append([op,args[0],int(args[1])]); continue
            if op == 'ASSERT':
                if len(args) > 1: raise CompileError(f'{name}:{ln}: ASSERT [message]')
                code.append([op,args[0] if args else 'assertion failed']); continue
            raise CompileError(f'{name}:{ln}: unknown opcode {op}')
        return cls(name=name, code=code, source_hash=hashlib.sha256(text.encode()).hexdigest())

@dataclass
class Proc:
    pid: int
    program: Program
    pc: int = 0
    stack: list[Any] = field(default_factory=list)
    locals: dict[str,Any] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)
    mailbox: list[Any] = field(default_factory=list)
    alive: bool = True
    blocked: bool = False
    wake_tick: int = 0
    exit_value: Any = None
    parent: int | None = None
    steps: int = 0
    fault: str | None = None

class ObjectStore:
    def __init__(self, root: Path):
        self.root = root; self.obj = root/'objects'; self.obj.mkdir(parents=True, exist_ok=True)
        self.names_path = root/'names.json'; self.names = self._read_json(self.names_path,{})
    def _read_json(self,p,d):
        try: return json.loads(p.read_text())
        except Exception: return d
    def _atomic(self,p,obj):
        p.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=p.name+'.',dir=str(p.parent))
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(obj,f,separators=(',',':'),sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    def put(self,v:Any)->str:
        data=json.dumps(v,separators=(',',':'),sort_keys=True).encode(); h=hashlib.sha256(data).hexdigest(); p=self.obj/h
        if not p.exists():
            fd,tmp=tempfile.mkstemp(prefix=h+'.',dir=str(self.obj))
            try:
                with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
                os.replace(tmp,p)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        return h
    def get(self,h:str)->Any:
        p=self.obj/h
        if not p.is_file(): raise VMFault(f'object not found: {h}')
        return json.loads(p.read_text())
    def set_name(self,name,h):
        if not (self.obj/h).is_file(): raise VMFault('name target missing')
        self.names[name]=h; self._atomic(self.names_path,self.names)
    def get_name(self,name):
        if name not in self.names: raise VMFault(f'name not found: {name}')
        return self.names[name]

class Kernel:
    def __init__(self, root: Path, workspace: Path | None = None, quantum: int = 64, recover: bool = True):
        self.root=root; root.mkdir(parents=True,exist_ok=True)
        self.modules=root/'modules'; self.modules.mkdir(exist_ok=True)
        self.workspace=(workspace or (root/'workspace')).resolve(); self.workspace.mkdir(parents=True,exist_ok=True)
        self.quantum=quantum; self.tick=0; self.seq=0; self.next_pid=1
        self.procs: dict[int,Proc]={}; self.ready=deque(); self.breakpoints:dict[int,set[int]]={}
        self.profile=Counter(); self.objects=ObjectStore(root)
        self.events_path=root/'events.jsonl'; self.snap_path=root/'kernel.json'
        self.boot_id=os.urandom(8).hex(); self.node_id=self._identity()
        if recover and self.snap_path.exists(): self._recover()
        self.event('boot',{'boot_id':self.boot_id,'node_id':self.node_id})
    def _identity(self):
        p=self.root/'identity.json'
        if p.exists(): return json.loads(p.read_text())['node_id']
        ident={'version':VERSION,'node_id':os.urandom(16).hex(),'created_ns':time.time_ns()}; self._atomic(p,ident); return ident['node_id']
    def _atomic(self,p,obj):
        fd,tmp=tempfile.mkstemp(prefix=p.name+'.',dir=str(p.parent))
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(obj,f,separators=(',',':'),sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    def event(self,kind,data):
        self.seq += 1; rec={'v':VERSION,'seq':self.seq,'tick':self.tick,'kind':kind,'data':data}
        with open(self.events_path,'a',encoding='utf-8') as f:
            f.write(json.dumps(rec,separators=(',',':'),sort_keys=True)+'\n'); f.flush(); os.fsync(f.fileno())
        return rec
    def module_text(self,name):
        p=(self.modules/(name+'.sol')).resolve()
        if self.modules.resolve() not in p.parents: raise VMFault('bad module path')
        if not p.is_file(): raise VMFault(f'module not found: {name}')
        return p.read_text()
    def load_module(self,name): return Program.compile(name,self.module_text(name))
    def spawn(self, program:Program, parent=None, locals_=None):
        pid=self.next_pid; self.next_pid+=1
        loc={'pid':pid,'parent':parent}; loc.update(locals_ or {})
        p=Proc(pid=pid,program=program,parent=parent,locals=loc); self.procs[pid]=p; self.ready.append(pid)
        self.event('spawn',{'pid':pid,'parent':parent,'program':program.name,'hash':program.source_hash}); return pid
    def send(self,src,dst,msg):
        p=self.procs.get(dst)
        if not p or not p.alive: raise VMFault(f'no live pid {dst}')
        p.mailbox.append({'from':src,'value':msg});
        if p.blocked: p.blocked=False; self.ready.append(dst)
        self.event('ipc.send',{'src':src,'dst':dst,'object':self.objects.put(msg)})
    def safe_path(self,rel):
        p=(self.workspace/str(rel)).resolve()
        if p!=self.workspace and self.workspace not in p.parents: raise VMFault('capability path escapes workspace')
        return p
    def cap(self,p:Proc,name,args):
        if name=='clock.tick': return self.tick
        if name=='proc.pid': return p.pid
        if name=='proc.list': return sorted(pid for pid,x in self.procs.items() if x.alive)
        if name=='fs.read_text':
            if len(args)!=1: raise VMFault('fs.read_text argc')
            return self.safe_path(args[0]).read_text()
        if name=='fs.write_text':
            if len(args)!=2: raise VMFault('fs.write_text argc')
            path=self.safe_path(args[0]); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(str(args[1])); return len(str(args[1]))
        if name=='fs.list':
            if len(args)!=1: raise VMFault('fs.list argc')
            pth=self.safe_path(args[0]); return sorted(x.name for x in pth.iterdir())
        raise VMFault(f'capability denied/unknown: {name}')
    def binary(self,p,fn):
        if len(p.stack)<2: raise VMFault('stack underflow')
        b=p.stack.pop(); a=p.stack.pop(); p.stack.append(fn(a,b))
    def step(self,p:Proc):
        if p.pc<0 or p.pc>=len(p.program.code): p.alive=False; p.exit_value=None; return
        ins=p.program.code[p.pc]; p.pc+=1; op=ins[0]; self.profile[op]+=1; p.steps+=1
        if op=='PUSH': p.stack.append(ins[1])
        elif op=='POP': p.stack.pop()
        elif op=='DUP': p.stack.append(p.stack[-1])
        elif op=='SWAP': p.stack[-1],p.stack[-2]=p.stack[-2],p.stack[-1]
        elif op=='LOAD': p.stack.append(p.locals.get(ins[1]))
        elif op=='STORE': p.locals[ins[1]]=p.stack.pop()
        elif op=='ADD': self.binary(p,lambda a,b:a+b)
        elif op=='SUB': self.binary(p,lambda a,b:a-b)
        elif op=='MUL': self.binary(p,lambda a,b:a*b)
        elif op=='DIV': self.binary(p,lambda a,b:a/b)
        elif op=='MOD': self.binary(p,lambda a,b:a%b)
        elif op=='EQ': self.binary(p,lambda a,b:a==b)
        elif op=='NE': self.binary(p,lambda a,b:a!=b)
        elif op=='LT': self.binary(p,lambda a,b:a<b)
        elif op=='LE': self.binary(p,lambda a,b:a<=b)
        elif op=='GT': self.binary(p,lambda a,b:a>b)
        elif op=='GE': self.binary(p,lambda a,b:a>=b)
        elif op=='NOT': p.stack.append(not p.stack.pop())
        elif op=='JMP': p.pc=ins[1]
        elif op=='JZ':
            if not p.stack.pop(): p.pc=ins[1]
        elif op=='JNZ':
            if p.stack.pop(): p.pc=ins[1]
        elif op=='CALL': p.calls.append(p.pc); p.pc=ins[1]
        elif op=='RET': p.pc=p.calls.pop()
        elif op=='PRINT':
            v=p.stack.pop(); print(v); self.event('print',{'pid':p.pid,'object':self.objects.put(v)})
        elif op=='EMIT':
            v=p.stack.pop(); self.event('user.'+ins[1],{'pid':p.pid,'object':self.objects.put(v)})
        elif op=='SEND':
            msg=p.stack.pop(); dst=int(p.stack.pop()); self.send(p.pid,dst,msg)
        elif op=='RECV':
            if p.mailbox:
                envelope=p.mailbox.pop(0); p.locals['sender']=envelope['from']; p.stack.append(envelope['value'])
            else: p.pc-=1; p.blocked=True
        elif op=='SPAWN': p.stack.append(self.spawn(self.load_module(ins[1]),parent=p.pid))
        elif op=='SLEEP': p.wake_tick=self.tick+max(1,ins[1]); p.blocked=True
        elif op=='YIELD': pass
        elif op=='OBJPUT': p.stack.append(self.objects.put(p.stack.pop()))
        elif op=='OBJGET': p.stack.append(self.objects.get(p.stack.pop()))
        elif op=='NAMESET': self.objects.set_name(ins[1],p.stack.pop())
        elif op=='NAMEGET': p.stack.append(self.objects.get_name(ins[1]))
        elif op=='CAP':
            argc=ins[2]
            if len(p.stack)<argc: raise VMFault('stack underflow')
            args=p.stack[-argc:] if argc else []
            if argc: del p.stack[-argc:]
            p.stack.append(self.cap(p,ins[1],args))
        elif op=='ASSERT':
            if not p.stack.pop(): raise VMFault(ins[1])
        elif op=='CHECKPOINT': self.checkpoint()
        elif op=='HALT': p.exit_value=p.stack.pop() if p.stack else None; p.alive=False; self.event('exit',{'pid':p.pid,'value':self.objects.put(p.exit_value)})
        else: raise VMFault(f'bad bytecode {op}')
        return op
    def run(self,max_ticks=100000):
        while self.tick<max_ticks and any(p.alive for p in self.procs.values()):
            for p in self.procs.values():
                if p.alive and p.blocked and p.wake_tick and p.wake_tick<=self.tick:
                    p.blocked=False; p.wake_tick=0; self.ready.append(p.pid)
            if not self.ready:
                self.tick+=1; continue
            pid=self.ready.popleft(); p=self.procs.get(pid)
            if not p or not p.alive or p.blocked: self.tick+=1; continue
            n=0
            try:
                while n<self.quantum and p.alive and not p.blocked:
                    if p.pc in self.breakpoints.get(pid,set()):
                        p.blocked=True; self.event('breakpoint',{'pid':pid,'pc':p.pc}); break
                    executed=self.step(p); n+=1
                    if executed=='YIELD': break
            except Exception as e:
                p.alive=False; p.fault=f'{type(e).__name__}: {e}'; self.event('fault',{'pid':pid,'fault':p.fault})
            if p.alive and not p.blocked: self.ready.append(pid)
            self.tick+=1
        return {pid:{'alive':p.alive,'exit':p.exit_value,'fault':p.fault,'steps':p.steps} for pid,p in self.procs.items()}
    def checkpoint(self):
        procs=[]
        for p in self.procs.values():
            procs.append({'pid':p.pid,'program':{'name':p.program.name,'code':p.program.code,'source_hash':p.program.source_hash},
                          'pc':p.pc,'stack':p.stack,'locals':p.locals,'calls':p.calls,'mailbox':p.mailbox,'alive':p.alive,
                          'blocked':p.blocked,'wake_tick':p.wake_tick,'exit_value':p.exit_value,'parent':p.parent,'steps':p.steps,'fault':p.fault})
        snap={'v':VERSION,'node_id':self.node_id,'tick':self.tick,'seq':self.seq,'next_pid':self.next_pid,
              'ready':list(self.ready),'profile':dict(self.profile),'procs':procs}
        self._atomic(self.snap_path,snap)
    def _recover(self):
        s=json.loads(self.snap_path.read_text())
        if s.get('v')!=VERSION or s.get('node_id')!=self.node_id: raise VMFault('snapshot identity/version mismatch')
        self.tick=s['tick']; self.seq=s['seq']; self.next_pid=s['next_pid']; self.ready=deque(); self.profile=Counter(s['profile']); self.procs={}
        for x in s['procs']:
            pr=Program(**x.pop('program')); p=Proc(program=pr,**x); self.procs[p.pid]=p
        for pid,p in sorted(self.procs.items()):
            if p.alive and not p.blocked: self.ready.append(pid)
        self.event('recover',{'processes':len(self.procs),'ready':len(self.ready)})
    def status(self):
        return {'node_id':self.node_id,'boot_id':self.boot_id,'tick':self.tick,'seq':self.seq,'next_pid':self.next_pid,
                'processes':{pid:{'program':p.program.name,'pc':p.pc,'alive':p.alive,'blocked':p.blocked,'mailbox':len(p.mailbox),'fault':p.fault} for pid,p in self.procs.items()},
                'profile':dict(self.profile)}

def self_test():
    with tempfile.TemporaryDirectory(prefix='sol-computer-') as td:
        root=Path(td); mods=root/'modules'; mods.mkdir()
        (mods/'child.sol').write_text('''\nrecv\nstore msg\nload msg\npush "ping"\neq\nassert "child got wrong message"\npush "pong"\nload parent\nswap\nsend\nhalt\n''')
        main='''\nspawn child\nstore child\nload child\npush "ping"\nsend\nrecv\nstore reply\nload reply\nobjput\nstore reply_obj\nload reply_obj\nnameset last_reply\ncheckpoint\nload reply\nhalt\n'''
        k=Kernel(root,quantum=8); pid=k.spawn(Program.compile('main',main)); r=k.run(200)
        assert r[pid]['exit']=='pong' and not r[pid]['fault']
        h=k.objects.get_name('last_reply'); assert k.objects.get(h)=='pong'
        k.checkpoint(); node=k.node_id
        k2=Kernel(root,quantum=8,recover=True); assert k2.node_id==node and pid in k2.procs
        loop=Program.compile('restart_loop','''
push 0
store n
loop:
load n
push 1
add
store n
load n
push 5
ge
jnz done
yield
jmp loop
done:
load n
halt
''')
        rp=k2.spawn(loop); k2.run(k2.tick+2); assert k2.procs[rp].alive
        k2.checkpoint(); seq_before=k2.seq
        k3=Kernel(root,quantum=8,recover=True); assert k3.seq==seq_before+2
        r3=k3.run(k3.tick+20); assert r3[rp]['exit']==5 and not r3[rp]['fault']
        k2=k3
        p=Proc(999,Program.compile('cap','push "x.txt"\npush "hello"\ncap fs.write_text 2\npop\npush "x.txt"\ncap fs.read_text 1\nhalt'))
        k2.procs[p.pid]=p; k2.ready.append(p.pid); rr=k2.run(k2.tick+50); assert rr[999]['exit']=='hello'
        print(json.dumps({'ok':True,'language':True,'vm':True,'scheduler':True,'ipc':True,'objects':True,'capabilities':True,'persistence':True,'node_id_stable':True,'events':k2.seq,'profile_ops':len(k2.profile)},sort_keys=True))

def main():
    ap=argparse.ArgumentParser(description='single-process local computer kernel')
    ap.add_argument('--root',default=str(Path.home()/'.sol-computer')); ap.add_argument('--workspace')
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('self-test'); sub.add_parser('status'); sub.add_parser('checkpoint')
    r=sub.add_parser('run'); r.add_argument('source'); r.add_argument('--max-ticks',type=int,default=100000)
    c=sub.add_parser('compile'); c.add_argument('source')
    a=ap.parse_args(); root=Path(a.root); ws=Path(a.workspace) if a.workspace else None
    if a.cmd=='self-test': return self_test()
    if a.cmd=='compile':
        p=Path(a.source); prog=Program.compile(p.stem,p.read_text()); print(json.dumps({'name':prog.name,'hash':prog.source_hash,'code':prog.code},indent=2)); return
    k=Kernel(root,workspace=ws)
    if a.cmd=='status': print(json.dumps(k.status(),indent=2,sort_keys=True)); return
    if a.cmd=='checkpoint': k.checkpoint(); print(json.dumps(k.status(),indent=2,sort_keys=True)); return
    p=Path(a.source); prog=Program.compile(p.stem,p.read_text()); pid=k.spawn(prog); out=k.run(a.max_ticks); k.checkpoint(); print(json.dumps({'pid':pid,'result':out[pid],'status':k.status()},indent=2,sort_keys=True,default=str))

if __name__=='__main__': main()
