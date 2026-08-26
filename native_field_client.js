(()=>{'use strict';
const core=window.ARCHIE_FIELD_CORE;
if(!core)return;
const STORE='archie.native.field.token';
let token='';
function hashToken(){let h=location.hash.replace(/^#/,'');if(h.startsWith('t='))h=h.slice(2);try{h=decodeURIComponent(h)}catch(_){}return /^[A-Za-z0-9_-]{24,160}$/.test(h)?h:''}
function init(){const h=hashToken();if(h){token=h;try{localStorage.setItem(STORE,token)}catch(_){}history.replaceState(null,'',location.pathname+location.search);return}try{const v=localStorage.getItem(STORE)||'';if(/^[A-Za-z0-9_-]{24,160}$/.test(v))token=v}catch(_){}}
init();
window.ARCHIE_FIELD_ENDPOINT={token:()=>token,forget:()=>{token='';try{localStorage.removeItem(STORE)}catch(_){}}};
core.configure({
  autostart:true,
  connectTimeoutMs:1500,
  resolve:async()=>{if(!token)throw Error('field token absent');return{base:location.origin,token}}
});
})();
