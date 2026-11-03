/* 从历史检测明细按类别浏览目标，不重复运行模型。 */
const galleryState = { page: 1, renderedPage: 1, requestId: 0, totalPages: 0, jumpTimer: null };

function galleryFilters() {
    return {
        category: $('#gallery-category').value,
        source: $('#gallery-source').value,
        min_confidence: $('#gallery-confidence').value,
        order: $('#gallery-order').value,
        keyword: $('#gallery-keyword').value.trim(),
        page: String(galleryState.page),
        page_size: '24',
    };
}

function updateCategories(categories, selected) {
    const select = $('#gallery-category');
    select.replaceChildren(new Option('全部类别', ''));
    categories.forEach((entry) => {
        select.add(new Option(`${entry.class_name}（${entry.count}）`, entry.class_name));
    });
    if (selected && !categories.some((entry) => entry.class_name === selected)) {
        select.add(new Option(selected, selected));
    }
    select.value = selected;
}

function galleryCard(item) {
    const id = Number(item.id);
    const recordId = Number(item.detection_id);
    const confidence = Number(item.confidence);
    const source = item.task_type === 'video' ? `视频采样 · 第 ${Number(item.frame_index)} 帧` : '图片检测';
    const label = escapeHtml(item.class_name || '未知类别');
    const file = escapeHtml(item.source_name || '未命名素材');
    const date = escapeHtml(item.created_at || '时间未知');
    const confidenceText = Number.isFinite(confidence) ? `${(confidence * 100).toFixed(1)}%` : '—';
    return `<article class="gallery-tile">
        <a class="gallery-picture" href="records.html?record_id=${recordId}" aria-label="查看 ${file} 的检测记录">
            <img src="/api/gallery/items/${id}/thumbnail" alt="${label}检测目标" loading="lazy">
            <span class="gallery-image-missing" hidden>原文件或对应帧不可用</span>
        </a>
        <div class="gallery-tile-body"><div class="gallery-tile-top"><strong>${label}</strong><span class="gallery-confidence">${confidenceText}</span></div>
            <p class="gallery-file" title="${file}">${file}</p><p class="gallery-meta">${source} · ${date}</p>
            <a class="gallery-record" href="records.html?record_id=${recordId}">查看来源记录 <span aria-hidden="true">→</span></a>
        </div></article>`;
}

// 输入暂停后跳页；保留输入框，避免异步结果返回时丢失焦点和正在输入的数字。
function cancelGalleryJump() {
    clearTimeout(galleryState.jumpTimer);
    galleryState.jumpTimer = null;
}

function renderGalleryPagination(data) {
    const container = $('#gallery-pagination');
    galleryState.totalPages = data.total_pages || 0;
    galleryState.renderedPage = data.page;
    if (galleryState.totalPages <= 1) {
        cancelGalleryJump();
        container.replaceChildren();
        return;
    }
    if (!$('#gallery-page-input')) {
        container.innerHTML = `<button type="button" class="btn btn-sm btn-secondary" data-step="-1">上一页</button>
            <span class="small muted" id="gallery-page-label"></span>
            <button type="button" class="btn btn-sm btn-secondary" data-step="1">下一页</button>
            <label class="gallery-page-jump" for="gallery-page-input">跳至 <input id="gallery-page-input" type="text" inputmode="numeric" autocomplete="off" aria-label="输入图库页码" aria-describedby="gallery-page-error"> 页</label>
            <span id="gallery-page-error" class="gallery-page-error" role="status" aria-live="polite"></span>`;
        const input = $('#gallery-page-input');
        const error = $('#gallery-page-error');
        const reset = () => { input.value = String(galleryState.page); input.removeAttribute('aria-invalid'); error.textContent = ''; };
        const commit = () => {
            cancelGalleryJump();
            const value = input.value.trim();
            if (!value) return;
            if (!/^-?\d+$/.test(value)) {
                input.setAttribute('aria-invalid', 'true');
                error.textContent = '请输入整数页码';
                return;
            }
            const target = Math.max(1, Math.min(galleryState.totalPages, Number(value)));
            input.value = String(target);
            input.removeAttribute('aria-invalid');
            error.textContent = '';
            if (target !== galleryState.page) { galleryState.page = target; loadGallery(); }
        };
        input.addEventListener('input', () => {
            cancelGalleryJump();
            input.removeAttribute('aria-invalid');
            error.textContent = '';
            if (input.value.trim()) galleryState.jumpTimer = setTimeout(commit, 650);
        });
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') { event.preventDefault(); commit(); }
        });
        input.addEventListener('blur', (event) => {
            cancelGalleryJump();
            // 点击翻页按钮时只执行按钮动作，避免失焦提交和按钮点击叠加。
            if (event.relatedTarget && event.relatedTarget.matches('[data-step]')) { reset(); return; }
            if (!/^-?\d+$/.test(input.value.trim())) reset(); else commit();
        });
        $$('#gallery-pagination [data-step]').forEach((button) => button.addEventListener('click', () => {
            cancelGalleryJump();
            galleryState.page += Number(button.dataset.step);
            reset();
            loadGallery();
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }));
    }
    const input = $('#gallery-page-input');
    if (document.activeElement !== input) input.value = String(data.page);
    $('#gallery-page-label').textContent = `第 ${data.page} / ${data.total_pages} 页`;
    $$('#gallery-pagination [data-step]').forEach((button) => {
        button.disabled = Number(button.dataset.step) < 0 ? data.page <= 1 : data.page >= data.total_pages;
    });
}

