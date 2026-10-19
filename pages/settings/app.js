
const FIELD_UI={
 'sys001.group.name_semantic_timeout_seconds':{u:'秒',r:[1,null]},
 'sys001.group.natural_participation_probability':{u:'%',r:[0,100]},
 'sys001.group.natural_context_max_messages':{u:'条',r:[1,200]},
 'sys001.group.natural_context_max_chars':{u:'字符',r:[1000,64000]},
 'sys001.group.natural_decision_timeout_seconds':{u:'秒',r:[1,null]},
 'sys001.group.natural_reply_cooldown_seconds':{u:'秒',r:[1,3600]},
 'sys001.group.natural_frequency_window_minutes':{u:'分钟',r:[1,1440]},
 'sys001.group.natural_max_replies_per_window':{u:'轮',r:[1,20]},
 'sys001.group.natural_no_action_backoff_base_seconds':{u:'秒',r:[1,300]},
 'sys001.group.natural_no_action_backoff_max_seconds':{u:'秒',r:[1,3600]},
 'sys001.group.continuous_window_seconds':{u:'秒',r:[1,60]},
 'sys001.group.continuous_window_max_scopes':{u:'个',r:[1,256]},
 'sys001.character_dialogue.preparation.timeout_seconds':{u:'秒',r:[1,null]},
 'sys001.final_review.timeout_seconds':{u:'秒',r:[1,null]},
 'sys001.final_review.max_repair_attempts':{u:'次',r:[0,3]},
 'sys001.master_alert.window_minutes':{u:'分钟',r:[1,null]},
 'sys001.lifecycle.shutdown_grace_seconds':{u:'秒',r:[1,120]},
 'sys001.presentation.text_component_max_segments':{u:'段'},
 'sys001.presentation.text_component_min_segments':{u:'段'},
 'sys001.presentation.bubble_send_min_wait_seconds':{u:'秒',r:[0,null]},
 'sys001.presentation.bubble_send_max_wait_seconds':{u:'秒',r:[0,null]},
 'sys001.presentation.model_segment_timeout_seconds':{u:'秒',r:[1,null]},
 'sys001.identity.master_relationship_prompt':{e:'留空不追加语气补充；这里不是主人名单或完整人格。'},
 'sys001.group.name_semantic_prompt':{e:'留空时保持为空；判断结果格式仍由插件固定。'},
 'sys001.group.natural_participation_prompt':{e:'已有自定义内容会保留；可恢复默认载入模板；留空时保持为空。'},
 'sys001.character_dialogue.situation_prompt':{e:'留空不添加本项；可用恢复默认找回。'},
 'sys001.character_dialogue.expression_prompt':{e:'留空不添加本项；可用恢复默认找回。'},
 'sys001.character_dialogue.preparation.prompt':{e:'留空时不添加自定义指导；可用恢复默认找回原文。'},
 'sys001.final_review.core_prompt':{e:'留空回到默认基础规则。'},
 'sys001.final_review.additional_prompt':{e:'留空表示不加要求，不会强制替换默认。'}
};
const ROOT='sys001';
function normalizeSchema(raw){
 function walk(src){
  const dst={};
  if(src.d!==undefined) dst.d=src.d;
  else if(src.description!==undefined) dst.d=src.description;
  if(src.h!==undefined) dst.h=src.h;
  else if(src.hint!==undefined) dst.h=src.hint;
  const type=src.type||src.t;
  const childItems=src.items||src.i;
  if(type==='object'||type==='o'||(!type&&childItems)){
   dst.t='o';
   dst.i={};
   Object.keys(childItems||{}).forEach(function(k){dst.i[k]=walk(childItems[k]);});
  }else if(type==='string'){
   dst.t='string';
  }else if(type==='text'){
   dst.t='text';
  }else if(type==='bool'||type==='boolean'){
   dst.t='bool';
  }else if(type==='int'||type==='integer'){
   dst.t='int';
  }else if(type==='float'||type==='number'){
   dst.t='float';
  }else if(type==='list'){
   dst.t='list';
  }else{
   dst.t='string';
  }
  if(src.o!==undefined) dst.o=src.o;
  else if(src.options!==undefined) dst.o=src.options;
  if(src.l!==undefined) dst.l=src.l;
  else if(src.labels!==undefined) dst.l=src.labels;
  if(src.c!==undefined) dst.c=src.c;
  else if(src.condition!==undefined) dst.c=src.condition;
  if(src._special==='select_provider') dst.s='provider';
  if(src._special==='select_providers') dst.s='providers';
  if(Object.prototype.hasOwnProperty.call(src,'v')) dst.v=src.v;
  else if(Object.prototype.hasOwnProperty.call(src,'default')) dst.v=src.default;
  return dst;
 }
 const hasWrapper=raw&&(raw.type||raw.t||raw.items||raw.i);
 const root=walk(hasWrapper?raw:{type:'object',items:raw||{}});
 function applyUI(node,path){
  const items=node.i||{};
  Object.keys(items).forEach(function(k){
   const p=path.concat(k);
   const child=items[k];
   const ui=FIELD_UI[p.join('.')];
   if(ui) Object.assign(child,ui);
   if(child.t==='o') applyUI(child,p);
  });
 }
 applyUI(root,[ROOT]);
 return root;
}
const bridge=window.AstrBotPluginPage;
let MODEL_WARNING='';
function escText(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&#39;');}
async function mount(settings,providers){
 const versionLabel = document.getElementById('pluginVersion');
 if (versionLabel) versionLabel.textContent = settings.version || '';
 const flat=[];
 const byPath={};
 function flatten(node,path,inherited){
  const items=node.i||{};
  Object.keys(items).forEach(function(k){
   const ch=items[k];
   const p=path.concat(k);
   const conds=inherited.slice();
   if(ch.c) conds.push({cond:ch.c,scope:path});
   if(ch.t==='o'){flatten(ch,p,conds);return;}
   const f=Object.assign({},ch,{path:p.join('.'),parent:path.join('.'),conds:conds});
   flat.push(f);
   byPath[f.path]=f;
  });
 }
 const SCHEMA=normalizeSchema(settings.schema[ROOT]);
 flatten(SCHEMA,[ROOT],[]);
const PAGES=[
 {id:'scope',name:'范围与工具',title:'范围与工具',sub:'先决定谁能进入聊天，再决定普通群友能看到哪些工具。',sections:[
  {title:'聊天范围',desc:'入口名单不代替 AstrBot 权限；主人身份由 AstrBot 判断。',fields:['ingress.private_allowed_sender_ids','ingress.blocked_sender_ids','ingress.group_allowed_scopes','ingress.group_blocked_scopes']},
  {title:'普通群友工具范围',desc:'只收窄普通群友可见工具；主人和私聊不受这层过滤影响。',fields:['capability_visibility.friendly_sender_ids','capability_visibility.ordinary_capability_names']}
 ]},
 {id:'wake',name:'唤醒与连发',title:'唤醒与连发',sub:'名称唤醒只在点名时工作；连续消息把同一段话合并成一轮。',sections:[
  {title:'名称唤醒',desc:'识别 AstrBot 已配置的唤醒词；不会自动读取人格名称。',fields:['group.name_wake_mode']},
  {title:'高级：名称判断参数',advanced:true,desc:'仅在“模型判断”时生效；直接匹配不会调用模型。',fields:['group.name_semantic_prompt','group.name_semantic_provider_id','group.name_semantic_streaming','group.name_semantic_fallback_provider_ids','group.name_semantic_timeout_seconds']},
  {title:'连续消息',desc:'群聊和私聊分别等待与统计；数值越大，首条回复等待越久。',fields:['group.continuous_window_enabled','group.continuous_window_seconds','group.continuous_window_max_scopes']}
 ]},
 {id:'natural',name:'自然参与',title:'自然参与',sub:'没有点名时，Shio 也可以判断是否自然地接一句话。默认关闭。',sections:[
  {title:'总开关与参与范围',desc:'未点名时才判断；@、引用机器人与官方唤醒不受此开关控制。',fields:['group.natural_participation_enabled','group.natural_group_scopes']},
  {title:'参与判断',desc:'模型先判断适不适合接话，再按概率决定是否回复。',fields:['group.natural_participation_probability','group.natural_context_max_messages','group.natural_context_max_chars','group.natural_participation_prompt']},
  {title:'高级：判断模型与时限',advanced:true,desc:'只做简短判断，正式回复仍使用当前聊天模型。',fields:['group.natural_decision_provider_id','group.natural_decision_streaming','group.natural_decision_fallback_provider_ids','group.natural_decision_timeout_seconds']},
  {title:'节奏与限额',desc:'一轮发多个文字气泡仍只计一次；只限制自然参与。',fields:['group.natural_reply_cooldown_seconds','group.natural_frequency_window_minutes','group.natural_max_replies_per_window']},
  {title:'高级：不参与后的退避',advanced:true,desc:'决定不参与后，至少等待一段时间再判断新消息；不计回复次数。',fields:['group.natural_no_action_backoff_base_seconds','group.natural_no_action_backoff_max_seconds']}
 ]},
 {id:'character',name:'角色与主人',title:'角色与主人',sub:'在现有人格上补充语气与表达指导；不重设人格，也不授予权限。',sections:[
  {title:'主人对话风格',desc:'只对 AstrBot 确认的主人追加；不是主人名单。',fields:['identity.master_relationship_enabled','identity.master_relationship_prompt']},
  {title:'角色化自然表达',desc:'默认关闭；开启后只添加表达指导，本身不额外调用模型。',fields:['character_dialogue.enabled','character_dialogue.situation_prompt','character_dialogue.expression_prompt','character_dialogue.preserve_character_in_review']},
  {title:'高级：回复准备',advanced:true,desc:'独立且默认关闭的额外准备步骤；涉及知识库文档时才实际检索。',fields:['character_dialogue.preparation.enabled','character_dialogue.preparation.provider_id','character_dialogue.preparation.streaming','character_dialogue.preparation.timeout_seconds','character_dialogue.preparation.prompt']}
 ]},
 {id:'review',name:'发送前审核',title:'发送前审核',sub:'正式回复先检查再发送；规则可编辑、可恢复默认，未选中的规则不生效。',sections:[
  {title:'审核方式与规则',desc:'基础/附加只读对应规则；“基础加附加”两组一起使用。',fields:['final_review.mode','final_review.core_prompt','final_review.additional_prompt']},
  {title:'审核模型与时间',desc:'每轮多一次检查，允许改写后还需复审，会增加等待。',fields:['final_review.provider_id','final_review.streaming','final_review.fallback_provider_ids','final_review.timeout_seconds']},
  {title:'高级：改写与失败处理',advanced:true,desc:'改写需同时开启“允许审核后改写”和“采用改写后的回复”。',fields:['final_review.repair_enabled','final_review.max_repair_attempts','final_review.use_repaired_text','final_review.send_last_reply_on_review_exhausted']}
 ]},
 {id:'bubble',name:'文字气泡',title:'文字气泡',sub:'控制回复拆成几个气泡、间隔多久；表情包由 Meme 单独发送，另计。',sections:[
  {title:'分段与数量',desc:'上限不是硬性三段；单条发送时数量与间隔不生效。',fields:['presentation.text_component_mode','presentation.text_component_max_segments','presentation.text_component_min_segments']},
  {title:'气泡间隔',desc:'后续气泡发送前随机等待；两项都为 0 表示不额外等待。',fields:['presentation.bubble_send_min_wait_seconds','presentation.bubble_send_max_wait_seconds']},
  {title:'高级：模型分段参数',advanced:true,desc:'仅在“模型分段”时生效；只决定文字边界，不能改写正式回复。',fields:['presentation.model_segment_provider_id','presentation.model_segment_streaming','presentation.model_segment_fallback_provider_ids','presentation.model_segment_timeout_seconds']}
 ]},
 {id:'notify',name:'通知与重载',title:'通知与重载',sub:'故障私聊提醒主人；重载或停用时给已接收的回复留出完成时间。',sections:[
  {title:'故障通知',desc:'正式回复或审核首次失败时私聊主人；自然参与决定不说话不算故障。',fields:['master_alert.master_alert_enabled','master_alert.main_reply_exhausted_enabled','master_alert.review_repair_exhausted_enabled','master_alert.window_minutes','master_alert.master_alert_quiet_enabled']},
  {title:'勿扰时间',advanced:true,desc:'按下面时区决定起止；跨午夜用 23:00 到 08:00 表示。',fields:['master_alert.quiet_start','master_alert.quiet_end','master_alert.display_timezone']},
  {title:'重载与停用',desc:'超时停止剩余任务；不在群里报错，也不补发旧消息。',fields:['lifecycle.shutdown_grace_seconds']}
 ]}
];
PAGES.forEach(function(page){
 page.sections.forEach(function(sec){
  sec.fields=sec.fields.map(function(sp){
   const f=byPath[ROOT+'.'+sp];
   if(!f) return null;
   f.page=page.id;
   return f;
  }).filter(Boolean);
 });
 page.allFields=page.sections.reduce(function(a,s){return a.concat(s.fields);},[]);
 page.count=page.allFields.length;
});
const STREAM_OF={
 'sys001.group.name_semantic_provider_id':'sys001.group.name_semantic_streaming',
 'sys001.group.natural_decision_provider_id':'sys001.group.natural_decision_streaming',
 'sys001.final_review.provider_id':'sys001.final_review.streaming',
 'sys001.presentation.model_segment_provider_id':'sys001.presentation.model_segment_streaming',
 'sys001.character_dialogue.preparation.provider_id':'sys001.character_dialogue.preparation.streaming'
};
const EMBEDDED=new Set(Object.keys(STREAM_OF).map(function(k){return STREAM_OF[k];}));
function clone(o){return o===undefined?undefined:JSON.parse(JSON.stringify(o));}
function sameValue(a,b){return JSON.stringify(a)===JSON.stringify(b);}
const defaults={};
flat.forEach(function(f){defaults[f.path]=clone(f.v);});
function readPath(config,path){
 const parts=String(path).split('.');
 const tries=[parts];
 if(parts[0]===ROOT) tries.push(parts.slice(1));
 for(let t=0;t<tries.length;t++){
  let cur=config;
  let ok=true;
  for(let i=0;i<tries[t].length;i++){
   if(cur===null||typeof cur!=='object'||!Object.prototype.hasOwnProperty.call(cur,tries[t][i])){ok=false;break;}
   cur=cur[tries[t][i]];
  }
  if(ok) return {found:true,value:cur};
 }
 return {found:false};
}
function initialFrom(config){
 const out={};
 flat.forEach(function(f){
  const r=readPath(config,f.path);
  out[f.path]=r.found?clone(r.value):clone(f.v);
 });
 return out;
}
const initial=initialFrom(settings.config);
let draft=clone(initial);
let saved=clone(initial);
let currentPage='natural';
let activeRulePath=null;
let revision=settings.revision;
let saving=false;
const PROVIDERS=Array.isArray(providers)?providers.filter(function(p){return p&&p.id!==undefined&&p.id!==null&&String(p.id)!=='';}):[];
const MODEL_LABEL={'':'跟随当前会话模型'};
PROVIDERS.forEach(function(p){
 const id=String(p.id);
 MODEL_LABEL[id]=p.model?String(p.model)+'（'+id+'）':id;
});
const pageRoot=document.getElementById('pageRoot');
const navA=document.getElementById('navA');
const navB=document.getElementById('navB');
const dirtyBadge=document.getElementById('dirtyBadge');
const dirtyText=document.getElementById('dirtyText');
const saveBtn=document.getElementById('saveBtn');
const discardBtn=document.getElementById('discardBtn');
const themeBtn=document.getElementById('themeBtn');
const searchInput=document.getElementById('searchInput');
const results=document.getElementById('searchResults');
const drawerBtn=document.getElementById('drawerBtn');
const overlay=document.getElementById('overlay');
const ruleDialog=document.getElementById('ruleDialog');
const ruleTitle=document.getElementById('ruleTitle');
const rulePath=document.getElementById('rulePath');
const ruleHint=document.getElementById('ruleHint');
const ruleText=document.getElementById('ruleText');
const ruleEmpty=document.getElementById('ruleEmpty');
const ruleDefault=document.getElementById('ruleDefault');
const ruleRestore=document.getElementById('ruleRestore');
const ruleCancel=document.getElementById('ruleCancel');
const ruleApply=document.getElementById('ruleApply');
const toast=document.getElementById('toast');
const toastMain=document.getElementById('toastMain');
const toastSub=document.getElementById('toastSub');
let toastTimer=null;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&#39;');}
function slug(p){return p.split('.').join('__');}
function truncate(s,n){const t=String(s).split(String.fromCharCode(10)).join(' ').trim();return t.length>n?t.slice(0,n)+'…':t;}
function joinPath(scope,key){return scope.concat(key).join('.');}
function brokenConds(f){
 const out=[];
 f.conds.forEach(function(c){
  Object.keys(c.cond).forEach(function(k){
   const cp=joinPath(c.scope,k);
   if(draft[cp]!==c.cond[k]) out.push({path:cp,label:(byPath[cp]||{d:k}).d});
  });
 });
 return out;
}
function typeBadge(f){
 if(f.s==='provider') return '模型';
 if(f.s==='providers') return '备用模型';
 if(f.t==='bool') return '开关';
 if(f.t==='list') return '列表';
 if(f.t==='text') return '规则文本';
 if(f.t==='int') return '整数';
 if(f.t==='float') return '小数';
 if(f.o) return '单选';
 return '文本';
}
function renderBool(f,val){
 return '<label class=\'switch\'><input type=\'checkbox\' data-kind=\'bool\' data-path=\''+f.path+'\' aria-label=\''+esc(f.d)+'\' '+(val?'checked':'')+'><span class=\'track\' aria-hidden=\'true\'></span><span class=\'state\'>'+(val?'已开启':'已关闭')+'</span></label>';
}
function renderSelect(f,val){
 const opts=(f.o||[]).map(function(v,i){return '<option value=\''+esc(v)+'\' '+(val===v?'selected':'')+'>'+esc((f.l&&f.l[i])||v)+'</option>';}).join('');
 return '<label class=\'sr-only\' for=\'sel-'+slug(f.path)+'\'>'+esc(f.d)+'</label><select id=\'sel-'+slug(f.path)+'\' data-kind=\'select\' data-path=\''+f.path+'\'>'+opts+'</select>';
}
function renderNumber(f,val){
 return '<div class=\'num-row\'><label class=\'sr-only\' for=\'num-'+slug(f.path)+'\'>'+esc(f.d)+'</label><input id=\'num-'+slug(f.path)+'\' type=\'number\' inputmode=\'decimal\' step=\''+(f.t==='float'?'0.1':'1')+'\' data-kind=\'num\' data-path=\''+f.path+'\' value=\''+esc(val)+'\'><span class=\'unit\'>'+esc(f.u||'')+'</span></div><p class=\'valid-msg\' data-valid-path=\''+f.path+'\' role=\'status\'></p>';
}
function renderString(f,val){
 const isTime=f.path==='sys001.master_alert.quiet_start'||f.path==='sys001.master_alert.quiet_end';
 return '<label class=\'sr-only\' for=\'txt-'+slug(f.path)+'\'>'+esc(f.d)+'</label><input id=\'txt-'+slug(f.path)+'\' type=\''+(isTime?'time':'text')+'\' data-kind=\'text\' data-path=\''+f.path+'\' value=\''+esc(val)+'\' placeholder=\'例如 +08:00\'>';
}
function renderProvider(f,val){
 const streamPath=STREAM_OF[f.path];
 let streamHTML='';
 let streamHint='';
 if(streamPath&&byPath[streamPath]){
  const sf=byPath[streamPath];
  const sv=draft[streamPath];
  streamHTML='<div class=\'stream-inline\' id=\'field-'+slug(streamPath)+'\' tabindex=\'-1\' title=\''+sf.path+'\'><span class=\'stream-label\'>'+esc(sf.d)+'</span><label class=\'switch compact\'><input type=\'checkbox\' data-kind=\'bool\' data-path=\''+streamPath+'\' aria-label=\''+esc(sf.d)+'\' '+(sv?'checked':'')+'><span class=\'track\' aria-hidden=\'true\'></span><span class=\'state\'>'+(sv?'已开启':'已关闭')+'</span></label></div>';
  streamHint='<details class=\'hint\'><summary>'+esc(sf.d)+' · 字段说明</summary><p>'+esc(sf.h)+'</p></details>';
 }
 let opts='<option value=\'\' '+(val===''?'selected':'')+'>跟随当前会话模型</option>';
 PROVIDERS.forEach(function(p){
  const id=String(p.id);
  const label=p.model?String(p.model)+'（'+id+'）':id;
  opts+='<option value=\''+esc(id)+'\' '+(val===id?'selected':'')+'>'+esc(label)+'</option>';
 });
 if(val&&!PROVIDERS.some(function(p){return String(p.id)===val;})){
  opts+='<option value=\''+esc(val)+'\' selected>当前不可用，保留设置（'+esc(val)+'）</option>';
 }
 return '<div class=\'model-row\'><label class=\'sr-only\' for=\'sel-'+slug(f.path)+'\'>'+esc(f.d)+'</label><select id=\'sel-'+slug(f.path)+'\' data-kind=\'select\' data-path=\''+f.path+'\'>'+opts+'</select>'+streamHTML+'</div><p class=\'field-note\'>空值 = 跟随当前会话模型。'+(MODEL_WARNING?' '+esc(MODEL_WARNING):'')+'</p>'+streamHint;
}
function renderProviderList(f,val){
 const arr=Array.isArray(val)?val:[];
 const known={};
 PROVIDERS.forEach(function(p){known[String(p.id)]=true;});
 const chips=arr.length?arr.map(function(v,i){
  const id=String(v);
  const label=known[id]?(MODEL_LABEL[id]||id):'当前不可用，保留设置（'+id+'）';
  return '<span class=\'chip\'><span class=\'ord\'>'+(i+1)+'</span>'+esc(label)+'<button type=\'button\' data-act=\'prov-up\' data-path=\''+f.path+'\' data-index=\''+i+'\' aria-label=\'上移 '+esc(id)+'\'>↑</button><button type=\'button\' data-act=\'prov-down\' data-path=\''+f.path+'\' data-index=\''+i+'\' aria-label=\'下移 '+esc(id)+'\'>↓</button><button type=\'button\' data-act=\'list-remove\' data-path=\''+f.path+'\' data-index=\''+i+'\' aria-label=\'移除 '+esc(id)+'\'>×</button></span>';
 }).join(''):'<span class=\'empty-inline\'>未设置备用模型；主选模型异常或结果无效时不会按序重试。</span>';
 let add='';
 if(PROVIDERS.length){
  add='<select id=\'add-'+slug(f.path)+'\' data-role=\'provider-add\'>'+PROVIDERS.map(function(p){const id=String(p.id);return '<option value=\''+esc(id)+'\'>'+esc(MODEL_LABEL[id]||id)+'</option>';}).join('')+'</select><button class=\'btn mini\' type=\'button\' data-act=\'prov-add\' data-path=\''+f.path+'\'>添加备用模型</button>';
 }else{
  add='<select id=\'add-'+slug(f.path)+'\' data-role=\'provider-add\' disabled><option value=\'\'>（没有可用模型）</option></select><button class=\'btn mini\' type=\'button\' data-act=\'prov-add\' data-path=\''+f.path+'\' disabled>添加备用模型</button>';
 }
 return '<div class=\'chips\'>'+chips+'</div><div class=\'list-add\'><label class=\'sr-only\' for=\'add-'+slug(f.path)+'\'>添加备用模型</label>'+add+'</div><p class=\'field-note\'>按从上到下的顺序尝试，共用该模块总时限；列表值仍是数组。'+(MODEL_WARNING?' '+esc(MODEL_WARNING):'')+'</p>';
}
const LIST_NOTES={
 'sys001.ingress.private_allowed_sender_ids':'留空 = 不限制私聊范围；禁止名单仍生效，主人例外不变。',
 'sys001.ingress.blocked_sender_ids':'禁止优先于允许名单；不影响主人。',
 'sys001.ingress.group_allowed_scopes':'留空 = 不限制群聊范围；自然参与需单独配置。',
 'sys001.ingress.group_blocked_scopes':'对普通群友优先于允许群号；主人不受此项限制。',
 'sys001.group.natural_group_scopes':'留空 = 不参与任何群；主人也须在本项指定的群。',
 'sys001.capability_visibility.friendly_sender_ids':'留空 = 没有工具过滤豁免；豁免不会变成主人。',
 'sys001.capability_visibility.ordinary_capability_names':'留空 = 仅保留 astr_kb_search；填工具精确名称，不是插件名。'
};
function listEmpty(path){
 if(path==='sys001.ingress.private_allowed_sender_ids') return '留空 = 不限制私聊范围（禁止名单仍生效，主人例外不变）';
 if(path==='sys001.ingress.group_allowed_scopes') return '留空 = 不限制群聊范围';
 if(path==='sys001.group.natural_group_scopes') return '留空 = 不参与任何群';
 if(path==='sys001.capability_visibility.ordinary_capability_names') return '留空 = 仅保留 astr_kb_search';
 return '暂无条目';
}
function renderList(f,val){
 const arr=Array.isArray(val)?val:[];
 const chips=arr.length?arr.map(function(v,i){
  return '<span class=\'chip\'>'+esc(v)+'<button type=\'button\' data-act=\'list-remove\' data-path=\''+f.path+'\' data-index=\''+i+'\' aria-label=\'移除 '+esc(v)+'\'>×</button></span>';
 }).join(''):'<span class=\'empty-inline\'>'+esc(listEmpty(f.path))+'</span>';
 return '<div class=\'chips\'>'+chips+'</div><div class=\'list-add\'><label class=\'sr-only\' for=\'in-'+slug(f.path)+'\'>添加'+esc(f.d)+'</label><input id=\'in-'+slug(f.path)+'\' type=\'text\' data-role=\'chip-input\' data-path=\''+f.path+'\' placeholder=\'输入后按回车添加\'><button class=\'btn mini\' type=\'button\' data-act=\'list-add\' data-path=\''+f.path+'\'>添加</button><button class=\'btn mini\' type=\'button\' data-act=\'list-clear\' data-path=\''+f.path+'\'>清空</button></div><p class=\'field-note\'>'+esc(LIST_NOTES[f.path]||'每项一条；多项可用逗号或空格分隔后粘贴。')+'</p>';
}
function renderText(f,val){
 let out=`<div class='rule-row'><div class='preview ${val?'':'empty'}'>${val?esc(val):'（空）'}</div></div>`;
 if(f.e) out+='<p class=\'field-note\'>'+esc(f.e)+'</p>';
 if(f.path==='sys001.final_review.core_prompt'&&!val) out+='<p class=\'field-note\'><span class=\'pill\'>当前草稿为空：生效时回到默认基础规则</span></p>';
 return out;
}
function fieldKind(f){
 if(f.s==='provider') return 'field--provider';
 if(f.s==='providers') return 'field--providers';
 if(f.t==='bool') return 'field--bool';
 if(f.t==='list') return 'field--list';
 if(f.t==='text') return 'field--text';
 if(f.t==='int'||f.t==='float') return 'field--num';
 if(f.o) return 'field--select';
 return 'field--string';
}
function hintLead(h){
 const s=String(h||'').trim();
 if(!s) return '';
 const m=s.match(/^[^。！？!?；;]+[。！？!?；;]?/);
 let t=(m?m[0]:s).trim();
 if(t.length>110) t=t.slice(0,108).replace(/[，、,：: ]+$/,'')+'…';
 return t;
}
function renderField(f){
 const val=draft[f.path];
 const broken=brokenConds(f);
 const hidden=broken.length>0;
 const slugp=slug(f.path);
 let ctrl='';
 if(f.s==='provider') ctrl=renderProvider(f,val);
 else if(f.s==='providers') ctrl=renderProviderList(f,val);
 else if(f.t==='bool') ctrl=renderBool(f,val);
 else if(f.t==='list') ctrl=renderList(f,val);
 else if(f.t==='text') ctrl=renderText(f,val);
 else if(f.t==='int'||f.t==='float') ctrl=renderNumber(f,val);
 else if(f.o) ctrl=renderSelect(f,val);
 else ctrl=renderString(f,val);
 const isText=f.t==='text';
 const tool=isText?`<div class='field-title-tools'><button class='btn mini' type='button' data-act='rule-open' data-path='${f.path}'>查看 / 编辑</button></div>`:'';
 const lead=(f.h&&(f.t!=='text'||!f.e))?hintLead(f.h):'';
 const leadHTML=lead?`<p class='hint-lead'>${esc(lead)}</p>`:'';
 return `<article class='field ${fieldKind(f)}' id='field-${slugp}' data-path='${f.path}' tabindex='-1'${hidden?' hidden':''}><div class='field-head'><div class='field-title'><h4>${esc(f.d)}</h4></div>${tool}</div><div class='ctrl' data-ctrl ${hidden?'hidden':''}>${ctrl}</div>${leadHTML}<details class='hint' id='hint-${slugp}'><summary>详细说明</summary><p class='hint-path'>${esc(f.path)}</p><p>${esc(f.h)}</p></details></article>`;
}
function pageHTML(page){
 let head=`<header class='page-head'><div class='page-title'><h2>${esc(page.title)}</h2><p class='page-sub'>${esc(page.sub)}</p></div><div class='page-meta'><span class='pill'>${page.count} 项设置</span>`;
 head+='</div></header>';
 const warn=MODEL_WARNING?'<div class=\'banner warn\' role=\'status\'><div><strong>模型列表加载失败</strong><p>'+esc(MODEL_WARNING)+'</p></div></div>':'';
 const banner=page.id==='natural'?'<div id=\'naturalBanner\' class=\'banner\'></div>':'';
 const sections=page.sections.map(function(sec){
  const cards=sec.fields.filter(function(f){return !EMBEDDED.has(f.path);}).map(renderField).join('');
  if(!cards) return '';
  if(sec.advanced){
   return '<details class=\'adv\'><summary><span class=\'chev\' aria-hidden=\'true\'></span>'+esc(sec.title)+'<span class=\'pill\'>'+sec.fields.length+' 项</span></summary><div class=\'adv-body\'>'+(sec.desc?'<p class=\'field-note adv-desc\'>'+esc(sec.desc)+'</p>':'')+cards+'</div></details>';
  }
  return '<section class=\'section-card\'><div class=\'section-head\'><h3>'+esc(sec.title)+'</h3>'+(sec.desc?'<p>'+esc(sec.desc)+'</p>':'')+'</div><div class=\'fields\'>'+cards+'</div></section>';
 }).join('');
 return head+warn+banner+sections;
}
function updateNavs(){
 document.querySelectorAll('[data-act=nav]').forEach(function(b){
  if(b.dataset.page===currentPage) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current');
 });
}
function buildNavs(){
 navA.innerHTML=PAGES.map(function(p,i){return '<button class=\'navitem\' type=\'button\' data-act=\'nav\' data-page=\''+p.id+'\'><span class=\'num\'>'+(i+1)+'</span><span>'+esc(p.name)+'</span><span class=\'count\'>'+p.count+'</span></button>';}).join('');
 navB.innerHTML=PAGES.map(function(p){return '<button class=\'tab\' type=\'button\' data-act=\'nav\' data-page=\''+p.id+'\'>'+esc(p.name)+'<span class=\'tabcount\'>'+p.count+'</span></button>';}).join('');
 updateNavs();
}
function updateNaturalBanner(){
 const el=document.getElementById('naturalBanner');
 if(!el) return;
 const on=!!draft['sys001.group.natural_participation_enabled'];
 const groups=Array.isArray(draft['sys001.group.natural_group_scopes'])?draft['sys001.group.natural_group_scopes']:[];
 const prob=draft['sys001.group.natural_participation_probability'];
 const cooldown=draft['sys001.group.natural_reply_cooldown_seconds'];
 el.className='banner '+(on?'':'warn');
 if(on){
  const range=groups.length?'当前群号：'+groups.map(function(v){return esc(v);}).join('、'):'当前未配置群号，因此不会自然参与';
  el.innerHTML='<div><strong>自然参与已开启</strong><p>'+range+'。仅在未被点名时判断；概率 '+esc(prob)+'%，自然回复冷却 '+esc(cooldown)+' 秒，并受统计上限限制。</p></div><div class=\'banner-actions\'><button class=\'btn\' type=\'button\' data-act=\'goto\' data-path=\'sys001.group.natural_group_scopes\'>检查群号</button></div>';
 }else{
  el.innerHTML='<div><strong>自然参与当前关闭</strong><p>关闭时 Shio 不会在未点名时主动接话；@、引用机器人与官方唤醒不受影响。</p></div><div class=\'banner-actions\'><button class=\'btn primary\' type=\'button\' data-act=\'goto\' data-path=\'sys001.group.natural_participation_enabled\'>前往开启</button></div>';
 }
}
function refreshConditions(){
 document.querySelectorAll('article.field[data-path]').forEach(function(card){
  const f=byPath[card.dataset.path];
  if(!f) return;
  const broken=brokenConds(f);
  const ctrl=card.querySelector('[data-ctrl]');
  if(ctrl) ctrl.hidden=broken.length>0;
  card.hidden=broken.length>0;
 });
 document.querySelectorAll('.section-card').forEach(function(card){
  const arts=card.querySelectorAll('article.field');
  if(!arts.length) return;
  let allHidden=true;
  for(let i=0;i<arts.length;i++){ if(arts[i].hidden!==true){ allHidden=false; break; } }
  card.hidden=allHidden;
 });
 document.querySelectorAll('details.adv').forEach(function(d){
  const arts=d.querySelectorAll('article.field');
  if(!arts.length) return;
  let allHidden=true;
  for(let i=0;i<arts.length;i++){ if(arts[i].hidden!==true){ allHidden=false; break; } }
  d.hidden=allHidden;
 });
 updateNaturalBanner();
}
function updateDirty(){
 const isDirty=!sameValue(draft,saved);
 dirtyBadge.classList.toggle('clean',!isDirty);
 dirtyText.textContent=isDirty?'有未保存修改':'与已保存配置一致';
 saveBtn.disabled=!isDirty||saving;
 discardBtn.disabled=!isDirty||saving;
 return isDirty;
}
function renderPage(opts){
 const page=PAGES.find(function(p){return p.id===currentPage;})||PAGES[0];
 pageRoot.innerHTML=pageHTML(page);
 refreshConditions();
 refreshNumberMsgs();
 updateNavs();
 updateDirty();
 if(!opts||!opts.keepScroll) window.scrollTo({top:0,behavior:'auto'});
}
function numberIssue(path){
 const f=byPath[path];
 if(!f) return null;
 const v=draft[path];
 if(v===''||v===null||v===undefined) return {msg:'请填写数字。',soft:false};
 const n=Number(v);
 if(!Number.isFinite(n)) return {msg:'请填写有效数字。',soft:false};
 if(f.t==='int'&&!Number.isInteger(n)) return {msg:'按 schema 应为整数。',soft:false};
 if(f.r){
  const min=f.r[0],max=f.r[1];
  if(min!==null&&n<min){
   if(min===0) return {msg:'不能小于 0。',soft:false};
   if(max===null) return {msg:'应大于 0。',soft:false};
   return {msg:'应在 '+min+'–'+max+' 之间（schema 原语义）。',soft:false};
  }
  if(max!==null&&n>max) return {msg:'应不大于 '+max+'（schema 范围 '+min+'–'+max+'）。',soft:false};
 }
 const pMin='sys001.presentation.text_component_min_segments';
 const pMax='sys001.presentation.text_component_max_segments';
 if(path===pMin||path===pMax){
  const a=Number(draft[pMin]),b=Number(draft[pMax]);
  if(Number.isFinite(a)&&Number.isFinite(b)&&a>b) return {msg:'期望最少气泡数大于最多气泡数；按 schema 说明，上限也会随之升高。',soft:true};
 }
 const wMin='sys001.presentation.bubble_send_min_wait_seconds';
 const wMax='sys001.presentation.bubble_send_max_wait_seconds';
 if(path===wMin||path===wMax){
  const a=Number(draft[wMin]),b=Number(draft[wMax]);
  if(Number.isFinite(a)&&Number.isFinite(b)&&b<a) return {msg:'最长间隔不得小于最短间隔；非法区间会保留单条回复。',soft:false};
 }
 return null;
}
function refreshNumberMsgs(){
 document.querySelectorAll('[data-valid-path]').forEach(function(el){
  const issue=numberIssue(el.dataset.validPath);
  el.textContent=issue?issue.msg:'';
  el.classList.toggle('show',!!issue);
  el.classList.toggle('soft',!!(issue&&issue.soft));
 });
}
function showToast(main,sub){
 toastMain.textContent=main;
 toastSub.textContent=sub||'';
 toast.classList.add('show');
 clearTimeout(toastTimer);
 toastTimer=setTimeout(function(){toast.classList.remove('show');},4200);
}
function hideResults(){results.hidden=true;searchInput.setAttribute('aria-expanded','false');}
function runSearch(q){
 if(!q){hideResults();return;}
 const ql=q.toLowerCase();
 const pageOrder={};
 PAGES.forEach(function(p,i){pageOrder[p.id]=i;});
 const hits=[];
 PAGES.forEach(function(p){
  if((p.name+' '+p.title+' '+p.sub).toLowerCase().indexOf(ql)>=0) hits.push({type:'page',page:p.id,title:p.name,sub:p.sub,score:2});
 });
 flat.forEach(function(f){
  if(f.d.toLowerCase().indexOf(ql)>=0) hits.push({type:'field',page:f.page,path:f.path,title:f.d,hint:f.h,score:3});
  else if(f.h.toLowerCase().indexOf(ql)>=0) hits.push({type:'field',page:f.page,path:f.path,title:f.d,hint:f.h,score:1});
  else if(f.path.toLowerCase().indexOf(ql)>=0) hits.push({type:'field',page:f.page,path:f.path,title:f.d,hint:f.h,score:1});
 });
 hits.sort(function(a,b){
  if(a.score!==b.score) return b.score-a.score;
  return pageOrder[a.page]-pageOrder[b.page];
 });
 if(!hits.length){
  results.innerHTML='<div class=\'noresult\'>没有匹配的设置。试试“审核”“气泡”“群号”“模型”。</div>';
 }else{
  results.innerHTML='<div class=\'rhead\'><span>'+hits.length+' 条结果</span><span>覆盖 7 页</span></div>'+hits.slice(0,60).map(function(h){
   const pn=(PAGES.find(function(p){return p.id===h.page;})||{}).name||'';
   if(h.type==='page'){
    return '<button type=\'button\' class=\'result\' data-act=\'search-page\' data-page=\''+h.page+'\'><span class=\'r-top\'><span class=\'pill\'>页面</span>'+esc(h.title)+'</span><span class=\'snip\'>'+esc(truncate(h.sub,120))+'</span></button>';
   }
   return '<button type=\'button\' class=\'result\' data-act=\'search-field\' data-path=\''+h.path+'\'><span class=\'r-top\'><span class=\'pill\'>'+esc(pn)+'</span>'+esc(h.title)+'</span><span class=\'snip\'>'+esc(truncate(h.hint,130))+'</span></button>';
  }).join('');
 }
 results.hidden=false;
 searchInput.setAttribute('aria-expanded','true');
}
function gotoField(path){
 const f=byPath[path];
 if(!f){showToast('未找到该设置');return;}
 const broken=brokenConds(f);
 if(broken.length){showToast('需先开启/切换：'+broken.map(function(b){return b.label;}).join('、'));}
 const nextPath=broken.length?broken[0].path:path;
 const nextField=byPath[nextPath]||f;
 if(currentPage!==nextField.page){currentPage=nextField.page;renderPage({keepScroll:true});}
 window.requestAnimationFrame(function(){
  const el=document.getElementById('field-'+slug(nextPath));
  if(!el) return;
  let d=el.closest('details');
  while(d){d.open=true;d=d.parentElement?d.parentElement.closest('details'):null;}
  el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
  try{el.focus({preventScroll:true});}catch(e){}
 });
}
function afterFieldChange(path){
 const el=document.getElementById('field-'+slug(path));
 if(el&&byPath[path]) el.outerHTML=renderField(byPath[path]);
 refreshConditions();
 refreshNumberMsgs();
 updateDirty();
}
function addChip(path,raw){
 const parts=String(raw).split(/[\s,，、;；]+/).map(function(s){return s.trim();}).filter(Boolean);
 if(!parts.length) return;
 const arr=Array.isArray(draft[path])?draft[path].slice():[];
 parts.forEach(function(p){if(arr.indexOf(p)<0) arr.push(p);});
 draft[path]=arr;
 afterFieldChange(path);
 const inp=document.getElementById('in-'+slug(path));
 if(inp) inp.focus();
}
function removeAt(path,index){
 const arr=Array.isArray(draft[path])?draft[path].slice():[];
 if(index>=0&&index<arr.length) arr.splice(index,1);
 draft[path]=arr;
 afterFieldChange(path);
}
function addProvider(path,val){
 if(!val) return;
 const arr=Array.isArray(draft[path])?draft[path].slice():[];
 if(!PROVIDERS.some(function(p){return String(p.id)===val;})){showToast('模型列表中找不到该模型');return;}
 if(arr.indexOf(val)>=0){showToast('该模型已在备用列表中');return;}
 arr.push(val);
 draft[path]=arr;
 afterFieldChange(path);
}
function moveProvider(path,index,dir){
 const arr=Array.isArray(draft[path])?draft[path].slice():[];
 const j=index+dir;
 if(index<0||index>=arr.length||j<0||j>=arr.length) return;
 const tmp=arr[index];arr[index]=arr[j];arr[j]=tmp;
 draft[path]=arr;
 afterFieldChange(path);
}
function openRule(path){
 const f=byPath[path];
 if(!f) return;
 activeRulePath=path;
 ruleTitle.textContent=f.d;
 rulePath.textContent=f.path;
 ruleHint.textContent=f.h;
 ruleText.value=draft[path]||'';
 ruleEmpty.textContent=f.e||'留空时该项保持为空。';
 ruleDefault.textContent=f.v||'（schema 默认为空）';
 ruleDialog.showModal();
 setTimeout(function(){ruleText.focus();},30);
}
document.addEventListener('click',function(e){
 const t=e.target.closest('[data-act]');
 if(!t) return;
 const act=t.dataset.act;
 if(saving) return;
 if(act==='nav'){currentPage=t.dataset.page;closeDrawer();hideResults();renderPage();}
 else if(act==='goto'){hideResults();gotoField(t.dataset.path);}
 else if(act==='rule-open'){openRule(t.dataset.path);}
 else if(act==='list-add'){const card=t.closest('.field');const input=card?card.querySelector('[data-role=chip-input]'):null;addChip(t.dataset.path,input?input.value:'');}
 else if(act==='list-clear'){draft[t.dataset.path]=[];afterFieldChange(t.dataset.path);}
 else if(act==='list-remove'){removeAt(t.dataset.path,Number(t.dataset.index));}
 else if(act==='prov-add'){const row=t.closest('.list-add');const sel=row?row.querySelector('select'):null;addProvider(t.dataset.path,sel?sel.value:'');}
 else if(act==='prov-up'){moveProvider(t.dataset.path,Number(t.dataset.index),-1);}
 else if(act==='prov-down'){moveProvider(t.dataset.path,Number(t.dataset.index),1);}
 else if(act==='search-field'){hideResults();gotoField(t.dataset.path);}
 else if(act==='search-page'){hideResults();currentPage=t.dataset.page;renderPage();}
 else if(act==='drawer-open'){openDrawer();}
 else if(act==='drawer-close'){closeDrawer();}
});
pageRoot.addEventListener('input',function(e){
 if(saving) return;
 const num=e.target.closest('[data-kind=num]');
 if(num){
  const p=num.dataset.path;
  const n=Number(num.value);
  draft[p]=num.value===''?'':(Number.isFinite(n)?n:num.value);
  refreshNumberMsgs();
  updateDirty();
  return;
 }
 const txt=e.target.closest('[data-kind=text]');
 if(txt){draft[txt.dataset.path]=txt.value;updateDirty();}
});
pageRoot.addEventListener('change',function(e){
 if(saving) return;
 const el=e.target.closest('[data-kind]');
 if(!el) return;
 const p=el.dataset.path;
 if(el.dataset.kind==='bool'){
  draft[p]=el.checked;
  const st=el.closest('.switch')?el.closest('.switch').querySelector('.state'):null;
  if(st) st.textContent=el.checked?'已开启':'已关闭';
  refreshConditions();
  updateDirty();
 }else if(el.dataset.kind==='select'){
  draft[p]=el.value;
  refreshConditions();
  updateDirty();
 }
});
pageRoot.addEventListener('keydown',function(e){
 if(saving) return;
 const inp=e.target.closest('[data-role=chip-input]');
 if(inp&&e.key==='Enter'){e.preventDefault();addChip(inp.dataset.path,inp.value);}
});
searchInput.addEventListener('input',function(){runSearch(this.value.trim());});
document.addEventListener('click',function(e){if(!e.target.closest('.searchbar')) hideResults();});
document.addEventListener('keydown',function(e){
 if(e.key==='Escape'){
  if(document.body.classList.contains('drawer-open')) closeDrawer();
  hideResults();
 }
});
overlay.addEventListener('click',closeDrawer);
saveBtn.addEventListener('click',async function(){
 if(saving) return;
 const changes={};
 flat.forEach(function(f){
  if(!sameValue(draft[f.path],saved[f.path])) changes[f.path]=clone(draft[f.path]);
 });
 const paths=Object.keys(changes);
 if(!paths.length){showToast('没有需要保存的修改');return;}
 for(let i=0;i<paths.length;i++){
  const p=paths[i];
  const f=byPath[p];
  if(f&&(f.t==='int'||f.t==='float')){
   const issue=numberIssue(p);
   if(issue&&!issue.soft){showToast('无法保存：'+issue.msg,'请修正后重试');gotoField(p);return;}
  }
 }
 saving=true;
 saveBtn.disabled=true;
 discardBtn.disabled=true;
 pageRoot.inert=true;
 try{
  const result=await bridge.apiPost('settings',{revision:revision,changes:changes});
  if(!result||!result.config||typeof result.revision!=='string') throw new Error('保存响应不完整');
  for(let i=0;i<paths.length;i++){
   const p=paths[i];
   const r=readPath(result.config,p);
   if(!r.found||!sameValue(r.value,changes[p])) throw new Error('保存响应与提交内容不一致');
  }
  revision=result.revision;
  const next=initialFrom(result.config);
  saved=clone(next);
  draft=clone(next);
  renderPage({keepScroll:true});
  showToast('已保存，后续处理使用新配置','无需重载插件；已在运行的计时和任务不会因保存而重新安排。');
 }catch(err){
  showToast('保存失败',(err&&err.message?err.message:'未知错误')+'；已保留当前草稿。如遇配置冲突，请刷新页面后重试。');
 }finally{
  saving=false;
  pageRoot.inert=false;
  updateDirty();
 }
});
discardBtn.addEventListener('click',function(){
 if(saving) return;
 draft=clone(saved);
 renderPage({keepScroll:true});
 showToast('已放弃修改，恢复上次保存的配置');
});
window.addEventListener('beforeunload',function(e){
 if(!sameValue(draft,saved)){e.preventDefault();e.returnValue='';}
});
themeBtn.addEventListener('click', function () {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.documentElement.setAttribute('data-theme', dark ? 'light' : 'dark');
  themeBtn.setAttribute('aria-pressed', String(!dark));
  themeBtn.textContent = dark ? '深色模式' : '浅色模式';
});

function openDrawer(){
 document.body.classList.add('drawer-open');
 drawerBtn.setAttribute('aria-expanded','true');
 const first=navA.querySelector('button');
 if(first) first.focus();
}
function closeDrawer(){
 document.body.classList.remove('drawer-open');
 drawerBtn.setAttribute('aria-expanded','false');
}
ruleRestore.addEventListener('click',function(){
 if(saving||!activeRulePath) return;
 const f=byPath[activeRulePath];
 ruleText.value=f&&f.v?f.v:'';
 ruleText.focus();
});
ruleCancel.addEventListener('click',function(){activeRulePath=null;ruleDialog.close();});
ruleDialog.addEventListener('cancel',function(){activeRulePath=null;});
ruleApply.addEventListener('click',function(){
 if(saving||!activeRulePath) return;
 const p=activeRulePath;
 activeRulePath=null;
 draft[p]=ruleText.value;
 ruleDialog.close();
 renderPage({keepScroll:true});
 showToast('已应用到草稿（未保存）');
});
 buildNavs();
 renderPage();
}
function renderLoadError(message){
 const root=document.getElementById('pageRoot');
 const save=document.getElementById('saveBtn');
 const discard=document.getElementById('discardBtn');
 if(save) save.disabled=true;
 if(discard) discard.disabled=true;
 if(!root) return;
 root.innerHTML='<div id=\'loadError\' class=\'banner warn\' role=\'alert\'><div><strong>设置加载失败</strong><p>'+escText(message)+'</p></div><div class=\'banner-actions\'><button class=\'btn\' type=\'button\' id=\'retryLoad\'>重试</button></div></div>';
 const retry=document.getElementById('retryLoad');
 if(retry) retry.addEventListener('click',function(){loadSettings();});
}
let loading = false;

async function loadSettings(){
 if (loading) return;
 if(!bridge){renderLoadError('请在 AstrBot 插件页面中打开设置');return;}
 loading = true;
 const retryBtn = document.getElementById('retryLoad');
 if (retryBtn) retryBtn.disabled = true;
 MODEL_WARNING='';
 try{
  await bridge.ready();
  const settings=await bridge.apiGet('settings');
  if(!settings||!settings.schema||!settings.schema.sys001||!settings.config||typeof settings.config!=='object'||typeof settings.revision!=='string'){
   throw new Error('设置数据不完整，无法加载。');
  }
  let providers=[];
  try{
   const models=await bridge.apiGet('models');
   providers=Array.isArray(models&&models.providers)?models.providers:[];
  }catch(err){
   providers=[];
   MODEL_WARNING='模型列表加载失败：仅保留已保存的模型 ID，已保存设置不受影响。';
  }
  await mount(settings,providers);
 }catch(err){
  renderLoadError(err&&err.message?err.message:'无法读取设置。');
 }finally{
  loading = false;
 }
}
loadSettings();

