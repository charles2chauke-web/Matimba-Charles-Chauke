import {useState} from 'react';
import {Database,RefreshCw,ShieldCheck} from 'lucide-react';
import {api} from './api';

type Check={ok:boolean;code:string;message:string;checked_at:string;subscription?:{plan:string;active:boolean;end:string};usage?:{current:number;daily_limit:number;daily_remaining:number}};
export type Provider={id:string;name:string;configured:boolean;status:string;updated_at:string|null;connection_test_available:boolean;last_check:Check|null};
const date=(v:string)=>new Intl.DateTimeFormat('en-ZA',{timeZone:'Africa/Johannesburg',dateStyle:'medium',timeStyle:'short'}).format(new Date(v));

export function ProviderWorkspace({providers,onRefresh}:{providers:Provider[];onRefresh:()=>Promise<void>}){
 const [keys,setKeys]=useState<Record<string,string>>({}),[busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState('');
 async function action(id:string,work:()=>Promise<unknown>,message:string){setBusy(id);setError('');setNotice('');try{await work();await onRefresh();setNotice(message);}catch(e){setError((e as Error).message);}finally{setBusy('');}}
 return <><section className="info-banner"><ShieldCheck size={21}/><p><strong>Encrypted, local credential storage</strong><br/>Keys stay on the backend. Saving stores your key; Test connection contacts API-Sports securely and checks account access. League coverage is checked when you load data. A successful connection does not validate betting predictions.</p></section>
 {error&&<div className="notification error" role="alert">{error}</div>}{notice&&<div className="notification success" role="status">{notice}</div>}
 <div className="provider-grid">{providers.map(p=><section className="panel provider" key={p.id}><div className="provider-icon"><Database size={22}/></div><h2>{p.name}</h2><span className={'pill '+(p.last_check?.ok?'green':'neutral')}>{p.status}</span>
 <p>{p.connection_test_available?'Use the key from your direct API-Sports dashboard. After a successful test, open Run Model to import league results and upcoming fixtures.':'This provider adapter is not implemented. A stored key cannot activate it.'}</p>
 <label>API key<input type="password" autoComplete="new-password" placeholder={p.configured?'Enter a replacement key':'Enter your provider API key'} value={keys[p.id]||''} onChange={e=>setKeys({...keys,[p.id]:e.target.value})}/></label>
 <div className="button-row"><button className="primary" disabled={!!busy||!keys[p.id]?.trim()} onClick={()=>action(p.id,async()=>{await api('/providers/'+p.id,'PUT',{key:keys[p.id]});setKeys(k=>({...k,[p.id]:''}));},'Key encrypted and saved. Use Test connection to verify account access.')}>Save securely</button>
 {p.configured&&p.connection_test_available&&<button className="secondary" disabled={!!busy} onClick={()=>action(p.id,async()=>{const check=await api<Check>('/providers/'+p.id+'/test','POST',{});await onRefresh();if(!check.ok)throw new Error(check.message);},'API-Football connection successful. Open Run Model to load data.')}><RefreshCw size={14} className={busy===p.id?'spin':''}/>Test connection</button>}
 {p.configured&&<button className="secondary" disabled={!!busy} onClick={()=>action(p.id,()=>api('/providers/'+p.id,'DELETE'),'Saved credential removed.')}>Remove key</button>}</div>
 <small>{p.updated_at?`Saved ${date(p.updated_at)} SAST`:p.configured?'Environment key configured':'No credential stored'}</small>
 {p.last_check&&<div className="provider-check" role="status"><strong>{p.last_check.message}</strong><small>Last tested {date(p.last_check.checked_at)} SAST</small>{p.last_check.subscription&&<p>Plan: {p.last_check.subscription.plan} · {p.last_check.subscription.active?'Active':'Inactive'}<br/>Subscription ends {date(p.last_check.subscription.end)} SAST</p>}{p.last_check.usage&&<p>{p.last_check.usage.current.toLocaleString()} requests used / {p.last_check.usage.daily_limit.toLocaleString()} per day<br/>{p.last_check.usage.daily_remaining.toLocaleString()} remaining at last test</p>}</div>}
 </section>)}</div></>;
}
