'use strict';
// Run against an isolated API: WX_TEST_URL=http://127.0.0.1:8011 node tests/viewer.browser.cjs
const assert = require('node:assert/strict');
const { chromium } = require(process.env.WX_PLAYWRIGHT_PATH || 'playwright');
const base = process.env.WX_TEST_URL || 'http://127.0.0.1:8011';
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jfN0AAAAASUVORK5CYII=', 'base64');
const run = '2026093000';
const oldRun = '2026092900';
const bounds = {south:25,north:50,west:-120,east:-70,center_lat:38,center_lon:-97};
let passed = 0;
function check(condition, message) { assert.ok(condition, message); passed++; console.log(`PASS ${message}`); }
(async () => {
 const browser=await chromium.launch({headless:true,executablePath:process.env.WX_CHROMIUM || undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
 for(const mobile of [false,true]) {
  const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1440,height:1000},isMobile:mobile,hasTouch:mobile,serviceWorkers:'block'});
  const page=await context.newPage();page.setDefaultTimeout(15000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/rendered/**',route=>route.fulfill({contentType:'image/png',body:image}));
  await page.route('**/api/**',async route=>{
    const url=new URL(route.request().url());const path=url.pathname;
    let data;let delay=0;
    if(path==='/api/runs') {const model=url.searchParams.get('model');delay=model==='gfs'?400:20;data=model==='rtma'?[]:[{model,run_id:run,is_complete:model!=='gdps',completed_fxx:[0,1,2],forecast_hours:[0,1,2],palette_version:1},{model,run_id:model==='gdps'?'2026052100':oldRun,is_complete:true,completed_fxx:[0,1,2],forecast_hours:[0,1,2]}];}
    else if(path.startsWith('/api/runs/')) {const parts=path.split('/');data={model:parts[3],run_id:parts[4],forecast_hours:[0,1,2],completed_fxx:[0,1,2],parameters:['temp_2m','reflectivity']};delay=parts[4]===oldRun?350:15;}
    else if(path.startsWith('/api/available/')) {data={temp_2m:[0,1,2],reflectivity:[0,1,2]};delay=path.endsWith(oldRun)?350:15;}
    else if(path.startsWith('/api/bounds/')) data=bounds;
    else if(path.startsWith('/api/satellite/frames/')) {delay=300;data=[{timestamp:'2026-273T17:56:00Z',png_path:'satellite/test.png',bounds}];}
    else if(path.startsWith('/api/satellite/latest/')) {delay=300;data={timestamp:'2026-273T17:56:00Z',png_path:'satellite/test.png',bounds};}
    else if(path.startsWith('/api/radar/tilts/')) data={tilts:[.5,1.5]};
    else if(path.startsWith('/api/radar/frames/')) {const tilt=Number(url.searchParams.get('tilt')||.5);data=[{timestamp:'20260930_120000',png_path:`radar/tilt${tilt}.png`,tilt,bounds,palette_version:2},{timestamp:'20260930_120500',png_path:`radar/tilt${tilt}-2.png`,tilt,bounds,palette_version:2}];}
    else if(path.startsWith('/api/radar/render/')) data={timestamp:'20260930_120500',png_path:'radar/upper.png',tilt:1.3,bounds,palette_version:2};
    if(data===undefined) return route.continue();
    if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
    await route.fulfill({json:data});
  });
  await page.goto(`${base}/?view=${mobile?'mobile':'models'}&source=models&model=hrrr&param=temp_2m`);
  await page.waitForFunction(mobile?()=>S.runId==='2026093000'&&S.fxxList.length===3:()=>state.runId==='2026093000'&&state.availableFxx.length===3);
  check(true,`${mobile?'mobile':'desktop'} model load`);
  if(mobile) {
    await page.evaluate(async()=>{S.model='gdps';await loadRun();});
    check(await page.evaluate(()=>S.runId==='2026093000'),'mobile uses fresh partial data before old completed runs');
    await page.evaluate(async()=>{S.model='gfs';const a=loadRun();S.model='nam3km';const b=loadRun();await Promise.all([a,b]);});
    check(await page.evaluate(()=>S.model==='nam3km'&&S.modelRuns.every(r=>r.model==='nam3km')),'mobile model switching ignores delayed responses');
    await page.evaluate(async()=>{await loadRun('2026092900');S.param='reflectivity';await loadRun(S.runId);});
    check(await page.evaluate(()=>S.runId==='2026092900'&&S.param==='reflectivity'),'mobile product change preserves selected run');
    await page.locator('#selectorPill').click();
    await page.locator('#modelProductSearch').fill('snow');
    check(await page.locator('[data-param]:visible').count()>0,'mobile product search');
    check(await page.locator('.chip[data-model="nam12km"]').count()===1,'mobile NAM 12km available');
    await page.keyboard.press('Escape');
    await page.evaluate(async()=>{const a=switchTab('satellite');const b=switchTab('models');await Promise.all([a,b]);});
    check(await page.evaluate(()=>S.tab==='models'&&!S.overlayLayer.getElement().src.includes('satellite/test')),'mobile tab switching cannot restore old satellite overlay');
    await page.evaluate(async()=>{await switchTab('radar');S.radarTilt=1.5;await loadRadarFrames();});
    check(await page.evaluate(()=>S.radarFrames.length===1&&S.radarFrames[0].tilt===1.3&&S.radarTilt===1.3&&S.overlayLayer.getElement().src.includes('upper.png')),'mobile upper radar tilt uses the requested sweep');
    check(await page.evaluate(()=>S.overlayLayer.getElement().src.includes('palette=2')),'mobile radar cache uses the frame palette version');
    check(await page.evaluate(()=>{const css=getComputedStyle(S.overlayLayer.getElement());return css.imageRendering==='pixelated'&&css.transitionDuration==='0s';}),'mobile radar preserves gate edges without crossfading');
    const oldLegend = page.waitForRequest(r=>r.url().includes('radar_palette_version=1'));
    await page.evaluate(()=>{S.radarFrames[S.radarIdx].palette_version=1;showRadarFrame(false);});
    await oldLegend;check(true,'mobile legend follows archived radar frames');
    await page.setViewportSize({width:320,height:740});
    check(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth),'mobile has no horizontal overflow at 320px');
    const boxes=await page.evaluate(()=>{const p=document.querySelector('#selectorPill').getBoundingClientRect();const l=document.querySelector('#legendBar').getBoundingClientRect();return {p:p.right,l:l.left}});
    check(boxes.p<=boxes.l,'mobile product selector and legend do not overlap');
    await page.evaluate(async()=>{await switchTab('models');});
    check(await page.evaluate(()=>getComputedStyle(S.overlayLayer.getElement()).imageRendering==='auto'),'mobile model imagery retains its own rendering after leaving radar');
    await page.evaluate(async()=>{S.model='rtma';await switchTab('models');});
    check(await page.evaluate(()=>S.runId===null&&S.overlayLayer===null&&playBtn.disabled),'mobile empty model clears old imagery and disables playback');
  } else {
    await page.evaluate(async()=>{state.model='gdps';await loadRuns();});
    check(await page.evaluate(()=>state.runId==='2026093000'),'desktop uses fresh partial data before old completed runs');
    await page.evaluate(async()=>{state.model='gfs';const a=loadRuns();state.model='nam3km';const b=loadRuns();await Promise.all([a,b]);});
    check(await page.evaluate(()=>state.model==='nam3km'&&state.runMeta.model==='nam3km'),'desktop model switching ignores delayed responses');
    await page.evaluate(async()=>{const a=selectRun('2026092900');const b=selectRun('2026093000');await Promise.all([a,b]);});
    check(await page.evaluate(()=>state.runId==='2026093000'&&state.runMeta.run_id==='2026093000'),'desktop run metadata commits atomically');
    await page.evaluate(async()=>{switchDataSource('satellite');await new Promise(r=>setTimeout(r,40));switchDataSource('models');await new Promise(r=>setTimeout(r,900));});
    check(await page.evaluate(()=>state.dataSource==='models'&&!state.satOverlayLayer),'desktop ignores satellite data after leaving the tab');
    await page.evaluate(async()=>{switchDataSource('radar');await new Promise(r=>setTimeout(r,100));state.radarTilt=1.5;await loadRadarTilt();});
    check(await page.evaluate(()=>state.radarFrames.length===1&&state.radarFrames[0].tilt===1.3&&state.radarTilt===1.3&&state.radarOverlayLayer.getElement().src.includes('upper.png')),'desktop upper tilt updates playback and image together');
    check(await page.evaluate(()=>state.radarOverlayLayer.getElement().src.includes('palette=2')),'desktop radar cache uses the frame palette version');
    check(await page.evaluate(()=>{const css=getComputedStyle(state.radarOverlayLayer.getElement());return css.imageRendering==='pixelated'&&css.transitionDuration==='0s';}),'desktop radar preserves gate edges without crossfading');
    const oldLegend = page.waitForRequest(r=>r.url().includes('radar_palette_version=1'));
    await page.evaluate(()=>{state.radarFrames[state.radarFrameIdx].palette_version=1;showRadarFrame(state.radarFrameIdx);});
    await oldLegend;check(true,'desktop legend follows archived radar frames');
    await page.setViewportSize({width:1024,height:768});
    check(await page.evaluate(()=>{const el=document.querySelector('#radarSiteSelect');const r=el.getBoundingClientRect();return r.width>150&&r.left>=0&&r.right<=innerWidth;}),'desktop radar station selector remains visible at tablet width');
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(async()=>{state.model='rtma';switchDataSource('models');await new Promise(r=>setTimeout(r,300));});
    check(await page.evaluate(()=>state.runId===null&&state.overlayLayer===null&&state.availableFxx.length===0),'desktop empty model clears previous run');
    await page.waitForFunction(()=>Boolean(window._stateBoundaryLayer));
    check(true,'desktop geographic boundary layer loads');
  }
  check(errors.length===0,`${mobile?'mobile':'desktop'} no JavaScript errors: ${errors.join(', ')}`);
  await context.close();
 }
 console.log(`${passed} browser checks passed`);
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
