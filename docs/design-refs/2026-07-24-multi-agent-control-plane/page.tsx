"use client";
import {useState} from "react";

const menu=[
 ["OVERVIEW","Control Center"],
 ["PIPELINE","Pipeline Builder","Templates"],
 ["AGENTS & ROLES","Providers & Roles","Role Overlap"],
 ["TOOLS","MCP Matrix","Plugins Matrix"],
 ["KNOWLEDGE","Skill Catalog","Skill Matrix"],
 ["GOVERNANCE","Users","Audit Log"]
];
const roles=["Lead","Frontend","Backend","Mobile","DevOps","QA","Reviewer","Design Critic"];
const colors=["#efad37","#28baa9","#4d7ff0","#9a64e9","#41b96f","#ec9534","#ef5f68","#e65798"];
const cols:{[k:string]:string[] }={
 "MCP Matrix":["playwright","chrome-devtools","context7","filesystem"],
 "Plugins Matrix":["addy-agent-skills","claude-plugins","pordee","superpowers","ui-ux-pro"],
 "Skill Matrix":["cockpit-ui-style","debug-mantra","management-talk","post-mortem"]
};
const subtitles:{[k:string]:string}={
 "Control Center":"ภาพรวมความพร้อมของระบบ Multi-Agent ก่อนเริ่มใช้งาน",
 "Pipeline Builder":"จัดลำดับการทำงาน และกำหนด Agent ที่ทำพร้อมกันหรือรอกัน",
 "Templates":"จัดการ Pipeline Template และนำกลับมาใช้ซ้ำ",
 "Providers & Roles":"เชื่อมต่อ Provider แล้วเลือก Model ให้แต่ละ Role",
 "Role Overlap":"ตรวจ Scope และ Instruction ที่ซ้ำหรือขัดแย้งกัน",
 "MCP Matrix":"กำหนดสิทธิ์ MCP Server แยกตาม Role",
 "Plugins Matrix":"กำหนด Plugin ที่แต่ละ Role เรียกใช้ได้",
 "Skill Catalog":"จัดการ Skill และ Instruction กลางของทีม",
 "Skill Matrix":"กำหนด Skill ที่แต่ละ Role อ้างอิงได้",
 "Users":"จัดการผู้ใช้งานและสิทธิ์ของ Workspace",
 "Audit Log":"ติดตามการแก้ไข ทดสอบ และ Apply Configuration"
};

export default function Home(){
 const [page,setPage]=useState("Control Center"),[dirty,setDirty]=useState(false),[toast,setToast]=useState(""),[nav,setNav]=useState(false),[modal,setModal]=useState(""),[tog,setTog]=useState<{[k:string]:boolean}>({});
 const flip=(k:string,f=true)=>{setTog(v=>({...v,[k]:!(v[k]??f)}));setDirty(true)};
 const apply=()=>{setDirty(false);setToast("ตรวจสอบและ Apply Configuration สำเร็จ");setTimeout(()=>setToast(""),2400)};
 return <main className="app">
  <aside className={nav?"side open":"side"}>
   <header><b>T</b><span><strong>Takkub Cockpit</strong><small>Multi-Agent Control Plane</small></span><button onClick={()=>setNav(false)}>×</button></header>
   <div className="workspace"><i/>Production Workspace <b>⌄</b></div>
   <nav>{menu.map(g=><section key={g[0]}><p>{g[0]}</p>{g.slice(1).map(x=><button className={page===x?"active":""} onClick={()=>{setPage(x);setNav(false)}} key={x}><i>{icon(x)}</i>{x}<span>›</span></button>)}</section>)}</nav>
   <footer><button onClick={()=>setModal("New Role")}>＋ New Role</button><div><b>AU</b><span><strong>Admin User</strong><small>Owner</small></span><i>•••</i></div></footer>
  </aside>
  {nav&&<button className="scrim" onClick={()=>setNav(false)}/>}
  <section className="main">
   <header className="top"><button onClick={()=>setNav(true)}>☰</button><span>Settings　/　<b>{page}</b></span><div><small>Active template</small><b>Feature (UI + API)</b><em>Built-in</em></div><button onClick={()=>setToast("System check: พร้อมใช้งาน 9/10 รายการ")}>✓</button><i>AU</i></header>
   <div className="content"><div className="heading"><span><p>CONFIGURATION</p><h1>{page}</h1><small>{subtitles[page]}</small></span>{page!=="Control Center"&&<button onClick={()=>setModal("Quick Help")}>? วิธีใช้งาน</button>}</div>
    {page==="Control Center"?<Dashboard go={setPage}/>:
     page==="Pipeline Builder"?<Pipeline change={()=>setDirty(true)}/>:
     page==="Templates"?<Templates go={()=>setPage("Pipeline Builder")}/>:
     page==="Providers & Roles"?<Providers tog={tog} flip={flip}/>:
     page==="Role Overlap"?<Overlap/>:
     cols[page]?<Matrix page={page} tog={tog} flip={flip}/>:
     page==="Skill Catalog"?<Skills create={()=>setModal("Create Skill")}/>:
     page==="Users"?<Users invite={()=>setModal("Invite User")}/>:<Audit/>}
   </div>
   <div className="save"><span className={dirty?"show":""}><i/>มีการแก้ไขที่ยังไม่บันทึก</span><button disabled={!dirty} onClick={()=>setDirty(false)}>↶ ย้อนกลับ</button><button>Save Draft</button><button className="primary" onClick={apply}>✓ Validate & Apply</button></div>
  </section>
  {modal&&<div className="modalbg" onMouseDown={()=>setModal("")}><section className="modal" onMouseDown={e=>e.stopPropagation()}><button onClick={()=>setModal("")}>×</button><i>✦</i><p>CONFIGURATION</p><h2>{modal}</h2><label>Name<input placeholder="ตั้งชื่อให้จำง่าย"/></label><label>Description<textarea placeholder="อธิบายหน้าที่หรือวัตถุประสงค์"/></label><footer><button onClick={()=>setModal("")}>Cancel</button><button className="primary" onClick={()=>{setModal("");setDirty(true);setToast("สร้างรายการแล้ว กรุณาตรวจสอบก่อน Apply")}}>Create</button></footer></section></div>}
  {toast&&<div className="toast"><b>✓</b>{toast}</div>}
 </main>
}
function icon(x:string){return x.includes("Matrix")?"⊞":x.includes("Pipeline")?"⌘":x.includes("Provider")?"◉":x.includes("Skill")?"✦":x.includes("User")?"♙":x.includes("Audit")?"◷":x==="Control Center"?"⌂":"◇"}

