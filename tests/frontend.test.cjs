'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const source = (file) => fs.readFileSync(path.join(root, file), 'utf8');

function mobileContext(search = '') {
  const elements = new Map();
  const element = () => ({textContent:'',innerHTML:'',value:'0',max:'0',disabled:false,hidden:false,
    classList:{add(){},remove(){},contains(){return true;},toggle(){}},
    setAttribute(){},addEventListener(){},querySelectorAll(){return [];},getContext(){return {clearRect(){},createLinearGradient(){return {addColorStop(){}}},fillRect(){}}},appendChild(){}});
  const context = vm.createContext({URL,URLSearchParams,AbortController,Image:class{},setTimeout,clearTimeout,setInterval,clearInterval,console,
    window:{location:{pathname:'/',search,href:'http://localhost/'+search}},
    document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},addEventListener(){},querySelectorAll(){return [];},createElement:element},
    navigator:{},localStorage:{setItem(){}}});
  vm.runInContext(source('web/static/js/mobile.js'),context);
  return context;
}

for (const filename of ['web/static/js/app.js','web/static/js/mobile.js']) {
 test(`${filename}: canceled preloads can be queued again`, () => {
  const code=source(filename).match(/function clearLoadQueue\(\) \{[\s\S]*?^\}/m)[0];
  const context=vm.createContext({_loadQueue:['queued'],_imgCache:new Map([['queued','pending'],['active','pending'],['loaded','ok']]),_productUrls:['queued']});
  vm.runInContext(code+'; clearLoadQueue();',context);
  assert.equal(context._imgCache.has('queued'),false);
  assert.equal(context._imgCache.get('active'),'pending');
  assert.equal(context._imgCache.get('loaded'),'ok');
 });
}

test('mobile: all desktop products and explicit model support are available', () => {
 const ctx=mobileContext();
 const catalog=vm.runInContext('PARAMS.flatMap(group => group.items)',ctx);
 const desktop=[...source('web/static/index.html').matchAll(/<button\b([^>]*)>[\s\S]*?<\/button>/g)];
 const expected=new Map();
 for(const [,attrs] of desktop){const param=attrs.match(/data-param="([^"]+)"/);const models=attrs.match(/data-models="([^"]+)"/);if(param&&models)expected.set(param[1],models[1].split(',').sort());}
 assert.equal(catalog.length,expected.size);
 for(const item of catalog) assert.deepEqual(Array.from(item.models).sort(),expected.get(item.id),item.id);
 assert.equal(vm.runInContext("modelSupportsParam('rap', {models:['hrrr']})",ctx),false);
});

test('mobile: subhourly forecast and satellite ordinal times remain valid', () => {
 const ctx=mobileContext();
 assert.equal(vm.runInContext("mobileForecastMinutes('01_15min')",ctx),75);
 const ordinal=vm.runInContext("formatTs('2026-273T17:56:00Z')",ctx);
 const calendar=vm.runInContext("formatTs('2026-09-30T17:56:00Z')",ctx);
 assert.equal(ordinal,calendar);
 assert.ok(!ordinal.includes('273T'));
});

test('mobile: shell switching retains the requested weather view', () => {
 const ctx=mobileContext('?view=mobile&source=models&model=nam12km');
 assert.equal(vm.runInContext('S.tab',ctx),'models');
 assert.equal(vm.runInContext('S.model',ctx),'nam12km');
});

test('mobile: delayed model response cannot overwrite a newer selection', async () => {
 const ctx=mobileContext();
 vm.runInContext(`S.tab='models'; S.model='hrrr'; S.param='temp_2m';
   let releaseOld;
   api = (path) => path.includes('model=hrrr') ? new Promise(resolve => { releaseOld=resolve; }) : Promise.resolve([]);
   clearOverlay=()=>{};
   const oldLoad=loadRun();
   S.model='gfs';
   const newLoad=loadRun();`,ctx);
 await vm.runInContext('newLoad',ctx);
 vm.runInContext("releaseOld([{run_id:'2026010100'}])",ctx);
 await vm.runInContext('oldLoad',ctx);
 assert.equal(vm.runInContext('S.model',ctx),'gfs');
 assert.equal(vm.runInContext('S.runId',ctx),null);
 assert.equal(vm.runInContext('S.modelRuns.length',ctx),0);
});

