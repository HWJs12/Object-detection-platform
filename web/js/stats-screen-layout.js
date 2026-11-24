/* 将现有统计区块重新排列，数据、事件和口径均沿用原页面。 */
(() => {
    const page = document.querySelector('.stats-page');
    const metrics = page && page.querySelector(':scope > .grid-4');
    const cardOf = (selector) => page?.querySelector(selector)?.closest('.card');
    const trend = cardOf('#chart-bar');
    const category = cardOf('#chart-pie');
    const sites = cardOf('#sites-list');
    const recent = cardOf('#recent-body');
    if (!metrics || !trend || !category || !sites || !recent) return;
    const originalCategoryGroup = category.parentElement;
    const board = document.createElement('section');
    board.className = 'stats-screen';
    board.setAttribute('aria-label', '检测数据总览');
    const heading = document.createElement('div');
    heading.className = 'stats-screen-heading';
    heading.innerHTML = '<div><span>MARINE OBSERVATION</span><h2>海洋生物检测数据总览</h2></div><p>基于已有检测记录 · 各图表范围见标题或筛选</p>';
    const grid = document.createElement('div');
    grid.className = 'stats-screen-grid';
    [[trend, 'stats-screen-trend'], [category, 'stats-screen-category'], [sites, 'stats-screen-sites'], [recent, 'stats-screen-recent']].forEach(([card, cls]) => {
        card.classList.add(cls);
        grid.appendChild(card);
    });
    if (originalCategoryGroup?.classList.contains('grid-2')) originalCategoryGroup.classList.add('stats-single-chart-grid');
    sites.querySelector('#sites-list').setAttribute('tabindex', '0');
    board.append(heading, grid);
    metrics.after(board);
    const title = document.createElement('div');
    title.className = 'stats-detail-heading';
    title.innerHTML = '<span>DETAILED ANALYSIS</span><h2>详细分析</h2><p>查看时间窗口、置信度、类别分布及统计口径</p>';
    board.after(title);
})();