function Dashboard({go}:{go:(x:string)=>void}){
 const steps=[["Providers","4 / 5 connected","Providers & Roles",1],["Roles","8 roles configured","Providers & Roles",1],["Pipeline","Feature template active","Pipeline Builder",1],["MCP access","3 permissions need review","MCP Matrix",0],["Plugins","Policy ready","Plugins Matrix",1],["Skills","5 skills assigned","Skill Matrix",1]];
 return <><section className="ready"><div className="score"><b>90</b><small>%</small></div><span><p>SYSTEM READINESS</p><h2>เกือบพร้อมใช้งาน</h2><small>เหลือ 1 จุดที่ควรตรวจสอบก่อน Apply Configuration</small></span><button onClick={()=>go("MCP Matrix")}>ตรวจรายการที่เหลือ →</button></section>
 <div className="dash"><section className="card checklist"><Head pre="SETUP FLOW" title="Configuration checklist" tag="6 steps"/>{steps.map((s,i)=><button onClick={()=>go(s[2] as string)} key={s[0]}><i className={s[3]?"ok":"warn"}>{s[3]?"✓":"!"}</i><span><b>{i+1}. {s[0]}</b><small>{s[1]}</small></span><strong>›</strong></button>)}</section>
 <section className="card flow"><Head pre="ACTIVE FLOW" title="Feature (UI + API)" tag="● Ready"/><div><p><i>1</i><b>Frontend</b><b>Backend</b></p><em>↓ wait for all</em><p><i>2</i><b>DevOps</b></p><em>↓ wait for all</em><p><i>3</i><b>QA</b><b>Reviewer</b></p></div><button onClick={()=>go("Pipeline Builder")}>Open Pipeline Builder →</button></section></div></>
}
function Head({pre,title,tag}:{pre:string,title:string,tag:string}){return <header className="cardhead"><span><p>{pre}</p><h2>{title}</h2></span><em>{tag}</em></header>}