test('mobile: failed surface frames are handled and stop playback', async () => {
 const ctx=mobileContext();
 vm.runInContext("S.tab='surface';S.surfaceFrames=[{generated:123}];S.isPlaying=true;api=async()=>{throw new Error('offline')}",ctx);
 await vm.runInContext('showSurfaceFrame(0)',ctx);
 assert.equal(vm.runInContext('S.isPlaying',ctx),false);
 assert.match(vm.runInContext('topInfo.textContent',ctx),/Unable to load/);
});

test('service worker: activation preserves unrelated application caches', async () => {
 const handlers={},deleted=[];
 const ctx=vm.createContext({self:{addEventListener(name,fn){handlers[name]=fn;},clients:{claim:async()=>{}},location:{origin:'https://example.test'}},caches:{keys:async()=>['unrelated-app','wx-pwa-v42',...vm.runInContext('[CACHE_NAME, IMAGE_CACHE]',ctx)],delete:async name=>deleted.push(name)}});
 vm.runInContext(source('web/static/sw.js'),ctx);
 let work;handlers.activate({waitUntil(promise){work=promise;}});await work;
 assert.deepEqual(deleted,['wx-pwa-v42']);
});

test('service worker: offline weather API returns an explicit failure, not stale data', async () => {
 const handlers={};const ctx=vm.createContext({URL,Response,self:{addEventListener(n,f){handlers[n]=f;},location:{origin:'https://example.test'}},fetch:async()=>{throw Error('offline')}});
 vm.runInContext(source('web/static/sw.js'),ctx);
 let response;handlers.fetch({request:{url:'https://example.test/api/alerts',method:'GET'},respondWith(promise){response=promise;}});
 assert.equal((await response).status,503);
});

test('service worker: image storage stays bounded', async () => {
 const deleted=[];const ctx=vm.createContext({self:{addEventListener(){}},caches:{open:async()=>({put:async()=>{},keys:async()=>Array.from({length:120},(_,i)=>i),delete:async key=>deleted.push(key)})}});
 vm.runInContext(source('web/static/sw.js'),ctx);
 await vm.runInContext("saveResponse('test',{}, {ok:true},96)",ctx);
 assert.equal(deleted.length,24);
});

test('all shipped scripts parse before browser execution', () => {
 for(const file of ['web/static/js/app.js','web/static/js/mobile.js','web/static/js/pressure-contours.js','web/static/js/map-capture.js','web/static/js/viewer-ui.js','web/static/js/surface-obs.js','web/static/sw.js']) {
  assert.doesNotThrow(()=>new vm.Script(source(file)),file);
 }
});