// 状态文字使用 textContent，接口错误不会作为 HTML 执行。
function showGalleryMessage(text, actionLabel, action) {
    const message = $('#gallery-message');
    const label = document.createElement('span');
    label.textContent = text;
    message.replaceChildren(label);
    message.hidden = false;
    const tip = document.createElement('span');
    tip.className = 'gallery-assistant-tip';
    const avatar = document.createElement('img');
    avatar.src = 'assets/whale-assistant.png';
    avatar.alt = '';
    avatar.setAttribute('aria-hidden', 'true');
    const tipText = document.createElement('span');
    tipText.textContent = actionLabel === '清空筛选'
        ? '小助手：可以调整或清空筛选条件。'
        : actionLabel === '重新加载'
            ? '小助手：服务恢复后可直接重试。'
            : '小助手：完成检测后，目标会出现在这里。';
    tip.append(avatar, tipText);
    message.appendChild(tip);
    if (actionLabel && action) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-sm btn-secondary gallery-message-action';
        button.textContent = actionLabel;
        button.addEventListener('click', action);
        message.appendChild(button);
    }
}

async function loadGallery() {
    const requestId = ++galleryState.requestId;
    const filters = galleryFilters();
    $('#gallery-message').textContent = '正在加载目标…';
    $('#gallery-message').hidden = false;
    $('#gallery-grid').setAttribute('aria-busy', 'true');
    try {
        const data = await API.get(`/api/gallery?${new URLSearchParams(filters)}`);
        if (requestId !== galleryState.requestId) return;
        if (data.total_pages && galleryState.page > data.total_pages) {
            galleryState.page = data.total_pages;
            return loadGallery();
        }
        updateCategories(data.categories || [], filters.category);
        $('#gallery-total').textContent = fmtNumber(data.total);
        $('#gallery-range').textContent = data.total ? `第 ${data.page} / ${data.total_pages} 页` : '';
        $('#gallery-message').hidden = data.items.length > 0;
        if (!data.items.length) {
            const filtered = Boolean(filters.category || filters.source || filters.keyword || Number(filters.min_confidence));
            if (filtered) {
                showGalleryMessage('没有符合筛选条件的目标。', '清空筛选', () => $('#gallery-reset').click());
            } else {
                showGalleryMessage('暂无检测目标，请先完成图片或视频检测。');
            }
        }
        $('#gallery-grid').innerHTML = data.items.map(galleryCard).join('');
        $('#gallery-grid').removeAttribute('aria-busy');
        $$('#gallery-grid img').forEach((img) => {
            const showMissing = () => { img.hidden = true; img.nextElementSibling.hidden = false; };
            img.addEventListener('error', showMissing);
            if (img.complete && !img.naturalWidth) showMissing();
        });
        renderGalleryPagination(data);
    } catch (error) {
        if (requestId !== galleryState.requestId) return;
        galleryState.page = galleryState.renderedPage;
        const input = $('#gallery-page-input');
        if (input && input.value === filters.page) input.value = String(galleryState.renderedPage);
        $('#gallery-grid').replaceChildren();
        $('#gallery-grid').removeAttribute('aria-busy');
        $('#gallery-total').textContent = '—';
        $('#gallery-range').textContent = '';
        showGalleryMessage(`加载失败：${error.message}。请检查服务后重试。`, '重新加载', () => {
            cancelGalleryJump();
            galleryState.page = Number(filters.page) || 1;
            loadGallery();
        });
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const apply = () => { cancelGalleryJump(); galleryState.page = 1; const input = $('#gallery-page-input'); if (input) { input.value = '1'; input.removeAttribute('aria-invalid'); $('#gallery-page-error').textContent = ''; } loadGallery(); };
    ['#gallery-category', '#gallery-source', '#gallery-confidence', '#gallery-order'].forEach((selector) => $(selector).addEventListener('change', apply));
    $('#gallery-apply').addEventListener('click', apply);
    $('#gallery-keyword').addEventListener('keydown', (event) => { if (event.key === 'Enter') apply(); });
    $('#gallery-reset').addEventListener('click', () => {
        ['#gallery-category', '#gallery-source', '#gallery-keyword'].forEach((selector) => { $(selector).value = ''; });
        $('#gallery-confidence').value = '0';
        $('#gallery-order').value = 'recent';
        apply();
    });
    loadGallery();
});
