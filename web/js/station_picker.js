/* 养殖所与站点联动；检测仍使用全局稳定的站点编号。 */
function requireSurveyStation() {
    const select = document.getElementById('survey-site');
    const farm = document.getElementById('survey-map');
    const option = select?.selectedOptions?.[0];
    if (!select || select.disabled || !select.value || (farm && option?.dataset.mapId !== farm.value)) {
        toast('请先选择养殖所及其中一个启用中的站点','error');
        select?.focus();
        return false;
    }
    return true;
}

document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('survey-site');
    const farm = document.getElementById('survey-map');
    if (!select || !farm) return;
    const hint = document.getElementById('station-picker-status');
    const refresh = document.getElementById('station-picker-refresh');
    const depth = document.getElementById('survey-depth');
    const mapLink = document.getElementById('station-map-link');
    const query = new URLSearchParams(location.search);
    let request = 0, options = [], maps = [];
    let linked = query.get('site_id');
    let initialMap = Number(query.get('map_id'));
    if (![1,2,3].includes(initialMap)) {
        try { initialMap = Number(localStorage.getItem('uwd-aquaculture-map')); } catch (_) {}
    }
    if (![1,2,3].includes(initialMap)) initialMap = 1;

    function fillDepth() {
        const s = options.find(v => String(v.id) === select.value);
        if (depth && (depth.value === '' || depth.value === depth.dataset.stationDepth)) {
            depth.value = s?.depth_m ?? '';
            depth.dataset.stationDepth = depth.value;
        }
    }

    function fillStations(previous = '') {
        const mapId = Number(farm.value);
        const available = options.filter(s => s.map_id === mapId);
        select.replaceChildren(new Option(available.length ? '请选择调查站点' : '该养殖所暂无启用站点', ''));
        available.forEach(s => {
            const option = new Option(s.name, String(s.id));
            option.dataset.mapId = String(s.map_id);
            select.add(option);
        });
        select.value = available.some(s => String(s.id) === previous) ? previous : '';
        const name = maps.find(m => m.id === mapId)?.name || '当前养殖所';
        hint.textContent = available.length ? `${name}：请选择站点，检测结果将归入所选站点。` : `${name}尚无启用站点，请前往地形图建立站点。`;
        if (previous && !select.value) hint.textContent = '此前选择的站点已停用或不存在，请重新选择。';
        if (mapLink) mapLink.href = `survey_map.html?map_id=${mapId}`;
        fillDepth();
    }

    async function load() {
        if (select.dataset.locked === '1') return;
        const token = ++request, previous = select.value || linked;
        const previousMap = maps.length ? (Number(farm.value) || initialMap) : initialMap;
        select.disabled = farm.disabled = refresh.disabled = true;
        hint.textContent = '正在读取养殖所与站点…';
        try {
            const data = await API.get('/api/stations?active_only=true');
            if (token !== request) return;
            options = data.items;
            maps = data.maps;
            const linkedStation = linked ? options.find(s => String(s.id) === linked) : null;
            const mapId = linkedStation?.map_id || previousMap;
            farm.replaceChildren(...maps.map(m => new Option(m.name, String(m.id))));
            farm.value = String(maps.some(m => m.id === mapId) ? mapId : 1);
            fillStations(previous);
            linked = null;
        } catch (err) {
            options = [];
            select.replaceChildren(new Option('站点读取失败',''));
            hint.textContent = err.message + '，请点击刷新站点。';
        } finally {
            if (token === request) {
                select.disabled = select.dataset.locked === '1';
                farm.disabled = farm.dataset.locked === '1';
                refresh.disabled = false;
            }
        }
    }
    function rememberSelection() {
        const url = new URL(location.href);
        url.searchParams.set('map_id', farm.value);
        if (select.value) url.searchParams.set('site_id', select.value);
        else url.searchParams.delete('site_id');
        history.replaceState(null, '', url);
    }
    select.addEventListener('change', () => { fillDepth(); rememberSelection(); });
    farm.addEventListener('change', () => {
        if (farm.dataset.locked === '1') return;
        try { localStorage.setItem('uwd-aquaculture-map', farm.value); } catch (_) {}
        fillStations(); rememberSelection();
    });
    refresh.addEventListener('click', load);
    load();
});