function mapCaptureContext() {
 const originals={model:{style:{visibility:''}},boundary:{style:{visibility:'visible'}}};
 const pane=(original)=>({children:[original],appendChild(canvas){this.children.push(canvas);canvas.parent=this;}});
 const panes={modelCanvasPane:pane(originals.model),boundaryPane:pane(originals.boundary)};
 const fills=[];
 const rectangle={options:{stroke:false,fill:true,fillColor:'#000000',fillOpacity:1},
  getLatLngs:()=>[[{lat:-200,lng:-300},{lat:-200,lng:1400},{lat:900,lng:1400},{lat:900,lng:-300}]]};
 const border={options:{stroke:true,color:'#ffffff',weight:1},getLatLngs:()=>[{lat:10,lng:10},{lat:20,lng:20}]};
 const mapEl={clientWidth:1000,clientHeight:600};
 const context=vm.createContext({state:{},window:{devicePixelRatio:2,_modelCanvasBackingLayer:rectangle,_stateBoundaryLayer:border},
  leafletMap:{getContainer:()=>mapEl,getPane:(name)=>panes[name],hasLayer:(layer)=>layer===rectangle||layer===border,
   containerPointToLayerPoint:()=>({x:237,y:-81}),latLngToContainerPoint:(point)=>({x:point.lng,y:point.lat})},
  document:{querySelector:()=>mapEl,createElement(){
   const drawing={scale(){},beginPath(){},moveTo(){},lineTo(){},closePath(){},stroke(){},fill(){fills.push(this.fillStyle);}};
   return {style:{},getContext:()=>drawing,remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};
  }},requestAnimationFrame:(callback)=>callback(),loadHtml2Canvas:async()=>{},buildCaptureMetadata:()=>'',beginMapLoading(){},endMapLoading(){}});
 const app=source('web/static/js/app.js');
 const start=app.indexOf('async function captureMapSectionCanvas()');
 vm.runInContext(app.slice(start,app.indexOf('\nfunction buildCaptureFilename()',start)),context);
 return {context,panes,originals,fills};
}

test('map export preserves the full model background, pan position and layer order', () => {
 const {context,panes,originals,fills}=mapCaptureContext();
 vm.runInContext('cleanup=prepareMapCaptureOverlays()',context);
 assert.deepEqual(fills,['#000000']);
 for(const name of ['modelCanvasPane','boundaryPane']) {
  const canvas=panes[name].children[1];
  assert.equal(canvas.width,2000);assert.equal(canvas.height,1200);
  assert.equal(canvas.style.left,'237px');assert.equal(canvas.style.top,'-81px');
 }
 assert.equal(originals.model.style.visibility,'hidden');
 assert.equal(originals.boundary.style.visibility,'hidden');
 vm.runInContext('cleanup()',context);
 assert.equal(originals.model.style.visibility,'');
 assert.equal(originals.boundary.style.visibility,'visible');
 assert.deepEqual(panes.modelCanvasPane.children,[originals.model]);
 assert.deepEqual(panes.boundaryPane.children,[originals.boundary]);
});

test('failed screenshot export restores the map background and boundaries', async () => {
 const {context,panes,originals}=mapCaptureContext();
 context.html2canvas=async()=>{throw new Error('capture failed');};
 await assert.rejects(vm.runInContext('captureMapSectionCanvas()',context),/capture failed/);
 assert.equal(originals.model.style.visibility,'');
 assert.equal(originals.boundary.style.visibility,'visible');
 assert.deepEqual(panes.modelCanvasPane.children,[originals.model]);
 assert.deepEqual(panes.boundaryPane.children,[originals.boundary]);
});

test('old model runs are clearly distinguished from recent runs', () => {
 const ctx=mobileContext();
 assert.equal(vm.runInContext("modelRunIsOld('2026093000','gdps',Date.UTC(2026,8,30,18))",ctx),false);
 assert.equal(vm.runInContext("modelRunIsOld('2026052100','gdps',Date.UTC(2026,8,30,18))",ctx),true);
 assert.equal(vm.runInContext("modelRunIsOld('2026093000','hrrr',Date.UTC(2026,8,30,18))",ctx),true);
});