function Pipeline({change}:{change:()=>void}){
 const [hops,setHops]=useState([["Frontend","Backend"],["DevOps"],["QA","Reviewer"]]);
 return <section className="card pipeline"><div className="toolbar"><label><small>Editing template</small><select><option>Feature (UI + API)</option><option>Design Review</option></select></label><button>◉ Preview run</button><button>•••</button></div>
 <div className="palette"><span>Quick add</span>{roles.map((r,i)=><button style={{"--c":colors[i]} as React.CSSProperties} onClick={()=>{setHops(v=>[...v.slice(0,-1),[...v.at(-1)!,r]]);change()}} key={r}>＋ {r}</button>)}</div>
 {hops.map((h,i)=><div key={i}><article className="hop"><header><span><b>HOP {i+1}</b>{h.length>1&&<em>Parallel</em>}</span><button onClick={()=>{setHops(v=>v.filter((_,x)=>x!==i));change()}}>×</button></header><p>{h.map(r=><span className="chip" key={r}><i style={{background:colors[roles.indexOf(r)]}}/>{r}<b>×</b></span>)}<button onClick={()=>{setHops(v=>v.map((x,n)=>n===i?[...x,"Lead"]:x));change()}}>＋ Add role</button></p></article>{i<hops.length-1&&<small className="wait">↓ Wait for all agents to complete</small>}</div>)}
 <button className="add" onClick={()=>{setHops(v=>[...v,["Lead"]]);change()}}>＋ Add next hop</button><aside><b>✓ Pipeline valid</b><span>ทุก Role มี Provider และสิทธิ์ที่จำเป็นครบ</span><button>View details</button></aside></section>
}
function Templates({go}:{go:()=>void}){
 const list=[["Feature (UI + API)","3 hops · 5 roles"],["Design Review","2 hops · 3 roles"],["Quick Fix","2 hops · 3 roles"]],[active,setActive]=useState(0);
 return <section className="card split"><aside><input placeholder="⌕  Search templates"/>{list.map((x,i)=><button className={active===i?"selected":""} onClick={()=>setActive(i)} key={x[0]}><span><b>{x[0]}</b><small>{x[1]}</small></span><em>{i?"Built-in":"Active"}</em></button>)}<button>＋ New template</button></aside><article><header className="detail"><i>⌘</i><span><p>PIPELINE TEMPLATE</p><h2>{list[active][0]}</h2><small>{list[active][1]}</small></span><button>•••</button></header><div className="timeline"><p><b>1</b><span><strong>Frontend + Backend</strong><small>Run in parallel</small></span></p><i/><p><b>2</b><span><strong>DevOps</strong><small>Wait for all</small></span></p><i/><p><b>3</b><span><strong>QA + Reviewer</strong><small>Final verification</small></span></p></div><footer><button className="primary" onClick={go}>Edit pipeline</button><button>Duplicate</button><button>Export JSON</button></footer></article></section>
}
function Switch({on,click}:{on:boolean,click:()=>void}){return <button className={on?"switch on":"switch"} onClick={click}><i/></button>}
function Providers({tog,flip}:{tog:{[k:string]:boolean},flip:(k:string,f?:boolean)=>void}){
 const ps=[["Codex","OpenAI Codex CLI","gpt-5.3-codex"],["Cursor","Editor-native agent","gemini-2.5-pro"],["Gemini","Google Antigravity","Gemini 3.1 Pro"],["Kimi","Long-context reasoning","k2.6"],["OpenCode","Open agent runtime","Default"]];
 return <div className="stack"><section className="card table"><Head pre="MODEL CONNECTIONS" title="Providers" tag="＋ Connect provider"/>{ps.map((p,i)=><div className="prow" key={p[0]}><i>{p[0].slice(0,2)}</i><span><b>{p[0]}</b><small>{p[1]}</small></span><button>Test connection</button><select><option>{p[2]}</option></select><Switch on={tog[p[0]]??i<4} click={()=>flip(p[0],i<4)}/></div>)}</section>
 <section className="card table"><Head pre="AGENT ASSIGNMENT" title="Role mapping" tag="8 roles"/>{roles.map((r,i)=><div className="rrow" key={r}><b style={{"--c":colors[i]} as React.CSSProperties}><i/>{r}</b><select><option>{i%3?"Cursor":"Codex"}</option><option>Gemini</option></select><select><option>Default model</option></select><em>{i<2?"Full tools":"Custom access"}</em><button>Configure</button><Switch on={tog["r"+r]??true} click={()=>flip("r"+r,true)}/></div>)}</section></div>
}
function Overlap(){
 const [sel,setSel]=useState(0);return <div className="overlap"><aside className="card"><input placeholder="⌕  Search roles"/>{roles.map((r,i)=><button className={sel===i?"selected":""} onClick={()=>setSel(i)} key={r}><i style={{background:colors[i]}}/>{r}<span>{i===2?"1":"✓"}</span></button>)}</aside><section className="card editor"><header><span><p>INSTRUCTION SCOPE</p><h2>{roles[sel]}</h2></span><em className={sel===2?"warning":""}>{sel===2?"! 1 overlap":"✓ No overlap"}</em></header><div><article><small>ROLE INSTRUCTION</small><pre>{`# ${roles[sel]} specialist\n\nOwn tasks within the assigned scope.\nUse approved tools only.\nReport blockers to Lead before handoff.\n\n## Responsibilities\n- Execute assigned work\n- Validate output\n- Document decisions`}</pre></article><article className="empty"><b>{sel===2?"⚠ Shared deployment scope":"✓ Scope is clear"}</b><p>{sel===2?"Backend และ DevOps มีสิทธิ์แก้ deployment configuration เหมือนกัน":"ไม่พบ Instruction ที่ซ้ำหรือขัดแย้งกับ Role อื่น"}</p>{sel===2&&<button>Resolve conflict</button>}</article></div></section></div>
}
function Matrix({page,tog,flip}:{page:string,tog:{[k:string]:boolean},flip:(k:string,f?:boolean)=>void}){
 return <section className="card matrixcard"><div className="mtools"><input placeholder={"⌕  Search "+page.toLowerCase()}/><button>＋ Add item</button><button>Import policy</button></div><div className="scroll"><div className="matrix" style={{"--n":cols[page].length} as React.CSSProperties}><header><b>Role</b>{cols[page].map(c=><span key={c}><b>{c}</b><small>{page.split(" ")[0]}</small></span>)}</header>{roles.map((r,i)=><p key={r}><b style={{"--c":colors[i]} as React.CSSProperties}><i/>{r}</b>{cols[page].map((c,x)=>{let f=(i+x)%3!==0,k=page+r+c;return <span key={c}><Switch on={tog[k]??f} click={()=>flip(k,f)}/></span>})}</p>)}</div></div><footer><span><i/>Allowed</span><span><i/>Blocked</span><em>Security policy will be validated before Apply</em></footer></section>
}
function Skills({create}:{create:()=>void}){
 const skills=[["cockpit-ui-style","Design system"],["debug-mantra","Engineering"],["management-talk","Communication"],["post-mortem","Operations"],["scrutinize","Quality"]],[sel,setSel]=useState(0);
 return <section className="card split skills"><aside><input placeholder="⌕  Search skills"/>{skills.map((s,i)=><button className={sel===i?"selected":""} onClick={()=>setSel(i)} key={s[0]}><span><b>{s[0]}</b><small>{s[1]}</small></span><em>{i?"Local":"Active"}</em></button>)}<button onClick={create}>＋ Create skill</button></aside><article><header className="detail"><i>✦</i><span><p>{skills[sel][1]}</p><h2>{skills[sel][0]}</h2><small>Updated 2 hours ago · Used by 3 roles</small></span><button>•••</button></header><div className="meta"><span><small>DESCRIPTION</small><b>The shared design and interaction system for Takkub Cockpit.</b></span><span><small>SOURCE</small><code>.claude/skills/{skills[sel][0]}/SKILL.md</code></span></div><label>Instructions <em>Markdown</em><textarea defaultValue={`# ${skills[sel][0]}\n\nUse this skill when building or reviewing the Cockpit UI.\n\n## Principles\n- Keep configuration flows clear\n- Show validation before applying\n- Prefer readable labels over shorthand`}/></label><footer><button className="primary">Edit skill</button><button>Duplicate</button><button>View usage</button></footer></article></section>
}
function Users({invite}:{invite:()=>void}){let u=[["Admin User","admin@example.com","Owner"],["Narin P.","narin@example.com","Editor"],["QA Team","qa@example.com","Reviewer"]];return <section className="card table"><Head pre="ACCESS CONTROL" title="Workspace users" tag="＋ Invite user"/>{u.map((x,i)=><div className="user" key={x[1]}><i>{x[0].split(" ").map(y=>y[0]).join("")}</i><span><b>{x[0]}</b><small>{x[1]}</small></span><select><option>{x[2]}</option></select><em>● {i===2?"Pending":"Active"}</em><button onClick={invite}>•••</button></div>)}</section>}
function Audit(){return <section className="card audit"><div className="mtools"><input placeholder="⌕  Search activity"/><button>Filter</button><button>Export CSV</button></div>{[["Configuration applied","Admin User · Today, 08:42","Success"],["MCP policy updated","Narin P. · Yesterday, 16:18","Changed"],["Provider connection tested","Admin User · Yesterday, 15:04","Success"],["Role overlap detected","System · 22 Jul, 11:37","Warning"]].map(x=><article key={x[0]}><i>✓</i><span><b>{x[0]}</b><small>{x[1]}</small></span><em>{x[2]}</em><button>View details</button></article>)}</section>}
