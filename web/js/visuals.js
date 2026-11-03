/* 仅为现有操作添加装饰性图形，不改变文字、数据和事件。 */
(function () {
  const NS='http://www.w3.org/2000/svg';
  function icon(name, cls) {
    const svg=document.createElementNS(NS,'svg'); svg.setAttribute('class',cls); svg.setAttribute('aria-hidden','true'); svg.setAttribute('focusable','false'); svg.setAttribute('viewBox','0 0 24 24');
    const use=document.createElementNS(NS,'use'); use.setAttribute('href',`assets/ocean-symbols.svg#${name}`); svg.appendChild(use); return svg;
  }
  function add(el,name,cls) { if (!el || el.querySelector(':scope > .'+cls)) return; el.prepend(icon(name,cls)); }
  document.addEventListener('DOMContentLoaded',()=>{
    const nav={'index.html':'home','detect_image.html':'image','detect_video.html':'video','survey_map.html':'station','records.html':'records','gallery.html':'gallery','stats.html':'stats','system.html':'system','api_docs.html':'api'};
    document.querySelectorAll('.navbar nav a').forEach(a=>add(a,nav[a.getAttribute('href')]||'home','nav-visual'));
    document.querySelectorAll('.stat-card .label').forEach(label=>{
      const t=label.textContent; let key='stats';
      if(/接口分组/.test(t)) key='type'; else if(/接口|响应格式/.test(t)) key='api'; else if(/服务地址/.test(t)) key='system'; else if(/类别/.test(t)) key='type'; else if(/时间|耗时|时段/.test(t)) key='clock'; else if(/目标|检出/.test(t)) key='count'; else if(/存储|空间|磁盘/.test(t)) key='storage'; else if(/任务|记录|检测次数/.test(t)) key='records'; else if(/状态/.test(t)) key='system';
      add(label,key,'metric-visual');
    });
    document.querySelectorAll('.detection-step').forEach((step,i)=>add(step.querySelector('h3'),['station',location.pathname.includes('video')?'video':'image','settings'][i]||'settings','step-visual'));
    document.querySelectorAll('.upload-area .icon').forEach(el=>{ el.replaceChildren(icon('upload','upload-visual')); });
    document.querySelectorAll('.gallery-filter .field label, #filter-type, #filter-site').forEach(el => {
      if (el.tagName !== 'LABEL') return;
      const t=el.textContent; add(el,/类别/.test(t)?'type':/名称|文件/.test(t)?'search':/置信度/.test(t)?'settings':'filter','field-visual');
    });
    if (location.pathname.endsWith('records.html')) document.querySelectorAll('.field label').forEach(el=>add(el,/日期/.test(el.textContent)?'clock':/名称|文件/.test(el.textContent)?'search':'filter','field-visual'));
    document.querySelectorAll('.op-card h4').forEach(el=>add(el,/清理|回收/.test(el.textContent)?'cleanup':'storage','op-visual'));
    document.querySelectorAll('.survey-sidebar .card-title').forEach((el,i)=>add(el,i?'records':'station','section-visual'));
    const groups=document.querySelector('#group-nav');
    if (groups) {
      const decorate=()=>groups.querySelectorAll('a').forEach(el=>{const t=el.textContent;add(el,/视频/.test(t)?'video':/图片|检测/.test(t)?'image':/统计/.test(t)?'stats':/站点|地图/.test(t)?'station':/记录|报告/.test(t)?'records':'api','field-visual');});
      new MutationObserver(decorate).observe(groups,{childList:true}); decorate();
    }
    const api=document.querySelector('.container #sample-curl');if(api){const h=api.closest('.card')?.querySelector('.card-title');add(h,'api','section-visual');}
  });
})();