function fakeTimers(ctx) {
 const timers=new Map();let id=0;
 ctx.setTimeout=(fn,delay)=>{timers.set(++id,{fn,delay});return id;};
 ctx.clearTimeout=key=>timers.delete(key);
 return {timers, next(){assert.equal(timers.size,1);const [key,task]=timers.entries().next().value;timers.delete(key);task.fn();return task.delay;}};
}
function desktopPlaybackContext() {
 const ctx=vm.createContext({state:{dataSource:'radar',radarFrames:[{},{},{}],radarFrameIdx:0},
  leafletMap:null,playBtn:{setAttribute(){}},PAUSE_ICON_SVG:'pause',PLAY_ICON_SVG:'play',
  _imgCache:new Map(),_getNextDesktopFrameUrl:()=>null});
 const clock=fakeTimers(ctx);const app=source('web/static/js/app.js');
 vm.runInContext(app.slice(app.indexOf('const LOOP_END_PAUSE_MULTIPLIER'),app.indexOf('\nfunction play()'))+'\n'+app.match(/function pause\(\) \{[\s\S]*?^\}/m)[0],ctx);
 return {ctx,clock};
}
test('desktop animation holds the last frame before wrapping; first frame has normal timing',()=>{
 const {ctx,clock}=desktopPlaybackContext();
 vm.runInContext('startLoopPlayback(100,()=>{state.radarFrameIdx=(state.radarFrameIdx+1)%3;});',ctx);
 assert.equal(ctx.state.radarFrameIdx,1);
 assert.equal(clock.next(),100);
 assert.equal(ctx.state.radarFrameIdx,2);
 assert.equal(clock.next(),1000);
 assert.equal(ctx.state.radarFrameIdx,0);
 assert.equal(clock.next(),100);
 vm.runInContext('pause()',ctx);assert.equal(clock.timers.size,0);
});
test('desktop last-frame hold begins after image load and canceled callbacks cannot resume playback',()=>{
 const {ctx,clock}=desktopPlaybackContext();const listeners=new Map();
 ctx.img={complete:false,addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:(name,fn)=>{if(listeners.get(name)===fn)listeners.delete(name);}};
 vm.runInContext('getActiveOverlayImage=()=>img;state.radarFrameIdx=1;startLoopPlayback(100,()=>{state.radarFrameIdx=(state.radarFrameIdx+1)%3;});',ctx);
 assert.equal(clock.timers.size,0);const oldLoad=listeners.get('load');
 ctx.img.complete=true;oldLoad();assert.equal(clock.timers.values().next().value.delay,1000);
 vm.runInContext('pause();img.complete=false;startLoopPlayback(100,()=>{});',ctx);
 oldLoad();assert.ok(listeners.has('load'));assert.equal(clock.timers.size,0);
 vm.runInContext('pause()',ctx);assert.equal(listeners.size,0);
 oldLoad();assert.equal(clock.timers.size,0);
});
test('desktop last-frame detection supports subhourly models and every animated view',()=>{
 const {ctx}=desktopPlaybackContext();
 for(const [type,props] of [['models',{currentFxx:'00_45min',availableFxx:[0,'00_15min','00_45min']}],['satellite',{satFrameIdx:1,satFrames:[{},{}]}],['home',{homeRadarFrameIdx:1,homeRadarFrames:[{},{}]}]]) {
  Object.assign(ctx.state,props,{dataSource:type});assert.equal(vm.runInContext('isLastPlaybackFrame()',ctx),true,type);
 }
 ctx.state.dataSource='chase';ctx.chaseState={currentFxx:3,availableFxx:[0,3]};assert.equal(vm.runInContext('isLastPlaybackFrame()',ctx),true);
});
test('phone radar animation holds the final frame, then wraps without an extra first-frame pause',()=>{
 const ctx=mobileContext();const clock=fakeTimers(ctx);
 vm.runInContext("S.tab='radar';S.speedIdx=0;scrubSlider.max='2';scrubSlider.value='0';frameUrl=()=>null;onScrub=()=>{S.radarIdx=Number(scrubSlider.value)};play();",ctx);
 const normal=vm.runInContext('SPEEDS[S.speedIdx].ms',ctx);
 assert.equal(vm.runInContext('S.radarIdx',ctx),1);
 assert.equal(clock.next(),normal);assert.equal(vm.runInContext('S.radarIdx',ctx),2);
 assert.equal(clock.next(),Math.max(1000,normal*1.75));assert.equal(vm.runInContext('S.radarIdx',ctx),0);
 assert.equal(clock.next(),normal);vm.runInContext('pause()',ctx);assert.equal(clock.timers.size,0);
});

