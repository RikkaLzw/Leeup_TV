(function () {
  const filters = document.getElementById("recommendFilters");
  if (!filters) return;

  const sections = Array.from(document.querySelectorAll(".recommend-section"));
  const hasLevel1Filter = Boolean(filters.querySelector('[data-filter-level="level1"]'));
  const state = { level1: "all", level2: "all" };
  const pagedSections = new Map();
  const pageCache = new Map();

  function sectionMatches(section, patch) {
    const next = { ...state, ...patch };
    return ["level1", "level2"].every((level) => {
      return next[level] === "all" || section.dataset[level] === next[level];
    });
  }

  function valuesFor(level) {
    const priorPatch = { level1: "all", level2: "all" };
    if (level === "level2") priorPatch.level1 = state.level1;
    const values = sections
      .filter((section) => sectionMatches(section, priorPatch))
      .map((section) => section.dataset[level] || "")
      .filter(Boolean);
    return Array.from(new Set(values));
  }

  function renderLevel(level) {
    const row = filters.querySelector(`[data-filter-level="${level}"]`);
    const tabs = row?.querySelector(".filter-tabs");
    if (!tabs) return;
    if (level === "level2" && hasLevel1Filter && state.level1 === "all") {
      row.hidden = true;
      state.level2 = "all";
      return;
    }
    row.hidden = false;
    const values = valuesFor(level);
    if (state[level] !== "all" && !values.includes(state[level])) {
      state[level] = "all";
    }
    tabs.innerHTML = "";
    tabs.appendChild(createButton(level, "all", "全部"));
    for (const value of values) {
      tabs.appendChild(createButton(level, value, value));
    }
  }

  function createButton(level, value, label) {
    const button = document.createElement("button");
    button.className = `filter-tab${state[level] === value ? " active" : ""}`;
    button.type = "button";
    button.dataset.filterLevel = level;
    button.dataset.filterValue = value;
    button.textContent = label;
    return button;
  }

  function applyFilters() {
    if (hasLevel1Filter) renderLevel("level1");
    renderLevel("level2");
    const shouldPage = state.level2 !== "all";
    sections.forEach((section) => {
      section.hidden = !sectionMatches(section, {});
      if (!section.hidden) {
        if (shouldPage) {
          renderPagination(section, 1, true);
        } else {
          renderPreview(section);
        }
      } else {
        resetPagination(section);
      }
    });
  }

  function renderPreview(section) {
    const grid = section.querySelector(".paged-grid");
    const pager = section.querySelector(".pagination");
    if (!grid) return;
    const previewSize = Math.max(Number(grid.dataset.previewSize || grid.dataset.pageSize || 12), 1);
    const items = Array.from(grid.querySelectorAll(".paged-item"));
    items.forEach((item, index) => {
      item.hidden = index >= previewSize;
    });
    if (pager) {
      pager.hidden = true;
      pager.innerHTML = "";
    }
    pagedSections.delete(section);
  }

  async function renderPagination(section, page, resetGrid = false) {
    const grid = section.querySelector(".paged-grid");
    const pager = section.querySelector(".pagination");
    if (!grid || !pager) return;

    const items = Array.from(grid.querySelectorAll(".paged-item"));
    const pageSize = Math.max(Number(grid.dataset.pageSize || 12), 1);
    const nextPage = Math.max(page || 1, 1);
    if (resetGrid || pageCache.get(cacheKey(section, nextPage)) || nextPage > Math.ceil(items.length / pageSize)) {
      await loadPage(section, nextPage, pageSize);
    }

    const latestItems = Array.from(grid.querySelectorAll(".paged-item"));
    const hasMore = section.dataset.hasMore === "1";
    pagedSections.set(section, nextPage);

    latestItems.forEach((item, index) => {
      item.hidden = index < (nextPage - 1) * pageSize || index >= nextPage * pageSize;
    });

    if (nextPage <= 1 && !hasMore && latestItems.length <= pageSize) {
      pager.hidden = true;
      pager.innerHTML = "";
      return;
    }

    pager.hidden = false;
    pager.innerHTML = `
      <button class="pagination-button" type="button" data-page-action="prev" ${nextPage <= 1 ? "disabled" : ""}>上一页</button>
      <span>第 ${nextPage} 页</span>
      <button class="pagination-button" type="button" data-page-action="next" ${!hasMore ? "disabled" : ""}>下一页</button>
    `;
  }

  async function loadPage(section, page, pageSize) {
    const key = cacheKey(section, page);
    if (pageCache.has(key)) {
      applyLoadedPage(section, page, pageSize, pageCache.get(key));
      return true;
    }
    setPagerLoading(section, true);
    try {
      const res = await fetch("/api/recommend-page", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          level1: section.dataset.level1 || "",
          level2: section.dataset.level2 || "",
          page,
          page_size: pageSize
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "加载失败");
      pageCache.set(key, data);
      applyLoadedPage(section, page, pageSize, data);
      return true;
    } catch {
      section.dataset.hasMore = "0";
      return false;
    } finally {
      setPagerLoading(section, false);
    }
  }

  function applyLoadedPage(section, page, pageSize, data) {
    const grid = section.querySelector(".paged-grid");
    if (!grid) return;
    const start = (page - 1) * pageSize;
    const existing = Array.from(grid.querySelectorAll(".paged-item"));
    for (let index = existing.length; index < start; index += 1) {
      const placeholder = document.createElement("div");
      placeholder.className = "paged-item";
      placeholder.hidden = true;
      grid.appendChild(placeholder);
    }
    Array.from(grid.querySelectorAll(".paged-item")).slice(start, start + pageSize).forEach((node) => node.remove());
    const fragment = document.createDocumentFragment();
    for (const item of data.items || []) {
      fragment.appendChild(renderItem(item));
    }
    const anchor = grid.children[start] || null;
    grid.insertBefore(fragment, anchor);
    section.dataset.hasMore = data.has_more ? "1" : "0";
  }

  function renderItem(item) {
    const wrapper = document.createElement("div");
    wrapper.className = "paged-item";
    const title = escapeHtml(item.title || "");
    const year = escapeHtml(item.year || "");
    const kindLabel = item.kind === "movie" ? "电影" : item.kind ? "剧集" : "";
    const poster = item.poster || "";
    const rawPoster = item.raw_poster || "";
    const resolveHref = `/resolve?title=${encodeURIComponent(item.title || "")}&year=${encodeURIComponent(item.year || "")}&douban_id=${encodeURIComponent(item.id || "")}&kind=${encodeURIComponent(item.kind || "")}`;
    wrapper.innerHTML = `
      <article class="media-card">
        <a class="poster" href="${resolveHref}">
          ${poster ? `<img src="${escapeAttr(poster)}" alt="${escapeAttr(item.title || "")}" loading="lazy" referrerpolicy="no-referrer" data-raw-poster="${escapeAttr(rawPoster)}" onerror="window.RikkaImages ? window.RikkaImages.retryDouban(this) : (this.remove(), this.parentElement.classList.add('poster-missing'));">` : ""}
          <span class="poster-fallback">${escapeHtml((item.title || "?").slice(0, 4))}</span>
          ${item.rate ? `<span class="rating-badge">${escapeHtml(item.rate)}</span>` : ""}
        </a>
        <div class="card-body">
          <a class="card-title" href="${resolveHref}">${title}</a>
          <div class="card-meta">
            ${year ? `<span>${year}</span>` : ""}
            ${kindLabel ? `<span>${kindLabel}</span>` : ""}
            <span>豆瓣</span>
          </div>
        </div>
      </article>
    `;
    return wrapper;
  }

  function setPagerLoading(section, loading) {
    const pager = section.querySelector(".pagination");
    if (!pager) return;
    pager.classList.toggle("loading", loading);
  }

  function cacheKey(section, page) {
    return `${section.dataset.level1 || ""}|${section.dataset.level2 || ""}|${page}`;
  }

  function resetPagination(section) {
    const items = section.querySelectorAll(".paged-item");
    const pager = section.querySelector(".pagination");
    items.forEach((item) => {
      item.hidden = false;
    });
    if (pager) {
      pager.hidden = true;
      pager.innerHTML = "";
    }
    pagedSections.delete(section);
  }

  filters.addEventListener("click", (event) => {
    const button = event.target.closest(".filter-tab");
    if (!button) return;
    const level = button.dataset.filterLevel;
    if (!level) return;
    state[level] = button.dataset.filterValue || "all";
    if (level === "level1") {
      state.level2 = "all";
    }
    applyFilters();
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".pagination-button");
    if (!button) return;
    const section = button.closest(".recommend-section");
    if (!section) return;
    const current = pagedSections.get(section) || 1;
    const delta = button.dataset.pageAction === "prev" ? -1 : 1;
    renderPagination(section, current + delta);
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[char]);
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  applyFilters();
})();
