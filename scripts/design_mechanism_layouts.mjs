/** Layouts only; all mechanism labels and relationships live in docs/diagrams. */
const definitions = [
  ['orchestration-modules','task-orchestration',
    [['planning',['planner','stage','reviewer']],['execution',['context','session','decision']],['runtime',['validate','dispatch','commit']]],
    ['identity','budgets'],{feedback:['runtime','planning']}],
  ['memory-assembly','memory-and-context',
    [['records',['raw','notes','facts']],['compaction',['trigger','summary','admit']],['assembly',['restore','project','recall']]],
    ['noteContract','sourceContract'],{bypass:['records','assembly'],feedback:['assembly','records']}],
  ['observation-package','observation-execution-consistency',
    [['capture',['pixels','tree','pairing']],['grounding',['package','registry','views']],['execution',['target','input','feedback']]],
    ['frameContract','effectContract'],{feedback:['execution','grounding']}],
  ['skill-delivery-flow','skill-delivery',
    [['discovery',['library','device','catalog']],['selection',['stage','core','scope']],['delivery',['roleBody','references','recipes']]],
    ['deliveryRecord','knowledgeBoundary'],{}],
  ['skill-learning-mechanisms','skill-evolution',
    [['research',['source','learner','candidate']],['review',['independent','criteria','admission']],['validation',['selective','utility','publish']]],
    ['binding','cost'],{feedback:['validation','research']}],
  ['hitl-mechanisms','hitl-design',
    [['questions',['ask','broker','continuation']],['steering',['inbox','revision','planner']],['runtime',['eligibility','inflight','lifecycle']]],
    ['replyContract','policyContract'],{bypass:['questions','runtime']}],
  ['recovery-mechanisms','crash-recovery',
    [['durable',['intent','receipt','postState']],['takeover',['detect','owner','fence']],['continuation',['reconcile','context','fresh']]],
    ['dispatchContract','accountingContract'],{}],
  ['observability-records','observability',
    [['facts',['calls','effects','observations']],['queries',['storage','joins','events']],['views',['details','preview','metrics']]],
    ['traceContract','measurementContract'],{feedback:['views','queries']}],
  ['evaluation-mechanisms','evaluation-design',
    [['freeze',['code','fixture','limits']],['execution',['initialize','harness','evidence']],['judgment',['oracle','cleanup','comparison']]],
    ['verdictContract','costContract'],{feedback:['judgment','execution']}],
  ['session-storage-map','session-storage',
    [['database',['state','records','transaction']],['files',['bodies','publication','retention']],['runtime',['memory','journal','restore']]],
    ['referenceContract','backupContract'],{feedback:['files','database']}],
];

export const mechanismLayouts=definitions.map(([id,doc,columns,contracts,options])=>{
  const groups={},boxes={},routes={};
  columns.forEach(([group,nodes],i)=>{
    const x=40+i*385;
    groups[group]={x,y:120,w:350,h:620,tone:i===0?'green':'blue'};
    nodes.forEach((node,j)=>{boxes[node]={x:x+24,y:210+j*180,w:302,h:144,tone:'blue'};});
  });
  groups.shared={x:40,y:825,w:1120,h:190,tone:'green',separators:[[600,900,600,996]]};
  contracts.forEach((node,i)=>{boxes[node]={x:64+i*560,y:900,w:512,h:96,tone:'green'};});
  // The recovery overview spans persistence, interruption handling and continuation.
  if(id==='recovery-mechanisms'){
    columns.forEach(([group,nodes])=>{
      groups[group].h=740;
      nodes.forEach((node,j)=>Object.assign(boxes[node],{y:210+j*220,h:184}));
    });
    groups.shared.y=945;
    groups.shared.separators=[[600,1020,600,1116]];
    contracts.forEach(node=>boxes[node].y=1020);
  }
  if(options.feedback){const[from,to]=options.feedback,a=groups[from],b=groups[to];
    routes[`${from}-${to}`]={from:'bottom',to:'bottom',via:[[a.x+175,780],[b.x+175,780]],labelAt:[(a.x+b.x)/2+175,771],dash:true};}
  if(options.bypass){const[from,to]=options.bypass,a=groups[from],b=groups[to];
    routes[`${from}-${to}`]={from:'top',to:'top',via:[[a.x+175,84],[b.x+175,84]],labelAt:[(a.x+b.x)/2+175,77],dash:true};}
  if(id==='orchestration-modules')routes['decision-reviewer']={labelAt:[407.5,626]};
  if(id==='skill-learning-mechanisms')routes['admission-publish']={labelAt:[792.5,626],labelSize:14};
  return {id,doc,kind:'mechanisms',height:id==='recovery-mechanisms'?1170:1050,groups,boxes,routes};
});
