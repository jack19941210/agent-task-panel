// Run: PLAYWRIGHT_MODULE=/path/to/playwright node tests/panel-ui.cjs
// Uses a temporary browser profile and mocked APIs only; no real agent data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const screenshot = filename=>path.join(process.env.PANEL_SCREENSHOT_DIR||os.tmpdir(),filename);
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
 try {
 const page = await browser.newPage({viewport:{width:400,height:640},timezoneId:'Asia/Shanghai'});
 await page.clock.install({time:new Date('2026-11-23T12:00:00+08:00')});
 const errors=[]; page.on('pageerror', e=>errors.push(e.message));
 const timelineDates=[];
 let fail=false, saveFail=false, diagnosticsFail=false, diagnosticsCalls=0, saved, cfg={agents:[{id:'codex',name:'Codex'},{id:'cursor',name:'Cursor'},{id:'workbuddy',name:'WorkBuddy'}],hidden:[],notifications:{completed:true,summary:true,focus:true,heartbeat:false}};
 const now=Date.now()/1000;
 const replyText='已修复登录。\n\n'+('逐项验证：单元测试已通过。\n'.repeat(60))+'\n<img src=x onerror="window.replyInjected=true">\n最后一段：请选择 A/B/C。';
 const handoff={changes:'更新登录校验',verification:'pytest：12 通过，1 失败',next_step:'修复超时测试'};
 const noProjectId='11111111-1111-4111-8111-111111111111';
 const noProjectLink=`cursor://anysphere.cursor-deeplink/agent?id=${noProjectId}`;
 const tasks=[
  {task_id:'a',agent:'cursor',title:'等待确认部署',cwd:'/work/shop',status:'idle',display_status:'waiting',mtime:now,link:'agentpanel://cursor?cwd=%2Fwork%2Fshop&id=a',recent:'请确认操作',status_reason:'Cursor 已生成计划，正在等待你点击 Build。'},
  {task_id:'b',agent:'codex',title:'发布失败',cwd:'/work/api',status:'failed',mtime:now,link:'codex://b',recent:'测试失败'},
  {task_id:'c',agent:'codex',title:'登录修复',cwd:'/work/shop',status:'done',mtime:now,link:'codex://c',recent:'更新登录校验',reply_text:replyText,handoff},
  {task_id:'d',agent:'workbuddy',title:'分析性能',cwd:'/work/api',status:'running',mtime:now,link:'',recent:'正在分析'},
  {task_id:'e',agent:'codex',title:'过期任务',cwd:'/work/api',status:'idle',display_status:'unknown',mtime:now-1800,link:'',recent:'',status_reason:'执行信号已过期，无法确认是否仍在执行。'},
  {task_id:'f',agent:'muse',title:'Muse 在线',status:'idle',display_status:'online',mtime:now,link:'',recent:'应用在线 · 无本地任务明细'},
 ];
 let visibleTasks=tasks;
 await page.addInitScript(()=>{window.agentPanel={platform:'macos',openExternal:async(url)=>{window.lastOpened=url;return true;},getSettings:async()=>({autostart:false,autoYield:false,cursorJumpMode:'ide_only'}),setSettings:async()=>true};});
 await page.route('http://panel.test/**', async route=>{
  const url=new URL(route.request().url());
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../resources/panel.html'),'utf8')});
  if(url.pathname==='/api/tasks'){if(fail)return route.abort();return route.fulfill({json:{tasks:visibleTasks,pin:false,finished:[]}});}
  if(url.pathname==='/api/config'){
   if(route.request().method()==='POST'){if(saveFail)return route.fulfill({status:500,json:{error:'无法保存设置'}});saved=route.request().postDataJSON();cfg={...cfg,...saved};return route.fulfill({json:{ok:true}});}
   return route.fulfill({json:cfg});
  }
  if(url.pathname==='/api/diagnostics'){
   diagnosticsCalls++;
   if(diagnosticsFail)return route.fulfill({status:500,json:{error:'检测失败'}});
   return route.fulfill({json:{channels:[{id:'codex',name:'Codex',state:'ok',supported:true},{id:'cursor',name:'Cursor',state:'error',supported:true,error:'PermissionError'},{id:'claude',name:'Claude Code',state:'empty',supported:true},{id:'cue',name:'Cue',state:'empty',supported:false},{id:'workbuddy',name:'WorkBuddy',state:cfg.workbuddy_data_dir?'incompatible':'not_found',supported:true}]}});
  }
  if(url.pathname==='/api/timeline'){
   const day=url.searchParams.get('date');timelineDates.push(day);
   const ts=Date.parse(day+'T12:00:00+08:00')/1000;
   return route.fulfill({json:{items:[{task_id:'c',agent:'codex',title:'历史结果',cwd:'/work/shop',ts,kind:'completed',recent:'结果',reply_text:replyText,handoff},{task_id:'old',agent:'cursor',title:'旧的无工程会话',cwd:'',ts:ts-1,kind:'completed',link:`agentpanel://cursor?id=${noProjectId}`}]}});
  }
  return route.fulfill({json:{ok:true}});
 });
 await page.goto('http://panel.test/');
 await page.waitForFunction(()=>document.querySelector('#stAll').textContent==='5');
 assert.equal(await page.locator('#stAttention').textContent(),'2');
 assert.equal(await page.locator('#stRun').textContent(),'1');
 assert.equal(await page.locator('.status-waiting .task-note').isVisible(),true);
 assert.match(await page.locator('.status-waiting .task-note').innerText(),/Build/);
 assert.match(await page.locator('.status-unknown .task-note').innerText(),/执行信号已过期/);
 assert.deepEqual((await page.locator('.group-title').allTextContents()).slice(0,4),['等待确认','失败','执行中','回合结束']);
 assert.equal(await page.locator('.group-toggle[data-state=done]').getAttribute('aria-expanded'),'false');
 assert.equal(await page.locator('.group-toggle[data-state=done] .group-count').textContent(),'1','failed must not count as ended');
 assert.equal(await page.locator('.status-online, #markAll, .seen-task').count(),0);
 assert.equal(await page.locator('#onlineNames').textContent(),'Muse');
 assert.equal(await page.locator('.status-running .t-recent').isVisible(),false);
 await page.screenshot({path:screenshot('agent-panel-updated.png')});
 await page.emulateMedia({colorScheme:'dark'});
 assert.equal(await page.locator('.status-waiting').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(57, 45, 32)');
 await page.screenshot({path:screenshot('agent-panel-dark.png')});await page.emulateMedia({colorScheme:'light'});
 await page.locator('[data-filter="attention"]').click();assert.equal(await page.locator('.task').count(),2);
 await page.locator('.status-failed .resume-btn').click();assert.equal(await page.evaluate(()=>window.lastOpened),'codex://b');
 await page.locator('#clearFilters').click();
 await page.locator('[data-filter="running"]').click();assert.equal(await page.locator('.task').count(),1);
 await page.locator('.status-running .detail-btn').click();
 assert.equal(await page.locator('.status-running .summary').innerText(),'正在分析');
 await page.locator('.status-running .detail-btn').click();
 await page.locator('#clearFilters').click();
 await page.locator('[data-view="projects"]').click();
 assert.equal(await page.locator('.project-closed .group-toggle').getAttribute('aria-expanded'),'false');
 await page.locator('[data-view="status"]').click();
 await page.locator('#search').fill('等待确认部署');
 await page.locator('.detail-btn').click();
 await page.locator('.open-ide').click();
 assert.equal(await page.evaluate(()=>new URL(window.lastOpened).searchParams.get('mode')),'ide_only');
 assert.equal(await page.evaluate(()=>new URL(window.lastOpened).searchParams.get('cwd')),'/work/shop');
 await page.locator('.open-task').click();
 assert.equal(await page.evaluate(()=>new URL(window.lastOpened).searchParams.get('mode')),'agents_only');
 await page.locator('.task-main').click();
 assert.equal(await page.evaluate(()=>new URL(window.lastOpened).searchParams.has('mode')),false,'normal click uses native preference');
 visibleTasks=[...tasks,{task_id:'no-project',agent:'cursor',title:'无工程会话',cwd:'',status:'running',mtime:now,link:noProjectLink}];
 await page.locator('#search').fill('无工程会话');
 await page.evaluate(()=>tick());
 await page.locator('.detail-btn').click();
 assert.equal(await page.locator('.open-ide').isVisible(),false);
 await page.getByRole('button',{name:'在 Cursor Agents 打开此会话'}).click();
 assert.equal(await page.evaluate(()=>window.lastOpened),noProjectLink);
 await page.evaluate(()=>{window.lastOpened='';document.querySelector('#toast').hidden=true;});
 await page.locator('.task-main').click();
 assert.equal(await page.evaluate(()=>window.lastOpened),noProjectLink);
 assert.equal(await page.locator('#toast').isVisible(),false,'native link does not show the bridge preparation notice');
 visibleTasks=tasks;
 await page.evaluate(()=>tick());
 await page.locator('#clearFilters').click();
 await page.locator('.group-toggle[data-state=done]').click();assert.equal(await page.locator('.status-done').isVisible(),true);
 await page.locator('.group-toggle[data-state=done]').click();
 await page.locator('#search').fill('登录');assert.equal(await page.locator('.task').count(),1);
 assert.equal(await page.locator('#stAll').textContent(),'1');
 assert.deepEqual(await page.locator('.handoff-preview dd').allTextContents(),Object.values(handoff));
 assert.equal(await page.locator('.handoff-preview').isVisible(),true);
 await page.locator('.detail-btn').click();assert.equal(await page.locator('.task-detail').isVisible(),true);
 assert.equal(await page.locator('.handoff-preview').isVisible(),false);
 assert.deepEqual(await page.locator('.task-detail .handoff dt').allTextContents(),['改动','验证','下一步']);
 await page.getByRole('button',{name:'查看完整回复',exact:true}).click();
 assert.equal(await page.locator('#replyBody').textContent(),replyText);
 assert.equal(await page.locator('#replyBody img').count(),0,'assistant text is never interpreted as markup');
 assert.equal(await page.locator('#replyBody').evaluate(el=>el.scrollHeight>el.clientHeight),true);
 await page.getByRole('button',{name:'打开原会话',exact:true}).click();
 assert.equal(await page.evaluate(()=>window.lastOpened),'codex://c');
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('#reply').isVisible(),false);
 assert.equal(await page.locator('.read-reply:focus').count(),1);
 await page.evaluate(()=>{window.cardBefore=document.querySelector('.task');return tick();});
 assert.equal(await page.evaluate(()=>window.cardBefore===document.querySelector('.task')),true,'polling must preserve card nodes');
 await page.locator('.task-main').click();
 assert.equal(await page.locator('.group-title').textContent(),'回合结束');
 assert.equal(await page.evaluate(()=>localStorage.getItem('panel.seen.v1')),null);
 await page.locator('#clearFilters').click();
 fail=true;await page.evaluate(async()=>{for(let i=0;i<4;i++)await tick();});
 assert.match(await page.locator('#connection').textContent(),/连接中断 · 数据可能过期/);assert.equal(await page.locator('#retry').isVisible(),true);
 assert.equal(await page.locator('.status-waiting').isVisible(),true,'last tasks survive a disconnect');
 fail=false;await page.locator('#retry').click();await page.waitForFunction(()=>document.querySelector('#connection').textContent.startsWith('已更新'));
 visibleTasks=[tasks[3],tasks[5]];await page.evaluate(()=>tick());
 await page.waitForFunction(()=>document.querySelector('.group-title')?.textContent==='执行中');
 assert.equal(await page.locator('.group-title').count(),1,'no empty attention group');
 visibleTasks=[tasks[5]];diagnosticsFail=true;await page.evaluate(()=>tick());
 await page.waitForFunction(()=>document.querySelector('.check-message')?.textContent.includes('检测失败'));
 assert.equal(diagnosticsCalls,1);
 diagnosticsFail=false;await page.locator('.onboarding-retry').click();await page.waitForFunction(()=>document.querySelectorAll('.channel-checks li').length===4);
 assert.match(await page.locator('.channel-checks').innerText(),/Cursor\s*读取失败/);
 assert.match(await page.locator('.channel-checks').innerText(),/Codex\s*已检测到应用数据/);
 assert.match(await page.locator('.channel-checks').innerText(),/Claude Code\s*等待首个任务/);
 assert.match(await page.locator('.channel-checks').innerText(),/WorkBuddy\s*未找到数据库/);
 assert.equal(await page.locator('#stAll').textContent(),'0');
 await page.evaluate(()=>document.querySelector('#toast').hidden=true);
 await page.mouse.move(0,0);
 await page.screenshot({path:screenshot('agent-panel-onboarding.png')});
 await page.evaluate(()=>tick());assert.equal(diagnosticsCalls,2,'polling does not repeat diagnostics');
 visibleTasks=tasks;await page.evaluate(()=>tick());await page.waitForFunction(()=>!document.querySelector('.onboarding'));
 await page.locator('#search').fill('无关任务');assert.equal(await page.locator('.onboarding').count(),0);assert.match(await page.locator('#list').innerText(),/没有符合当前筛选的任务/);
 await page.locator('#clearFilters').click();
 await page.locator('#settingsBtn').click();await page.waitForFunction(()=>!document.querySelector('#saveSettings').disabled);
 assert.equal(await page.locator('[data-notification="completed"]').isChecked(),true);
 assert.equal(await page.locator('[data-notification="focus"]').isChecked(),true);
 assert.equal(await page.locator('[data-notification="summary"]').isChecked(),true);
 assert.equal(await page.locator('#workbuddyDir').inputValue(),'');
 await page.locator('#workbuddyDir').fill('/Users/example/WorkBuddy Data');
 await page.locator('[data-agent="cursor"]').uncheck();
 saveFail=true;await page.locator('#saveSettings').click();await page.waitForFunction(()=>!document.querySelector('#settingsError').hidden);assert.equal(await page.locator('#settings').isVisible(),true);
 saveFail=false;await page.locator('#saveSettings').click();await page.waitForFunction(()=>document.querySelector('#saveStatus').textContent==='已保存');assert.deepEqual(saved.hidden,['cursor']);
 assert.equal(saved.workbuddy_data_dir,'/Users/example/WorkBuddy Data');
 await page.waitForFunction(()=>document.querySelector('#diagnostics').textContent.includes('WorkBuddy：数据库格式不兼容'));
 assert.deepEqual(saved.notifications,{completed:true,blocked:true,failed:true,heartbeat:false,summary:true,focus:true});
 await page.screenshot({path:screenshot('agent-panel-settings.png')});
 await page.keyboard.press('Escape');assert.equal(await page.locator('#settings').isVisible(),false);
 await page.locator('[data-view="timeline"]').click();await page.waitForFunction(()=>document.querySelector('.event-open')?.textContent==='历史结果');
 assert.deepEqual(await page.locator('.tl-item .handoff dd').allTextContents(),Object.values(handoff));
 assert.equal(timelineDates.at(-1),'2026-11-23','timeline opens on the local day');
 await page.clock.setSystemTime(new Date('2026-11-24T00:00:01+08:00'));
 await page.evaluate(()=>tick());
 await page.waitForFunction(()=>!busy&&!queued);
 assert.equal(timelineDates.at(-1),'2026-11-24','open timeline follows local midnight');
 assert.equal(await page.locator('#historyDate').inputValue(),'2026-11-24');
 await page.getByRole('button',{name:'查看完整回复',exact:true}).click();
 assert.equal(await page.locator('#replyBody').textContent(),replyText);
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('.tl-item .read-reply').count(),1,'old timeline entries do not pretend to contain full replies');
 await page.locator('#historyDate').fill('2026-11-22');
 await page.evaluate(()=>tick());
 await page.waitForFunction(()=>!busy&&!queued);
 assert.equal(timelineDates.at(-1),'2026-11-22','polling preserves an explicitly selected day');
 await page.locator('[data-view="status"]').click();
 await page.locator('[data-view="timeline"]').click();
 await page.waitForFunction(()=>!busy&&!queued);
 assert.equal(timelineDates.at(-1),'2026-11-24','reopening the timeline returns to today');
 assert.equal(await page.locator('#historyDate').inputValue(),'2026-11-24');
 await page.evaluate(()=>{window.lastOpened='';document.querySelector('#toast').hidden=true;});
 await page.getByRole('button',{name:'旧的无工程会话'}).click();
 assert.equal(await page.evaluate(()=>window.lastOpened),noProjectLink);
 assert.equal(await page.locator('#toast').isVisible(),false);
 await page.evaluate(()=>{
  document.querySelector('#toast').hidden=true;
  timeline=[
   {task_id:'x',agent:'cursor',title:'优化任务面板的交互与视觉层级',cwd:'/work/agent-task-panel',ts:Date.now()/1000,kind:'completed',recent:'统一控件样式，精简顶部工具区。浅色与深色布局已通过验证。'},
   {task_id:'y',agent:'codex',title:'修复登录状态与会话恢复',cwd:'/work/customer-portal',ts:Date.now()/1000-600,kind:'completed',recent:'**登录流程已修复**，补充了会话过期与恢复测试，全部检查通过。'},
   {task_id:'z',agent:'cursor',title:'等待确认发布配置',cwd:'/work/api-service',ts:Date.now()/1000-1200,kind:'blocked',recent:'请确认生产环境配置后继续部署。'}
  ]; render(true);
 });
 assert.equal(await page.locator('#statusFilters').isVisible(),false);
 await page.screenshot({path:screenshot('agent-panel-timeline.png')});
 await page.emulateMedia({colorScheme:'dark'});await page.screenshot({path:screenshot('agent-panel-timeline-dark.png')});await page.emulateMedia({colorScheme:'light'});
 await page.locator('#view').selectOption('status');
 await page.keyboard.press('Control+k');assert.equal(await page.locator('#search').evaluate(e=>e===document.activeElement),true);
 await page.keyboard.press('Escape');
 await page.locator('#search').blur();
 await page.evaluate(()=>document.querySelector('#toast').hidden=true);
 assert.equal(await page.evaluate(()=>document.body.scrollWidth<=window.innerWidth),true,'no horizontal overflow');
 await page.emulateMedia({colorScheme:'dark'});
 await page.setViewportSize({width:340,height:480});
 assert.equal(await page.locator('.status-failed .resume-btn').evaluate(el=>el.getBoundingClientRect().bottom<=document.querySelector('#list').getBoundingClientRect().bottom),true,'both attention actions are visible at the minimum window size');
 await page.screenshot({path:screenshot('agent-panel-compact-window.png')});
 await page.locator('#settingsBtn').click();await page.waitForFunction(()=>!document.querySelector('#saveSettings').disabled);
 assert.equal(await page.locator('#workbuddyDir').inputValue(),'/Users/example/WorkBuddy Data');
 await page.locator('#workbuddyDir').clear();
 assert.equal(await page.evaluate(()=>document.body.scrollWidth<=window.innerWidth),true);
 await page.locator('#background').selectOption('clear');await page.locator('#saveSettings').click();await page.waitForFunction(()=>document.querySelector('#saveStatus').textContent==='已保存');await page.keyboard.press('Escape');
 assert.equal(saved.workbuddy_data_dir,'');
 assert.equal(await page.locator('#settingsBtn').isVisible(),true);
 visibleTasks=[
  {task_id:'wb',agent:'workbuddy',title:'等待测试结果',status:'idle',display_status:'waiting',mtime:now,link:'workbuddy://chat/wb',recent:'请选择测试结果'},
  {task_id:'qc',agent:'qoder-cn',title:'等待工具审批',status:'idle',display_status:'waiting',mtime:now,link:'qoder-cn://chat/open?sessionId=qc',recent:''},
 ];
 await page.evaluate(()=>tick());
 await page.waitForFunction(()=>document.querySelector('#stAttention').textContent==='2'&&document.querySelector('.a-workbuddy'));
 for(const [title,link] of [['等待测试结果','workbuddy://chat/wb'],['等待工具审批','qoder-cn://chat/open?sessionId=qc']]){
  await page.getByRole('button',{name:'回到会话：'+title,exact:true}).click();
  assert.equal(await page.evaluate(()=>window.lastOpened),link);
 }
 visibleTasks=[...visibleTasks,{...tasks[2],reply_needed:'你回复 A/B/C 后我再继续。'}];
 await page.locator('#search').fill('登录修复');await page.evaluate(()=>tick());
 await page.waitForFunction(()=>document.querySelector('.status-reply'));
 assert.equal(await page.locator('.group-title').textContent(),'待回复');
 assert.equal(await page.locator('#stAttention').textContent(),'1');
 await page.getByRole('button',{name:'查看问题：登录修复'}).click();
 assert.equal(await page.locator('#replyBody').textContent(),replyText);
 await page.keyboard.press('Escape');
 assert.deepEqual(errors,[]);console.log('PASS: counts, priorities, search, details, stable DOM, direct navigation, connection recovery, save retry, notifications, timeline, keyboard, layout');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
