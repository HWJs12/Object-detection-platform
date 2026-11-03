/* Regressions: stopping a video must invalidate queued frames and clear its preview. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page() {
    const elements = new Map(), images = [], sockets = [], saved = new Map();
    let draws = 0, clears = 0;
    function element(selector) {
        if (!elements.has(selector)) {
            const hidden = new Set(['#video-canvas','#summary-card','#paused-mask','#count-warning','#btn-download-video','#btn-stop']);
            const classes = new Set(hidden.has(selector) ? ['hidden'] : []);
            elements.set(selector, {
                value: '', textContent: '', innerHTML: '', width:640, height:480,
                style: {}, disabled:false, dataset:{},
                classList: {add:c=>classes.add(c), remove:c=>classes.delete(c), contains:c=>classes.has(c),
                    toggle(c,on) { const next=on===undefined?!classes.has(c):on; next?classes.add(c):classes.delete(c); }},
                getContext:()=>({clearRect:()=>{clears++;}}),
                addEventListener() {}, setAttribute() {}, removeAttribute() {},
            });
        }
        return elements.get(selector);
    }
    class Image {
        constructor() { this.complete=false; this.width=640; this.height=480; images.push(this); }
        removeAttribute(name) { if(name==='src')this.src=''; }
    }
    class WebSocket {
        static CLOSED=3; static OPEN=1;
        constructor() {this.readyState=1;sockets.push(this);}
        close() {this.readyState=3;this.onclose?.();}
    }
    const context=vm.createContext({
        document:{addEventListener(){}},window:{addEventListener(){}},
        location:{protocol:'http:',host:'127.0.0.1:8766'},
        localStorage:{setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k),getItem:k=>saved.get(k)||null},
        performance:{now:()=>10000}, Image,WebSocket,
        $:element,$$:()=>[], API:{get:async()=>({items:[]})},
        setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},
        toast(){},fmtNumber:v=>String(v??0),escapeHtml:v=>String(v??''),classColor:()=> '#123456',
        drawDetections(canvas,img){draws++;canvas.width=img.width;canvas.height=img.height;},
        URLSearchParams,
    });
    const script=process.env.VIDEO_SCRIPT||path.join(__dirname,'..','web','js','video.js');
    vm.runInContext(fs.readFileSync(script,'utf8'),context,{filename:'video.js'});
    const run=code=>vm.runInContext(code,context);
    const frame={type:'frame',total:7,unique:2,unique_count_valid:true,fps:5,timestamp:2,
        counts:[{class_name:'fish',count:7}],detections:[{class_name:'fish',class_id:0}],
        progress:35,frame_index:10,conf_threshold:.25,image:'fixture'};
    function start() {
        run("currentFile={name:'fixture.mp4',size:100};currentTaskId='old';rememberTask('old');openSocket('old');");
        const ws=sockets.at(-1); ws.onopen(); return ws;
    }
    function send(ws,msg) {ws.onmessage({data:JSON.stringify(msg)});}
    function render(ws) {send(ws,frame);images[0].complete=true;images[0].onload();}
    return {context,element,run,frame,start,send,render,images,sockets,saved,
        draws:()=>draws,clears:()=>clears};
}

const tests=[];
function test(name,fn) {tests.push({name,fn});}
function assertCleared(p) {
    assert.equal(p.element('#video-canvas').classList.contains('hidden'),true,'Old detection canvas must be hidden');
    assert.equal(p.element('#video-canvas').width,0,'Old canvas pixels must be cleared');
    assert.equal(p.element('#stage-placeholder').classList.contains('hidden'),false);
    assert.equal(p.run('renderedMsg'),null);
    assert.equal(p.run('pendingFrame'),false);
    assert.equal(p.element('#live-total').textContent,'0');
    assert.equal(p.element('#live-fps').textContent,'0.0');
    assert.equal(p.element('#live-time').textContent,'0.0 s');
    assert.equal(p.element('#live-peak').textContent,'0');
    assert.equal(p.element('#progress-bar').style.width,'0%');
    assert.equal(p.element('#summary-card').classList.contains('hidden'),true);
    assert.equal(p.element('#paused-mask').classList.contains('hidden'),true);
}

test('stop clears immediately, drops late frames and ignores queued decode',async()=>{
    const p=page(),ws=p.start();p.render(ws);
    assert.equal(p.element('#video-canvas').classList.contains('hidden'),false);
    p.send(ws,p.frame);const queued=p.images[0].onload;
    p.run("paused=true;setControls('paused')");
    let resolve;
    p.context.API.postJSON=()=>new Promise(r=>{resolve=r;});
    const pending=p.run("controlVideo('stop')");
    assertCleared(p);assert.equal(p.run('stopping'),true);
    const draws=p.draws(); queued();
    p.send(ws,{...p.frame,total:99});p.send(ws,{type:'paused'});
    assertCleared(p);assert.equal(p.draws(),draws);
    resolve({paused:false});await pending;
    assert.equal(p.run('currentTaskId'),'old','Keep task ownership until server releases it');
    p.send(ws,{type:'stopped',message:'stopped'});
    assertCleared(p);assert.equal(p.run('currentTaskId'),null);
    assert.equal(p.saved.size,0,'Do not auto-restore a stopped task');
    p.send(ws,{...p.frame,total:123});assertCleared(p);
});

test('server stopped notification clears a rendered frame without local stop click',()=>{
    const p=page(),ws=p.start();p.render(ws);
    p.send(ws,{type:'stopped'});assertCleared(p);
    assert.match(p.element('#progress-text').textContent,/已停止/);
});

test('a stopped reconnect snapshot cannot replay the cached last frame',()=>{
    const p=page(),ws=p.start();p.render(ws);
    p.send(ws,{type:'task',task:{task_id:'old',source_name:'old.mp4',state:'stopped',paused:false,
        terminal:{type:'stopped'},meta:{width:640,height:480,fps:25,total_frames:100},conf_threshold:.25}});
    p.send(ws,p.frame);assertCleared(p);
});

test('reopening a stopped task displays status without reconnecting to its old frame',async()=>{
    const p=page();
    p.context.API.get=async()=>({task_id:'stopped',source_name:'old.mp4',state:'stopped',paused:false,
        terminal:{type:'stopped'},meta:{width:640,height:480,fps:25,total_frames:100},conf_threshold:.25});
    await p.run("attachVideoJob('stopped')");
    assertCleared(p);assert.equal(p.sockets.length,0);assert.equal(p.saved.size,0);
});

test('normal completion keeps the final frame available for result inspection',()=>{
    const p=page(),ws=p.start();p.render(ws);
    p.send(ws,{type:'done',summary:{unique_objects:2,unique_count_valid:true,total_detections:7,
        frames_processed:10,total_frames:10,avg_fps:5,peak_objects:7,avg_objects:7,counts:[]}});
    assert.equal(p.element('#video-canvas').classList.contains('hidden'),false);
    assert.equal(p.element('#summary-card').classList.contains('hidden'),false);
    assert.notEqual(p.run('renderedMsg'),null);
});

test('choosing a different file clears a previous completed preview',()=>{
    const p=page(),ws=p.start();p.render(ws);
    p.run('currentTaskId=null;socket=null;');
    p.run("selectFile({name:'new.mp4',size:500})");
    assertCleared(p);assert.equal(p.run('currentFile.name'),'new.mp4');
});

test('ordinary pause and resume preserve the current frame',async()=>{
    const p=page(),ws=p.start();p.render(ws);
    p.context.API.postJSON=async(url,body)=>({paused:body.action==='pause',frame_index:10});
    await p.run("controlVideo('pause')");
    assert.equal(p.element('#video-canvas').classList.contains('hidden'),false);
    assert.equal(p.element('#paused-mask').classList.contains('hidden'),false);
    await p.run("controlVideo('resume')");
    assert.equal(p.element('#video-canvas').classList.contains('hidden'),false);
    assert.equal(p.element('#paused-mask').classList.contains('hidden'),true);
});

test('failed stop restores truthful paused controls instead of claiming success',async()=>{
    const p=page(),ws=p.start();p.render(ws);p.run("paused=true;setControls('paused')");
    p.context.API.postJSON=async()=>{throw new Error('temporary network failure');};
    await p.run("controlVideo('stop')");
    assert.equal(p.run('stopping'),false);assert.equal(p.run('paused'),true);
    assert.equal(p.element('#btn-resume').disabled,false);
    assert.match(p.element('#progress-text').textContent,/停止失败/);
});

test('old image error cannot unlock a new task decode',()=>{
    const p=page(),ws=p.start();p.send(ws,p.frame);
    const oldError=p.images[0].onerror;
    p.run('resetLiveStats()');p.send(ws,p.frame);
    assert.equal(p.run('pendingFrame'),true);oldError();assert.equal(p.run('pendingFrame'),true);
});

test('a late pause response does not restore the paused mask during stopping',async()=>{
    const p=page(),ws=p.start();p.render(ws);
    let pause,stop;
    p.context.API.postJSON=(url,body)=>new Promise(r=>{if(body.action==='pause')pause=r;else stop=r;});
    const first=p.run("controlVideo('pause')"),second=p.run("controlVideo('stop')");
    pause({paused:true,frame_index:10});await first;assertCleared(p);
    stop({paused:false});await second;p.send(ws,{type:'stopped'});assertCleared(p);
});

(async()=>{
    for(const {name,fn} of tests){await fn();console.log('PASS',name);}
    console.log(`Video stop regressions passed: ${tests.length} cases.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
