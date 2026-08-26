(()=>{'use strict';
const core=window.ARCHIE_FIELD_CORE,surface=window.ARCHIE_FIELD_SURFACE;
if(!core)return;
const TAIL='https://desktop-6fn9b4m-1.tail1bf489.ts.net:8444';
const BOOT='https://raw.githubusercontent.com/Pokitomas/archie-backend-xfer-1787735782/master/field_bootstrap.ps1';
const STORE='archie.field.native.v1';
let activeToken='',bootPromise=null,lastReturned='';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const rand=n=>{const a=crypto.getRandomValues(new Uint8Array(n));let s='';for(const b of a)s+=String.fromCharCode(b);return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')};
function timeoutFetch(url,opt={},ms=2200){const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),ms);return fetch(url,{cache:'no-store',...opt,signal:ac.signal}).finally(()=>clearTimeout(tm))}
function valid(v){return !!v&&/^https:\/\//i.test(String(v.base||''))&&/^[A-Za-z0-9_-]{24,160}$/.test(String(v.token||''))}
function save(v){if(!valid(v))return;activeToken=String(v.token);lastReturned=String(v.base).replace(/\/$/,'');try{localStorage.setItem(STORE,JSON.stringify({base:lastReturned,token:activeToken,at:Date.now()}))}catch(_){}}
function load(){try{const v=JSON.parse(localStorage.getItem(STORE)||'{}');if(valid(v)&&Date.now()-Number(v.at||0)<30*86400000)return{base:String(v.base).replace(/\/$/,''),token:String(v.token)}}catch(_){}return null}
function forget(){try{localStorage.removeItem(STORE)}catch(_){}if(lastReturned){lastReturned='';activeToken=''}}
async function tail(path,opt={},ms=2600){const r=await timeoutFetch(TAIL+path,{...opt,headers:{...(opt.headers||{}),'Content-Type':'application/json'}},ms);let v={};try{v=await r.json()}catch(_){}if(!r.ok)throw Error('bootstrap '+r.status);return v}
async function controllerAction(agent,value){return tail('/action',{method:'POST',body:JSON.stringify({...value,agent})},5200)}
async function announce(topic,token){for(let i=0;i<160;i++){try{const r=await timeoutFetch('https://ntfy.sh/'+encodeURIComponent(topic)+'/json?poll=1&since=all',{},1500);if(r.ok){const raw=await r.text();for(const line of raw.trim().split('\n').reverse()){try{const item=JSON.parse(line),m=JSON.parse(item.message||'{}');if(m.native===true&&m.token===token&&/^https:\/\//i.test(String(m.url||'')))return{base:String(m.url).replace(/\/$/,''),token}}catch(_){}}}}catch(_){}await sleep(180)}throw Error('native cutover not announced')}
async function bootstrap(){if(bootPromise)return bootPromise;bootPromise=(async()=>{if(!crypto?.getRandomValues||typeof fetch!=='function')throw Error('browser preflight');const head=await tail('/seat/head',{},1700);if(!head?.active_occupant)throw Error('controller preflight');const agent=String(head.active_occupant),token=rand(32),topic='archie-'+rand(18).toLowerCase();await controllerAction(agent,{action:'timebox',seconds:900,label:'entry-pressure',intent:'automatic ingress pressure'});const cmd=`powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$u='${BOOT}';$p=Join-Path $env:TEMP 'archie_field_bootstrap.ps1';Invoke-WebRequest -UseBasicParsing ($u+'?t='+[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -OutFile $p;& $p -Token '${token}' -Topic '${topic}'"`;await controllerAction(agent,{action:'clipboard_set',text:cmd,value:cmd,clipboard:cmd});await controllerAction(agent,{action:'hotkey',keys:['win','r']});await sleep(150);await controllerAction(agent,{action:'hotkey',keys:['ctrl','v']});await sleep(45);await controllerAction(agent,{action:'key',key:'enter'});const native=await announce(topic,token);save(native);return native})().finally(()=>bootPromise=null);return bootPromise}
async function resolve({lastError,current}={}){if(lastError&&current?.base&&String(current.base).replace(/\/$/,'')===lastReturned)forget();const remembered=load();if(remembered){save(remembered);return remembered}return bootstrap()}
window.ARCHIE_FIELD_ENDPOINT={token:()=>activeToken,forget};
core.configure({resolve,autostart:false,connectTimeoutMs:1700});
// Scene is only a provisional skin while the machine field is absent. The
// connected controller immediately replaces it with surface.scene.
(async()=>{try{const r=await timeoutFetch(new URL('phone_scene.json?t='+Date.now(),location.href).href,{},1400);if(r.ok)surface?.apply(await r.json())}catch(_){}})();
document.addEventListener('pointerdown',()=>core.wake(),{once:true,capture:true});
document.addEventListener('focusin',()=>core.wake(),{once:true,capture:true});
})();
