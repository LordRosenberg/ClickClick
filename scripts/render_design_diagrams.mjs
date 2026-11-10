/** Rebuild bilingual design SVGs from docs/diagrams/*.mmd sources.
 * Layout metadata controls presentation only; labels and edges come from source files.
 * Supports the grouped-flow and state subset used by these design sources.
 * Run: node scripts/render_design_diagrams.mjs [--check]
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { parse as parseSequence, render as renderSequence } from './render_observation_sequences.mjs';
import { mechanismLayouts } from './design_mechanism_layouts.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const diagrams = [
  ...mechanismLayouts,
  {"id":"orchestration-round","doc":"task-orchestration","kind":"sequence"},
  {"id":"hitl-question","doc":"hitl-design","kind":"sequence"},
  {"id":"hitl-steering","doc":"hitl-design","kind":"sequence"},
  {"id":"recovery-lifecycle","doc":"crash-recovery","kind":"sequence"},
  {"id":"recovery-identities","doc":"crash-recovery","kind":"sequence"},
  {"id":"recovery-states","doc":"crash-recovery","kind":"state-lanes","height":870},
  {"id":"evaluation-lifecycle","doc":"evaluation-design","kind":"sequence"},
  {"id":"session-publish","doc":"session-storage","kind":"sequence"},
];

const escape=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const palette={blue:['#EDF3FA','#456B96'],green:['#EEF7F4','#287568'],purple:['#F5EFF9','#87659A']};
const font='Segoe UI, Microsoft YaHei, Arial, sans-serif';
function measure(s,size){return [...s].reduce((a,c)=>a+size*(/[\u2e80-\u9fff\uff00-\uffef]/u.test(c)?1:/\s/u.test(c)?.3:/[ilI.,:!|]/u.test(c)?.3:/[MW]/u.test(c)?.85:.56),0);}
function wrap(s,max,size){const out=[];let line='';for(const t of s.match(/[\u2e80-\u9fff\uff00-\uffef]|[^\s\u2e80-\u9fff\uff00-\uffef]+|\s+/gu)||[]){if(line&&measure(line+t,size)>max){out.push(line.trim());line=t.trimStart();}else line+=t;}if(line.trim())out.push(line.trim());return out;}
function parseGraph(source){
  const nodes=new Map(),groups=new Map(),edges=[],stack=[];
  for(const raw of source.trim().split(/\r?\n/)){
    const l=raw.trim();if(!l||l.startsWith('%%')||l.startsWith('flowchart ')||l==='stateDiagram-v2')continue;
    let m=l.match(/^subgraph (\w+)\["(.+)"\]$/u);
    if(m){assert(!nodes.has(m[1])&&!groups.has(m[1]));groups.set(m[1],{label:m[2],members:[]});stack.push(m[1]);continue;}
    if(l==='end'){assert(stack.length);stack.pop();continue;}
    m=l.match(/^(\w+)\["(.+)"\]$/u);
    if(!m){const state=l.match(/^state "(.+)" as (\w+)$/u);if(state)m=[state[0],state[2],state[1]];}
    if(m){assert(!nodes.has(m[1])&&!groups.has(m[1]));nodes.set(m[1],m[2]);if(stack.length)groups.get(stack.at(-1)).members.push(m[1]);continue;}
    m=l.match(/^(\w+) -->\|"(.+)"\| (\w+)$/u);
    if(m){edges.push({from:m[1],to:m[3],label:m[2]});continue;}
    m=l.match(/^(\w+) --> (\w+)(?:: (.+))?$/u);assert(m,`Unknown graph line: ${l}`);edges.push({from:m[1],to:m[2],label:m[3]||''});
  }
  assert.equal(stack.length,0);
  for(const e of edges)assert((nodes.has(e.from)||groups.has(e.from))&&(nodes.has(e.to)||groups.has(e.to)));
  return {nodes,groups,edges};
}
function anchor(b,side){return side==='left'?[b.x,b.y+b.h/2]:side==='right'?[b.x+b.w,b.y+b.h/2]:side==='top'?[b.x+b.w/2,b.y]:[b.x+b.w/2,b.y+b.h];}
function renderPanels(graph,meta,title,language){
  assert.deepEqual([...graph.nodes.keys()].sort(),Object.keys(meta.boxes).sort(),`Node layout mismatch: ${meta.id}`);
  assert.deepEqual([...graph.groups.keys()].sort(),Object.keys(meta.groups).sort(),`Group layout mismatch: ${meta.id}`);
  const parts=[`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="${meta.height}" viewBox="0 0 1200 ${meta.height}" role="img" aria-labelledby="title desc">`,
    `<title id="title">${escape(title)}</title><desc id="desc">${escape(language==='zh-CN'?'分组表示职责和归属，组间箭头表示整体交互或流程；具体时序见正文。':'Groups express responsibilities and ownership; arrows between groups express overall interaction or flow. See the text for timing.')}</desc>`,
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 1L9 5L0 9Z" fill="#6686A8"/></marker></defs>',
    `<rect width="1200" height="${meta.height}" fill="white"/>`,
    `<text x="40" y="43" font-family="${font}" font-size="25" font-weight="500" fill="#263449">${escape(title)}</text>`];
  const text=(x,y,s,size=18,color='#586575',weight=400)=>parts.push(`<text x="${x}" y="${y}" font-family="${font}" font-size="${size}" font-weight="${weight}" fill="${color}">${escape(s)}</text>`);
  const rects={...meta.groups,...meta.boxes};
  for(const[id,g]of graph.groups){
    const b=meta.groups[id],header=palette[b.tone][0],labels=g.label.split(' / ');
    assert(labels.length<=2&&measure(labels[0],20)<=b.w-44&&measure(labels[1]||'',16)<=b.w-44,`Group title does not fit: ${meta.id}/${language}/${id}`);
    parts.push(`<g data-group="${id}" data-label="${escape(g.label)}"><defs><clipPath id="clip-${id}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8"/></clipPath></defs><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8" fill="white"/><rect x="${b.x}" y="${b.y}" width="${b.w}" height="66" fill="${header}" clip-path="url(#clip-${id})"/><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8" fill="none" stroke="#BDCFDF" stroke-width="1.2"/>`);
    text(b.x+22,b.y+28,labels[0],20,'#365D84',500);if(labels[1])text(b.x+22,b.y+51,labels[1],16);
    for(const[x1,y1,x2,y2]of b.separators||[])parts.push(`<path d="M${x1} ${y1}L${x2} ${y2}" stroke="#DCE5ED" stroke-width="1"/>`);
    for(const child of g.members){const c=meta.boxes[child];assert(c.x>=b.x+16&&c.x+c.w<=b.x+b.w-16&&c.y>=b.y+66&&c.y+c.h<=b.y+b.h-12,`Child outside group: ${meta.id}/${child}`);}
    parts.push('</g>');
  }
  for(const e of graph.edges){
    const a=rects[e.from],b=rects[e.to],r=meta.routes?.[`${e.from}-${e.to}`]||{},horizontal=Math.abs(a.y-b.y)<20;
    const start=r.start||anchor(a,r.from||(horizontal?(a.x<b.x?'right':'left'):a.y<b.y?'bottom':'top'));
    const finish=r.finish||anchor(b,r.to||(horizontal?(a.x<b.x?'left':'right'):a.y<b.y?'top':'bottom'));
    const points=[start,...(r.via||[]),finish];
    for(let i=1;i<points.length;i++)assert(points[i-1][0]===points[i][0]||points[i-1][1]===points[i][1],`Connector must be orthogonal: ${meta.id}`);
    parts.push(`<path data-from="${e.from}" data-to="${e.to}" data-label="${escape(e.label)}" d="${points.map((p,i)=>(i?'L':'M')+p.join(' ')).join(' ')}" fill="none" stroke="#6686A8" stroke-width="1.6"${r.dash?' stroke-dasharray="6 4"':''} marker-end="url(#arrow)"/>`);
    if(e.label){
      const local=!r.labelAt&&start[0]===finish[0]&&!graph.groups.has(e.from),size=r.labelSize??(local?14:16);
      const lines=wrap(e.label,local?130:740,size),x=r.labelAt?.[0]??(local?start[0]+14:(start[0]+finish[0])/2),y=r.labelAt?.[1]??(start[1]+finish[1])/2;
      assert(!local||lines.length===1,`Local edge label too long: ${meta.id}/${e.label}`);
      if(!local){const w=Math.max(...lines.map(l=>measure(l,size)))+20;parts.push(`<rect x="${x-w/2}" y="${y-16}" width="${w}" height="${lines.length*22+6}" fill="white"/>`);}
      lines.forEach((l,i)=>text(local?x:x-measure(l,size)/2,y+4+i*22,l,size));
    }
  }
  const owned=new Set([...graph.groups.values()].flatMap(g=>g.members));
  for(const[id,label]of graph.nodes){
    const b=meta.boxes[id],bits=label.split(' / '),body=bits.slice(1).flatMap(s=>wrap(s,b.w-36,18));
    assert(measure(bits[0],19)<=b.w-36&&body.every(line=>measure(line,18)<=b.w-36)&&52+(body.length-1)*24<=b.h-12,`Panel text does not fit: ${meta.id}/${language}/${id}`);
    parts.push(`<g data-node="${id}" data-label="${escape(label)}">`);
    if(!owned.has(id))parts.push(`<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8" fill="${palette[b.tone][0]}" stroke="#C5DBD4" stroke-width="1"/>`);
    text(b.x+18,b.y+26,bits[0],19,'#263449',500);body.forEach((line,i)=>text(b.x+18,b.y+52+i*24,line));parts.push('</g>');
  }
  parts.push('</svg>');return parts.join('\n')+'\n';
}
function renderRecoveryStates(graph,meta,title,language){
  const zh=language==='zh-CN',seenNodes=new Set(),seenEdges=new Set();
  assert.deepEqual([...graph.nodes.keys()].sort(),['running','recovering','queued','waiting','blocked','paused','terminal'].sort());
  const parts=[`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="${meta.height}" viewBox="0 0 1200 ${meta.height}" role="img" aria-labelledby="title desc">`,
    `<title id="title">${escape(title)}</title><desc id="desc">${escape(zh?'上方为恢复主流程，中间为资源分支，下方为控制与终止。返回路径用条件和目标状态标注，完整保留状态转换。':'Main recovery flow above, resource branches in the middle, control and termination below. Return paths use condition and destination labels; all transitions are retained.')}</desc>`,
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 1L9 5L0 9Z" fill="#6686A8"/></marker></defs>',
    `<rect width="1200" height="${meta.height}" fill="white"/>`];
  const text=(x,y,value,size=18,color='#586575',weight=400,align='start')=>parts.push(`<text x="${x}" y="${y}" font-family="${font}" font-size="${size}" font-weight="${weight}" fill="${color}" text-anchor="${align}">${escape(value)}</text>`);
  const primary=id=>graph.nodes.get(id).split(' / ')[0];
  const edge=(from,to)=>{const e=graph.edges.find(e=>e.from===from&&e.to===to);assert(e,`Missing recovery transition: ${from}/${to}`);const key=`${from}/${to}`;assert(!seenEdges.has(key));seenEdges.add(key);return e;};
  const edgeGroup=e=>parts.push(`<g data-from="${e.from}" data-to="${e.to}" data-label="${escape(e.label)}">`);
  function state(id,x,y,w,h){
    const label=graph.nodes.get(id),bits=label.split(' / '),lines=bits.slice(1).flatMap(s=>wrap(s,w-36,18));
    assert(measure(bits[0],19)<=w-36&&52+(lines.length-1)*23<=h-10,`Recovery state text does not fit: ${language}/${id}`);
    seenNodes.add(id);parts.push(`<g data-node="${id}" data-label="${escape(label)}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="7" fill="#EDF3FA" stroke="#BDCFDF" stroke-width="1.2"/>`);
    text(x+18,y+28,bits[0],19,'#263449',500);lines.forEach((l,i)=>text(x+18,y+55+i*23,l));parts.push('</g>');
  }
  text(40,43,title,25,'#263449',500);
  text(40,91,zh?'恢复主流程':'Main recovery flow',19,'#365D84',500);
  const main=['running','recovering','queued'];
  main.forEach((id,i)=>state(id,40+i*400,110,320,112));
  for(let i=0;i<2;i++){
    const e=edge(main[i],main[i+1]),start=360+i*400,end=440+i*400;assert(measure(e.label,16)<end-start);
    edgeGroup(e);parts.push(`<path d="M${start} 166H${end}" stroke="#6686A8" stroke-width="1.6" marker-end="url(#arrow)"/>`);text((start+end)/2,148,e.label,16,'#586575',400,'middle');parts.push('</g>');
  }
  const schedule=edge('queued','running');edgeGroup(schedule);text(840,257,`${schedule.label} → ${primary('running')}`,16);parts.push('</g>');
  parts.push('<rect x="40" y="300" width="1120" height="320" rx="8" fill="white" stroke="#BDCFDF" stroke-width="1.2"/><path d="M48 300H1152Q1160 300 1160 308V366H40V308Q40 300 48 300Z" fill="#EEF7F4"/>');
  text(62,328,zh?'资源等待与有界重查':'Resource waits and bounded rechecks',20,'#365D84',500);
  text(62,352,zh?'以下分支从 recovering 进入，后续条件直接标明目标状态':'Branches enter from recovering; follow-up conditions name their destination state',16);
  parts.push('<path d="M64 496H1136" stroke="#DCE5ED" stroke-width="1"/>');
  for(const[id,y]of[['waiting',390],['blocked',510]]){
    const incoming=edge('recovering',id),condition=wrap(incoming.label,280,18);assert(condition.length<=2);
    edgeGroup(incoming);condition.forEach((l,i)=>text(64,y+37+i*23,l));parts.push(`<path d="M354 ${y+48}H430" stroke="#6686A8" stroke-width="1.6" marker-end="url(#arrow)"/></g>`);
    state(id,430,y,340,96);
    let outcomeY=y+30;
    for(const candidate of graph.edges.filter(e=>e.from===id)){
      const e=edge(candidate.from,candidate.to),lines=wrap(`${e.label} → ${primary(e.to)}`,326,16);
      edgeGroup(e);lines.forEach((l,i)=>text(810,outcomeY+i*22,l,16));parts.push('</g>');outcomeY+=lines.length*22+14;
    }
    assert(outcomeY-14<=y+90,`Recovery follow-up text does not fit: ${language}/${id}`);
  }
  text(40,673,zh?'控制与终止':'Control and termination',19,'#365D84',500);
  for(const[id,x]of[['paused',40],['terminal',620]]){
    const e=edge('recovering',id),label=graph.nodes.get(id),heading=`${e.label} → ${primary(id)}`,body=label.split(' / ').slice(1).flatMap(s=>wrap(s,492,18));
    assert(measure(heading,18)<=492&&body.length<=2,`Recovery control text does not fit: ${language}/${id}`);
    edgeGroup(e);parts.push(`<g data-node="${id}" data-label="${escape(label)}"><rect x="${x}" y="695" width="540" height="134" rx="8" fill="white" stroke="#BDCFDF" stroke-width="1.2"/>`);
    text(x+24,731,heading,18,'#365D84',500);body.forEach((l,i)=>text(x+24,771+i*24,l));parts.push('</g></g>');seenNodes.add(id);
  }
  assert.equal(seenNodes.size,graph.nodes.size);assert.equal(seenEdges.size,graph.edges.length,'Every recovery transition must be represented');
  parts.push('</svg>');return parts.join('\n')+'\n';
}
export function rebuild(check=false){let count=0;for(const meta of diagrams)for(const[language,suffix]of[['en',''],['zh-CN','.zh-CN']]){
  const md=readFileSync(resolve(root,`docs/${meta.doc}${suffix}.md`),'utf8');
  const block=md.match(new RegExp(`<!-- design-diagram:${meta.id} -->[\\s\\S]*?<!-- /design-diagram:${meta.id} -->`));assert(block,`Missing diagram: ${meta.id}/${language}`);
  const sourcePath=`diagrams/design-${meta.id}${suffix}.mmd`;
  assert(block[0].includes(`](${sourcePath})`),`Missing source link: ${meta.id}/${language}`);
  const source=readFileSync(resolve(root,'docs',sourcePath),'utf8');
  const title=block[0].match(/!\[([^\]]+)\]/)?.[1];assert(title);
  const svg=meta.kind==='sequence'?renderSequence(parseSequence(source),title,language):meta.kind==='panels'||meta.kind==='mechanisms'?renderPanels(parseGraph(source),meta,title,language):renderRecoveryStates(parseGraph(source),meta,title,language);
  const output=resolve(root,`docs/assets/design-${meta.id}${suffix}.svg`);
  if(check)assert.equal(readFileSync(output,'utf8'),svg,`Stale SVG: ${output}`);
  else if(!existsSync(output)||readFileSync(output,'utf8')!==svg)writeFileSync(output,svg,'utf8');
  count++;
}return count;}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url))console.log(`${process.argv.includes('--check')?'Verified':'Rendered'} ${rebuild(process.argv.includes('--check'))} bilingual design SVGs.`);
