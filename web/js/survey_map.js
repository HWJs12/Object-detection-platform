/* 虚拟地图上的站点是独立可编辑数据，背景不包含任何固定站点。 */
(() => {
    let stations = [], selected = null, placing = false, editing = null, deleting = null;
    let loading = false, saving = false, selectionRequest = 0, loadRequest = 0, drag = null;
    let maps = [], currentMap = 1, terrainLoading = false, terrainReady = true, formMap = 1;
    const queryMap = Number(new URLSearchParams(location.search).get('map_id'));
    try { currentMap = Number(localStorage.getItem('uwd-aquaculture-map')) || 1; } catch (_) {}
    if ([1,2,3].includes(queryMap)) currentMap = queryMap;
    if (![1,2,3].includes(currentMap)) currentMap = 1;
    const stage = document.getElementById('map-stage');
    const terrain = document.getElementById('map-terrain');
    const farmSwitcher = document.getElementById('farm-switcher');

    function controls() {
        const busy = loading || saving || terrainLoading || !terrainReady || !maps.length;
        $('#station-tool').disabled = busy;
        stage.setAttribute('aria-busy', String(loading || terrainLoading));
        farmSwitcher.querySelectorAll('button').forEach(button => { button.disabled = saving; });
    }

    function renderFarms() {
        farmSwitcher.innerHTML = maps.map(m => `<button type="button" class="farm-choice" data-map="${m.id}" aria-pressed="${m.id === currentMap}"><img src="${escapeHtml(m.image)}" alt="" draggable="false"><span><strong>${escapeHtml(m.name)}</strong><small>${escapeHtml(m.description.split(' · ')[0])}</small></span></button>`).join('');
        controls();
    }

    function applyMap() {
        const map = maps.find(m => m.id === currentMap);
        if (!map) return;
        $('#map-title').textContent = map.title;
        $('#map-description').textContent = map.description;
        document.title = `${map.title} - 水下海洋生物检测系统`;
        stage.setAttribute('aria-label', `${map.title}。可点击放置站点，也可在表单中设置位置。`);
        stage.classList.toggle('ocean-current', [1, 2, 3].includes(Number(map.id)));
        renderFarms();
        if (terrain.dataset.mapId === String(map.id)) return;
        terrain.dataset.mapId = String(map.id);
        terrain.alt = map.alt;
        terrainLoading = true; terrainReady = false;
        stage.classList.add('terrain-loading');
        const requestedMap = currentMap;
        terrain.onload = () => {
            if (currentMap !== requestedMap) return;
            terrainLoading = false; terrainReady = true;
            stage.classList.remove('terrain-loading'); controls(); render();
        };
        terrain.onerror = () => {
            if (currentMap !== requestedMap) return;
            terrainLoading = false; terrainReady = false; controls();
            $('#station-load-state').textContent = '地形图读取失败，请点击刷新后重试。';
            delete terrain.dataset.mapId;
        };
        terrain.src = map.image;
        if (terrain.complete && terrain.naturalWidth) terrain.onload();
        controls();
    }

    function switchMap(id) {
        if (saving || dialog.open || deleteDialog.open || !maps.some(m => m.id === id) || id === currentMap) return;
        currentMap = id; selected = null; selectionRequest++; drag = null;
        stations = []; setPlacing(false);
        $('#station-detail').innerHTML = '<h2 class="card-title">站点档案</h2><p class="muted">选择当前养殖所的一座调查站，查看记录与统计。</p>';
        try { localStorage.setItem('uwd-aquaculture-map', String(id)); } catch (_) {}
        const url = new URL(location.href); url.searchParams.set('map_id', id);
        history.replaceState(null, '', url);
        applyMap(); render(); load();
    }
    farmSwitcher.addEventListener('click', event => {
        const button = event.target.closest('[data-map]');
        if (button) switchMap(Number(button.dataset.map));
    });
    const dialog = document.getElementById('station-dialog');
    const form = document.getElementById('station-form');
    const deleteDialog = document.getElementById('station-delete-dialog');
    const point = (event) => {
        const r = stage.getBoundingClientRect();
        return { x: Math.max(0, Math.min(1, (event.clientX-r.left)/r.width)),
            y: Math.max(0, Math.min(1, (event.clientY-r.top)/r.height)) };
    };
    const payload = (s, patch = {}) => ({ name:s.name, x:s.x, y:s.y,
        depth_m:s.depth_m, note:s.note, map_id:s.map_id, active:s.active, version:s.version, ...patch });

    function setPlacing(value) {
        placing = value;
        stage.classList.toggle('placing', value);
        $('#map-cancel').hidden = !value;
        $('#map-instruction').textContent = value ? '点击地图放置调查站；也可在表单中调整位置' : '拖入调查站建筑，或点击“添加站点”后选择位置';
        $('#station-tool').setAttribute('aria-pressed', String(value));
    }

    function render() {
        $('#station-total').textContent = `站点 ${stations.filter(s=>s.active).length} 启用 / ${stations.length} 总计`;
        $('#map-empty').hidden = loading || terrainLoading || !terrainReady || stations.length > 0;
        $('#station-list').innerHTML = stations.map(s=>`<button type="button" class="station-list-item ${s.id===selected?'selected':''}" data-station="${s.id}"><span>${escapeHtml(s.name)}</span><small>${s.active?`${s.record_count} 条记录`:'已停用'}</small></button>`).join('');
        $('#station-layer').innerHTML = stations.map(s=>`<button type="button" class="station-marker ${s.id===selected?'selected':''} ${s.active?'':'inactive'} ${s.x<.1?'edge-left':''} ${s.x>.9?'edge-right':''} ${s.y<.08?'edge-top':''} ${s.y>.92?'edge-bottom':''}" style="left:${s.x*100}%;top:${s.y*100}%" data-station="${s.id}" aria-label="${escapeHtml(s.name)}${s.active?'':'，已停用'}，点击查看，拖动调整位置" title="${escapeHtml(s.name)}"><img src="assets/survey-station.svg" alt="" draggable="false"><span>${escapeHtml(s.name)}</span></button>`).join('');
    }

    async function load() {
        const request = ++loadRequest;
        loading = true; controls();
        $('#map-empty').hidden = true;
        $('#map-refresh').disabled = true;
        try {
            const data = await API.get(`/api/stations?map_id=${currentMap}`);
            if (request !== loadRequest) return;
            maps = data.maps;
            applyMap();
            stations = data.items;
            $('#station-load-state').textContent = stations.length ? '' : '当前养殖所尚无站点，拖入建筑即可开始。';
            render();
            if (selected && stations.some(s=>s.id===selected)) await select(selected);
            else if (selected) {
                selected=null; selectionRequest++;
                $('#station-detail').innerHTML='<h2 class="card-title">站点档案</h2><p class="muted">该站点已删除。请选择其他站点，或建立一个新站点。</p>';
                render();
            }
        } catch(err) { if(request===loadRequest) $('#station-load-state').textContent = '读取失败：'+err.message; }
        finally { if(request===loadRequest){loading=false; $('#map-refresh').disabled=false;controls();render();} }
    }

    async function select(id) {
        if (!stations.some(s => s.id === id && s.map_id === currentMap)) return;
        const request = ++selectionRequest;
        setPlacing(false);
        selected = id;
        render();
        $('#station-detail').innerHTML = '<h2 class="card-title">站点档案</h2><p role="status">正在读取…</p>';
        try {
            const [s, records] = await Promise.all([API.get(`/api/stations/${id}`),API.get(`/api/records?site_id=${id}&page_size=5`)]);
            if (request!==selectionRequest) return;
            const i=stations.findIndex(v=>v.id===id);
            if (i>=0) stations[i]={...s,record_count:s.summary.record_count};
            const n=s.summary;
            $('#station-detail').innerHTML = `<h2 class="card-title">站点档案</h2><h3>${escapeHtml(s.name)}</h3>
                <div class="station-detail-meta">${escapeHtml(maps.find(m=>m.id===s.map_id)?.name || '')} · 站点 #${s.id} · ${s.active?'启用中':'已停用'}${s.depth_m===null?'':` · 参考水深 ${s.depth_m} m`}</div>
                ${s.note?`<p class="station-note small">${escapeHtml(s.note)}</p>`:''}
                <div class="station-detail-actions"><button class="btn btn-sm btn-secondary" data-edit-station="${id}">编辑站点</button><button class="btn btn-sm btn-secondary" data-toggle-station="${id}">${s.active?'停用':'启用'}</button><button class="btn btn-sm btn-danger" data-delete-station="${id}">删除站点</button></div>
                ${s.active?`<div class="station-detail-actions"><a class="btn btn-sm" href="detect_image.html?site_id=${id}&map_id=${s.map_id}">图片检测</a><a class="btn btn-sm btn-secondary" href="detect_video.html?site_id=${id}&map_id=${s.map_id}">视频检测</a></div>`:'<p class="small muted">停用站点保留历史记录，启用后可继续检测。</p>'}
                <div class="station-summary"><div><strong>${n.image_count}</strong>图片记录</div><div><strong>${n.video_count}</strong>视频记录</div><div><strong>${n.image_objects}</strong>图片检出目标合计</div><div><strong>${n.video_maxn_sum}</strong>各视频单帧峰值之和</div></div>
                <p class="small muted">以上为检测统计，重复调查与视频之间可能重复计数，不代表海域实际生物个体数。</p>
                ${s.categories.length?`<div class="station-categories">${s.categories.map(c=>`<span>${escapeHtml(c.class_name)} · ${c.count}</span>`).join('')}</div><p class="small muted">类别数字为保存的类别检出计数。</p>`:''}
                <h4>最近检测</h4>${records.items.length?records.items.map(r=>`<a class="station-record" href="records.html?record_id=${r.id}">${escapeHtml(r.source_name)}<small>${r.task_type==='video'?'视频':'图片'} · ${escapeHtml(r.created_at)}</small></a>`).join(''):'<p class="muted small">尚无检测记录，选择上方入口开始调查。</p>'}
                <a class="station-record" href="records.html?site_id=${id}">查看该站全部记录 →</a>`;
        } catch(err) { if(request===selectionRequest) $('#station-detail').innerHTML=`<h2 class="card-title">站点档案</h2><p class="form-error">${escapeHtml(err.message)}</p>`; }
    }

    function openForm(location, station=null) {
        if (saving || loading || terrainLoading || !terrainReady) return;
        editing=station; formMap = station?.map_id || currentMap;
        $('#station-map-context').textContent = maps.find(m=>m.id===formMap)?.name || '当前养殖所';
        setPlacing(false);
        form.reset();
        $('#station-dialog-title').textContent=station?'编辑调查站':'建立调查站';
        $('#station-name').value=station?.name||'';
        $('#station-x').value=(location.x*100).toFixed(2);
        $('#station-y').value=(location.y*100).toFixed(2);
        $('#station-depth').value=station?.depth_m??'';
        $('#station-note').value=station?.note||'';
        $('#station-form-error').textContent='';
        dialog.showModal();
        $('#station-name').focus();
    }

    async function save(event) {
        event.preventDefault();
        if (saving || !form.reportValidity()) return;
        saving=true; controls(); $('#station-save').disabled=true;
        const body={map_id:formMap,name:$('#station-name').value.trim(),x:Number($('#station-x').value)/100,y:Number($('#station-y').value)/100,
            depth_m:$('#station-depth').value===''?null:Number($('#station-depth').value),note:$('#station-note').value.trim()};
        try {
            const s=editing?await API.putJSON(`/api/stations/${editing.id}`,payload(editing,body)):await API.postJSON('/api/stations',body);
            selected=s.id; dialog.close(); await load(); toast('站点已保存','success');
        } catch(err) { $('#station-form-error').textContent=err.message; }
        finally { saving=false; controls(); $('#station-save').disabled=false; }
    }

    async function move(s,location) {
        if(saving || loading || s.map_id !== currentMap) {render();return;}
        saving=true;controls();
        try { await API.putJSON(`/api/stations/${s.id}`,payload(s,location)); selected=s.id; await load(); toast('位置已更新','success'); }
        catch(err) {toast(err.message,'error');await load();render();}
        finally {saving=false;controls();}
    }

    stage.addEventListener('pointerdown',event=>{
        const el=event.target.closest('.station-marker');
        if(!el || saving || loading || terrainLoading || !terrainReady || event.button!==0) return;
        const s=stations.find(v=>v.id===Number(el.dataset.station));
        drag={id:event.pointerId,s,el,startX:event.clientX,startY:event.clientY,moved:false};
        el.setPointerCapture(event.pointerId);
    });
    stage.addEventListener('pointermove',event=>{
        if(!drag || drag.id!==event.pointerId) return;
        if(Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>5) drag.moved=true;
        if(drag.moved) {const p=point(event);drag.el.style.left=`${p.x*100}%`;drag.el.style.top=`${p.y*100}%`;}
    });
    stage.addEventListener('pointerup',event=>{
        if(!drag || drag.id!==event.pointerId) return;
        const previous=drag;drag=null;
        if(previous.moved) {event.preventDefault();move(previous.s,point(event));}
        else select(previous.s.id);
    });
    stage.addEventListener('pointercancel',()=>{drag=null;render();});
    stage.addEventListener('click',event=>{
        if(event.target.closest('.station-marker')) {if(event.detail===0) select(Number(event.target.closest('.station-marker').dataset.station));return;}
        if(placing && !saving && !loading && terrainReady) openForm(point(event));
    });
    $('#station-tool').addEventListener('dragstart',event=>{
        if(saving || loading || terrainLoading || !terrainReady){event.preventDefault();return;}
        event.dataTransfer.setData('application/x-survey-station','new');event.dataTransfer.effectAllowed='copy';
    });
    stage.addEventListener('dragover',event=>{if([...event.dataTransfer.types].includes('application/x-survey-station')) {event.preventDefault();event.dataTransfer.dropEffect='copy';}});
    stage.addEventListener('drop',event=>{if(event.dataTransfer.getData('application/x-survey-station')==='new'){event.preventDefault();if(!saving && !loading && terrainReady)openForm(point(event));}});
    $('#station-tool').addEventListener('click',()=>{if(!saving && !loading && terrainReady){setPlacing(!placing);if(placing && innerWidth<850)stage.scrollIntoView({block:'center'});}});
    $('#map-cancel').addEventListener('click',()=>setPlacing(false));
    stage.addEventListener('keydown',event=>{if(event.key==='Escape')setPlacing(false);else if(placing && event.target===stage && event.key==='Enter'){event.preventDefault();openForm({x:.5,y:.5});}});
    $('#station-list').addEventListener('click',event=>{const el=event.target.closest('[data-station]');if(el)select(Number(el.dataset.station));});
    $('#station-detail').addEventListener('click',async event=>{
        const edit=event.target.closest('[data-edit-station]'), toggle=event.target.closest('[data-toggle-station]'), remove=event.target.closest('[data-delete-station]');
        const id=Number(edit?.dataset.editStation||toggle?.dataset.toggleStation||remove?.dataset.deleteStation),s=stations.find(v=>v.id===id);
        if(!s || saving)return;
        if(edit){openForm(s,s);return;}
        if(remove){
            deleting={...s};
            setPlacing(false);
            $('#station-delete-description').textContent=`即将删除“${s.name}”。该站点当前有 ${s.record_count} 条检测记录。`;
            $('#station-delete-error').textContent='';
            deleteDialog.showModal();
            $('#station-delete-cancel').focus();
            return;
        }
        if(toggle){
            saving=true;controls();toggle.disabled=true;
            try {await API.putJSON(`/api/stations/${s.id}`,payload(s,{active:!s.active}));await load();toast(s.active?'站点已停用':'站点已启用','success');}
            catch(err){toast(err.message,'error');await load();}
            finally{saving=false;controls();toggle.disabled=false;}
        }
    });
    $('#station-delete-form').addEventListener('submit',async event=>{
        event.preventDefault();
        if(saving || !deleting)return;
        const target={...deleting};
        saving=true;controls();
        $('#station-delete-confirm').disabled=true;
        $('#station-delete-cancel').disabled=true;
        $('#station-delete-error').textContent='';
        try {
            await API.del(`/api/stations/${target.id}?version=${target.version}`);
            if(selected===target.id)selected=null;
            selectionRequest++;
            deleting=null;deleteDialog.close();
            $('#station-detail').innerHTML='<h2 class="card-title">站点档案</h2><p class="muted">站点已删除，已有检测记录保留在检测记录页面。</p>';
            await load();toast('站点已删除，已有检测记录已保留','success');
        } catch(err) {$('#station-delete-error').textContent=err.message;}
        finally {saving=false;controls();$('#station-delete-confirm').disabled=false;$('#station-delete-cancel').disabled=false;}
    });
    $('#station-delete-cancel').addEventListener('click',()=>{if(!saving){deleting=null;deleteDialog.close();}});
    deleteDialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();else deleting=null;});
    form.addEventListener('submit',save);
    $('#station-dialog-cancel').addEventListener('click',()=>{if(!saving)dialog.close();});
    dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
    $('#map-refresh').addEventListener('click',load);
    load();
})();