function pressureVectorContext() {
 const requests=new Map(),events=[];
 function Group() {}
 Group.prototype.initialize=function(layers,options){this.options=options||{};this.layers=[];};
 Group.prototype.clearLayers=function(){this.layers=[];};
 Group.prototype.addLayer=function(layer){this.layers.push(layer);};
 Group.prototype.onRemove=function(){};
 Group.prototype.fire=function(name){events.push(name);};
 Group.extend=function(methods){function Layer(...args){this.initialize(...args);}Layer.prototype=Object.assign(Object.create(Group.prototype),methods);return Layer;};
 const ctx=vm.createContext({window:{},Event,fetch:(url)=>new Promise(resolve=>requests.set(url,resolve)),
  L:{LayerGroup:Group,Marker:class{},geoJSON:(data,options)=>({data,options}),marker:(position,options)=>({position,options}),divIcon:(options)=>options}});
 vm.runInContext(source('web/static/js/pressure-contours.js'),ctx);
 const layer=ctx.window.PressureContours.layer('old.contours.json');
 layer._map={};layer._renderer={_container:new EventTarget(),remove(){}};
 const data=(value)=>({type:'FeatureCollection',features:[{type:'Feature',properties:{kind:'isobar',major:true,opacity:.65,value},geometry:{type:'MultiLineString',coordinates:[[[0,0],[1,1]]]}}]});
 const resolve=(url,value)=>requests.get(url)({ok:true,json:async()=>data(value)});
 return {ctx,layer,events,resolve};
}

test('late pressure-vector downloads cannot replace the selected forecast frame', async()=>{
 const {layer,events,resolve}=pressureVectorContext();
 layer.setUrl('old.contours.json');const old=layer._loading;
 layer.setUrl('new.contours.json');const current=layer._loading;
 resolve('new.contours.json',1008);await current;
 resolve('old.contours.json',1004);await old;
 assert.equal(layer._data.features[0].properties.value,1008);
 assert.deepEqual(events,['load']);
 assert.equal(layer.getElement().complete,true);
});

test('removing pressure vectors cancels pending frame updates', async()=>{
 const {layer,events,resolve}=pressureVectorContext();
 layer.setUrl('old.contours.json');const pending=layer._loading;
 layer.onRemove({});resolve('old.contours.json',1004);await pending;
 assert.deepEqual(layer.layers,[]);assert.deepEqual(events,[]);
 assert.equal(layer._data,null);
});

test('pressure frame selection uses vectors when available and PNGs for older frames',()=>{
 const ctx=mobileContext();
 ctx.window.PressureContours={isUrl:()=>true};
 vm.runInContext("S.model='nam3km';S.runId='2026100812';S.param='mslp';S.vectorAvailability={mslp:[0,2]};",ctx);
 assert.match(vm.runInContext('mobileModelFrameUrl(0)',ctx),/f00\.contours\.json/);
 assert.match(vm.runInContext('mobileModelFrameUrl(1)',ctx),/f01\.png/);
});

function captureMetadataContext() {
 const ctx=vm.createContext({window:{},Date});
 vm.runInContext(source('web/static/js/map-capture.js'),ctx);
 return ctx.window.MapCaptureMetadata;
}

test('export metadata contains the selected model, run and subhourly valid time across midnight',()=>{
 const capture=captureMetadataContext();
 const text=capture.model({model:'nam3km',product:'MSLP (mb)',runId:'2026100823',fxx:'01_15min'});
 assert.match(text,/NAM 3 km · MSLP \(mb\)/);
 assert.match(text,/Run: 2026-10-08 23:00 UTC · F\+01:15/);
 assert.match(text,/Valid: 2026-10-09 00:15 UTC/);
 assert.match(capture.model({model:'hrrr',runId:'invalid',fxx:0}),/Valid: --/);
});

