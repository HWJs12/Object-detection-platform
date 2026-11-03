/* 三处虚拟海域：水面折射、随流波纹与各自岸线的拍岸浪花。 */
(() => {
    const stage = document.getElementById('map-stage');
    const image = document.getElementById('map-terrain');
    const canvas = document.getElementById('ocean-water');
    if (!stage || !image || !canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const surface = document.createElement('canvas');
    const water = surface.getContext('2d');
    const mask = document.createElement('canvas');
    const maskCtx = mask.getContext('2d', { willReadFrequently: true });
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, visible = true, width = 0, height = 0, builtFor = '', time = 0;
    // 坐标为地形底图像素中的真实沙滩边缘，法线朝向海水。
    const coasts = {
      1: [
        [[786,103],[695,105],[345,171],[402,242]],
        [[318,299],[279,302],[206,345],[250,388]],
        [[377,419],[411,458],[258,454],[256,480]],
        [[133,546],[170,555],[209,571],[185,589]],
      ],
      2: [
        [[1044,185],[991,189],[938,148],[887,144]],
        [[890,144],[828,162],[758,130],[702,111]],
        [[226,352],[217,391],[262,423],[304,437]],
        [[348,489],[333,527],[402,553],[460,582]],
        [[463,583],[585,665],[684,698],[780,704]],
        [[798,704],[865,748],[836,778],[780,796]],
      ],
      3: [
        [[919,157],[852,185],[777,145],[689,130]],
        [[685,129],[647,112],[626,91],[655,63]],
        [[1094,340],[1055,318],[975,278],[920,291]],
        [[1255,236],[1200,242],[1172,278],[1209,317]],
        [[371,633],[320,645],[270,630],[244,607]],
        [[820,765],[747,737],[710,788],[655,743]],
        [[652,742],[603,699],[545,699],[507,710]],
      ],
    };
    function point(c, u) {
        const v=1-u, x=v*v*v*c[0][0]+3*v*v*u*c[1][0]+3*v*u*u*c[2][0]+u*u*u*c[3][0];
        const y=v*v*v*c[0][1]+3*v*v*u*c[1][1]+3*v*u*u*c[2][1]+u*u*u*c[3][1];
        const dx=3*v*v*(c[1][0]-c[0][0])+6*v*u*(c[2][0]-c[1][0])+3*u*u*(c[3][0]-c[2][0]);
        const dy=3*v*v*(c[1][1]-c[0][1])+6*v*u*(c[2][1]-c[1][1])+3*u*u*(c[3][1]-c[2][1]);
        const length=Math.hypot(dx,dy)||1;
        return {x,y,nx:dy/length,ny:-dx/length};
    }
    function build() {
        if (!image.complete || !image.naturalWidth || !coasts[image.dataset.mapId]) return false;
        const rect=stage.getBoundingClientRect();
        const next=Math.round(Math.min(1280,Math.max(580,rect.width)));
        if (width===next && builtFor===image.currentSrc) return true;
        width=next; height=Math.round(width*5/8);
        canvas.width=surface.width=mask.width=width;
        canvas.height=surface.height=mask.height=height;
        maskCtx.drawImage(image,0,0,width,height);
        try {
            const pixels=maskCtx.getImageData(0,0,width,height), d=pixels.data;
            for(let i=0;i<d.length;i+=4) {
                // 水体偏青蓝，陆地/岩石偏灰；过渡区以柔和遮罩衔接。
                const blue=Math.max(1,d[i+2]), chroma=(d[i+1]-d[i])/blue;
                const a=Math.max(0,Math.min(1,(chroma-.31)/.12));
                d[i]=d[i+1]=d[i+2]=255; d[i+3]=Math.round(a*a*(3-2*a)*255);
            }
            maskCtx.putImageData(pixels,0,0);
        } catch (_) { return false; }
        builtFor=image.currentSrc; return true;
    }
    function surf(t) {
        const scale=width/1586;
        ctx.save(); ctx.scale(scale, height/992);
        (coasts[image.dataset.mapId] || []).forEach((coast,ci)=>{
            for(let wave=0;wave<4;wave++) {
                const phase=((t/5.6+wave/4+ci*.16)%1), arrival=phase<.78;
                // 先从近海推向沙滩，触岸后变宽并回退消散。
                const dist=arrival?54*Math.pow(1-phase/.78,1.25)-3:-3+(phase-.78)/.22*10;
                const alpha=Math.sin(Math.PI*phase)*(.36+.48*Math.min(1,phase/.6));
                ctx.beginPath();
                for(let j=0;j<=100;j++) {
                    const u=j/100,p=point(coast,u), taper=Math.pow(Math.sin(Math.PI*u),.4);
                    const wobble=Math.sin(u*37+t*2+wave)*1.2;
                    const offset=dist*taper+wobble;
                    const x=p.x+p.nx*offset,y=p.y+p.ny*offset;
                    if(j===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
                }
                ctx.strokeStyle=`rgba(192,243,249,${alpha*.52})`;
                ctx.lineWidth=6+7*Math.max(0,(phase-.6)/.4);ctx.stroke();
                ctx.strokeStyle=`rgba(235,255,255,${alpha})`;
                ctx.lineWidth=1.6+2.2*Math.max(0,(phase-.45)/.55);
                ctx.setLineDash([7,3,17,4,2,3]);ctx.lineDashOffset=-t*4;ctx.stroke();ctx.setLineDash([]);
                // 不均匀泡沫随浪头移动，避免一条整齐的发光曲线。
                for(let j=1;j<65;j++) {
                    const u=j/65,p=point(coast,u), taper=Math.pow(Math.sin(Math.PI*u),.4);
                    const off=dist*taper+Math.sin(j*13.7)*2.4;
                    ctx.fillStyle=`rgba(233,255,255,${alpha*.55*(.5+.5*Math.sin(j*8.4)**2)})`;
                    ctx.beginPath();ctx.ellipse(p.x+p.nx*off,p.y+p.ny*off,1.3+Math.sin(j)**2*1.5,.7,0,0,Math.PI*2);ctx.fill();
                }
            }
        });ctx.restore();
    }
    function draw(t) {
        if (!build()) return;
        water.clearRect(0,0,width,height);
        // 对水体作连续的局部折射，底图里的陆地和站点保持固定。
        const strip=5, scale=width/1586;
        for(let y=0;y<height;y+=strip) {
            const dy=Math.min(strip,height-y), shift=(Math.sin(y*.065-t*2.0)*2.5+Math.sin(y*.026+t*1.1)*1.6)*scale;
            water.drawImage(image,0,y/height*image.naturalHeight,image.naturalWidth,dy/height*image.naturalHeight,shift,y,width,dy);
        }
        water.save();water.scale(width/1586,height/992);
        // 弧形短水纹沿一致方向流动；不同长度、亮度与间距减少平铺感。
        for(let i=0;i<155;i++) {
            const x=((i*137.51-t*17)%1700+1700)%1700-55;
            const y=((i*79.31-t*7)%1080+1080)%1080-44;
            const len=20+(i*17%55), strength=.10+.13*(.5+.5*Math.sin(t*1.1+i*2.4));
            water.beginPath();water.moveTo(x,y);
            water.bezierCurveTo(x+len*.28,y-5+Math.sin(t+i)*2,x+len*.65,y+4,x+len,y-2);
            water.strokeStyle=`rgba(168,237,249,${strength})`;water.lineWidth=1+(i%3)*.3;water.stroke();
            if(i%4===0) {water.strokeStyle=`rgba(115,217,237,${strength*.45})`;water.lineWidth=5;water.stroke();}
        }
        water.restore();water.globalCompositeOperation='destination-in';water.drawImage(mask,0,0);water.globalCompositeOperation='source-over';
        ctx.clearRect(0,0,width,height);ctx.drawImage(surface,0,0);surf(t);
    }
    function active() { return visible && !document.hidden && stage.classList.contains('ocean-current') && !stage.classList.contains('terrain-loading') && Boolean(coasts[image.dataset.mapId]); }
    function tick(now) {
        frame=0;if(!active() || motion.matches)return;
        if(now-last>=1000/30) { const dt=last?Math.min((now-last)/1000,.1):0;last=now;time+=dt;draw(time); }
        frame=requestAnimationFrame(tick);
    }
    function sync() {
        if(frame)cancelAnimationFrame(frame);frame=0;last=0;
        if(!active()){ctx.clearRect(0,0,canvas.width,canvas.height);return;}
        if(motion.matches) {draw(2.5);return;}
        frame=requestAnimationFrame(tick);
    }
    new MutationObserver(sync).observe(stage,{attributes:true,attributeFilter:['class']});
    new ResizeObserver(()=>{builtFor='';sync();}).observe(stage);
    new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();},{rootMargin:'100px'}).observe(stage);
    image.addEventListener('load',()=>{builtFor='';sync();});
    document.addEventListener('visibilitychange',sync);motion.addEventListener('change',sync);
    window.addEventListener('pagehide',()=>{if(frame)cancelAnimationFrame(frame);});
    window.addEventListener('pageshow',sync);sync();
})();