test('export caption resets the scaled capture context and fits inside phone images',()=>{
 const capture=captureMetadataContext(),rectangles=[],texts=[],transforms=[];
 const ctx={save(){},restore(){},measureText:(text)=>({width:text.length*7}),
  setTransform:(...matrix)=>transforms.push(matrix),beginPath(){},roundRect:(...rect)=>rectangles.push(rect),fill(){},stroke(){},
  fillText:(text,x,y)=>texts.push({text,x,y})};
 const canvas={width:780,height:1688,getContext:()=>ctx};
 const text=capture.model({model:'nam3km',product:'A long product title that should wrap neatly inside the exported phone image',runId:'2026100812',fxx:48});
 assert.equal(capture.stamp(canvas,text,{scale:2}),canvas);
 assert.deepEqual(transforms,[[2,0,0,2,0,0]]);
 const [x,y,width,height]=rectangles[0];
 assert.ok(x>=0&&y>=0&&x+width<=390&&y+height<=844);
 assert.ok(texts.every(row=>row.x>=0&&row.x+ctx.measureText(row.text).width<=390&&row.y<=844));
 assert.ok(texts.some(row=>row.text.includes('Run:')));
 assert.ok(texts.some(row=>row.text.includes('Valid:')));
});

function viewerUIContext(saved='[]') {
 const storage=new Map([['wx_favorite_products',saved]]),handlers={};
 const ctx=vm.createContext({window:{addEventListener:(name,handler)=>{handlers[name]=handler;}},
  localStorage:{getItem:(key)=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}});
 vm.runInContext(source('web/static/js/viewer-ui.js'),ctx);
 return {ui:ctx.window.ViewerUI,storage,handlers};
}

test('favorites persist, reject invalid identifiers, and follow storage changes',()=>{
 const {ui,storage,handlers}=viewerUIContext('["mslp","mslp","bad/id",null]');
 assert.deepEqual(Array.from(ui.favorites.list()),['mslp']);
 ui.favorites.toggle('temp_2m');ui.favorites.toggle('bad/id');
 assert.deepEqual(JSON.parse(storage.get('wx_favorite_products')),['mslp','temp_2m']);
 ui.favorites.toggle('mslp');assert.equal(ui.favorites.has('mslp'),false);
 storage.set('wx_favorite_products','["reflectivity"]');handlers.storage({key:'wx_favorite_products'});
 assert.deepEqual(Array.from(ui.favorites.list()),['reflectivity']);
 assert.deepEqual(Array.from(viewerUIContext('invalid JSON').ui.favorites.list()),[]);
});

test('run labels distinguish completeness, progress and age in plain language',()=>{
 const {ui}=viewerUIContext();
 assert.equal(ui.runStatus({is_complete:true}),'Complete');
 assert.equal(ui.runStatus({completed_fxx:[0,1]}),'Updating');
 assert.equal(ui.runStatus({completed_fxx:[]}),'Waiting');
 assert.equal(ui.runStatus({is_complete:true},true),'Older · Complete');
});

test('reference-layer refreshes retain the viewport unless fitting is requested',()=>{
 let fits=0;
 const ctx=vm.createContext({state:{parameter:'mslp'},window:{},
  leafletMap:{getZoom:()=>8,setMaxBounds(){},setMinZoom(){}},baseTileLayer:null,labelTileLayer:null,
  defaultMinZoom:0,REFERENCE_COUNTY_ZOOM_THRESHOLD:5,
  updateSatelliteViewport:()=>fits++,updateModelViewport:()=>fits++,resetModelBackgroundWash(){},resetSatelliteBackgroundWash(){},
  clearSatellitePaneClip(){},updateModelCityLayerVisibility(){},updateModelDetailLayerVisibility(){},updateModelFrame(){},setLayerVisible(){}});
 const app=source('web/static/js/app.js'),start=app.indexOf('function setReferenceOverlayMode(');
 vm.runInContext(app.slice(start,app.indexOf('\nfunction setSatelliteBasemapMode(',start)),ctx);
 vm.runInContext("setReferenceOverlayMode('models');setReferenceOverlayMode('satellite');",ctx);
 assert.equal(fits,0);
 vm.runInContext("setReferenceOverlayMode('models',{fitViewport:true});",ctx);
 assert.equal(fits,1);
});
