(function () {
  const filters = document.getElementById("recommendFilters");
  if (!filters) return;

  const sections = Array.from(document.querySelectorAll(".recommend-section"));
  const hasLevel1Filter = Boolean(filters.querySelector('[data-filter-level="level1"]'));
  const defaultPagination = filters.dataset.defaultPagination || "";
  const state = { level1: "all", level2: "all", region: "all" };
  const pagedSections = new Map();
  const pageCache = new Map();
  sections.forEach((section) => {
    const title = section.querySelector(".section-title h2");
    if (title) section.dataset.originalTitle = title.textContent || "";
  });

  function sectionMatches(section, patch) {
    const next = { ...state, ...patch };
    if (next.level1 !== "all" && section.dataset.level1 !== next.level1) return false;
    if (next.level2 !== "all") {
      if (next.region !== "all") return false;
      return section.dataset.filterGroup !== "region" && section.dataset.level2 === next.level2;
    }
    if (next.region !== "all") {
      return section.dataset.filterGroup === "region" && regionValue(section) === next.region;
    }
    return true;
  }

  function baseMatches(section, patch = {}) {
    const next = { ...state, ...patch };
    return ["level1"].every((level) => {
      return next[level] === "all" || section.dataset[level] === next[level];
    });
  }

  function valuesFor(level) {
    const priorPatch = { level1: "all" };
    if (level === "level2" || level === "region") priorPatch.level1 = state.level1;
    const values = sections
      .filter((section) => baseMatches(section, priorPatch))
      .filter((section) => {
        if (level === "level2") return section.dataset.filterGroup === "category";
        if (level === "region") return section.dataset.filterGroup === "region";
        return true;
      })
      .map((section) => ({
        value: filterValue(section, level),
        label: level === "level1"
          ? section.dataset.level1 || ""
          : section.dataset.filterLabel || section.dataset.level2 || ""
      }))
      .filter((item) => item.value && item.label);
    const seen = new Set();
    return values.filter((item) => {
      if (seen.has(item.value)) return false;
      seen.add(item.value);
      return true;
    });
  }

  function renderLevel(level) {
    const row = filters.querySelector(`[data-filter-level="${level}"]`);
    const tabs = row?.querySelector(".filter-tabs");
    if (!tabs) return;
    if ((level === "level2" || level === "region") && hasLevel1Filter && state.level1 === "all") {
      row.hidden = true;
      state[level] = "all";
      return;
    }
    row.hidden = false;
    const values = valuesFor(level);
    if (state[level] !== "all" && !values.some((item) => item.value === state[level])) {
      state[level] = "all";
    }
    tabs.innerHTML = "";
    tabs.appendChild(createButton(level, "all", "全部"));
    for (const item of values) {
      tabs.appendChild(createButton(level, item.value, item.label));
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
    renderLevel("region");
    resetComboSections();
    const shouldPage = shouldUsePagination();
    const comboSection = findRequestSection();
    sections.forEach((section) => {
      if (section.classList.contains("filter-only-section")) {
        section.hidden = true;
        resetPagination(section);
        return;
      }
      section.hidden = Boolean(comboSection) || !sectionMatches(section, {});
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
    if (comboSection) {
      renderRequestSection(comboSection);
    }
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
      const loaded = await loadPage(section, nextPage, pageSize);
      if (!loaded && nextPage > 1) {
        return renderPagination(section, nextPage - 1);
      }
    }

    const latestItems = Array.from(grid.querySelectorAll(".paged-item"));
    const hasMore = section.dataset.hasMore === "1" || nextPage * pageSize < latestItems.length;
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
          level2: section.dataset.requestLevel2 || section.dataset.level2 || "",
          region: section.dataset.requestRegion || "",
          page,
          page_size: pageSize
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "加载失败");
      pageCache.set(key, data);
      applyLoadedPage(section, page, pageSize, data);
      return page <= 1 || Boolean((data.items || []).length);
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
    const resolveHref = `/resolve?title=${encodeURIComponent(item.title || "")}&year=${encodeURIComponent(item.year || "")}&douban_id=${encodeURIComponent(item.id || "")}&kind=${encodeURIComponent(item.kind || "")}&poster=${encodeURIComponent(poster)}&raw_poster=${encodeURIComponent(rawPoster)}`;
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
    return `${section.dataset.level1 || ""}|${section.dataset.requestLevel2 || section.dataset.level2 || ""}|${section.dataset.requestRegion || ""}|${page}`;
  }

  function shouldUsePagination() {
    if (state.level2 !== "all" || state.region !== "all") return true;
    if (defaultPagination === "always") return true;
    return defaultPagination === "type" && state.level1 !== "all";
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

  function resetComboSections() {
    sections.forEach((section) => {
      delete section.dataset.requestLevel2;
      delete section.dataset.requestRegion;
      const title = section.querySelector(".section-title h2");
      if (title && section.dataset.originalTitle) title.textContent = section.dataset.originalTitle;
    });
  }

  filters.addEventListener("click", (event) => {
    const button = event.target.closest(".filter-tab");
    if (!button) return;
    const level = button.dataset.filterLevel;
    if (!level) return;
    state[level] = button.dataset.filterValue || "all";
    if (level === "level1") {
      state.level2 = "all";
      state.region = "all";
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

  function findRequestSection() {
    if (state.level2 === "all" && state.region === "all") return null;
    if (state.level2 !== "all") {
      return sections.find((section) => (
        (state.level1 === "all" || section.dataset.level1 === state.level1)
        && section.dataset.filterGroup === "category"
        && section.dataset.level2 === state.level2
        && !section.classList.contains("filter-only-section")
      )) || findVisibleCarrierSection();
    }
    return sections.find((section) => (
      (state.level1 === "all" || section.dataset.level1 === state.level1)
      && section.dataset.filterGroup === "region"
      && regionValue(section) === state.region
      && !section.classList.contains("filter-only-section")
    )) || findVisibleCarrierSection();
  }

  function findVisibleCarrierSection() {
    return sections.find((section) => (
      (state.level1 === "all" || section.dataset.level1 === state.level1)
      && !section.classList.contains("filter-only-section")
      && section.querySelector(".paged-grid")
      && section.querySelector(".pagination")
    )) || null;
  }

  function renderRequestSection(section) {
    const regionLabel = state.region === "all" ? "" : filterLabel("region", state.region);
    const categoryLabel = state.level2 === "all" ? "" : filterLabel("level2", state.level2);
    section.hidden = false;
    if (state.level2 !== "all") section.dataset.requestLevel2 = state.level2;
    if (state.region !== "all") section.dataset.requestRegion = state.region;
    const title = section.querySelector(".section-title h2");
    if (title) title.textContent = `${regionLabel}${categoryLabel}` || section.dataset.originalTitle || "推荐";
    renderPagination(section, 1, true);
  }

  function filterLabel(level, value) {
    const button = filters.querySelector(`[data-filter-level="${level}"][data-filter-value="${cssEscape(value)}"]`);
    return button?.textContent || value;
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function filterValue(section, level) {
    if (level === "level1") return section.dataset.level1 || "";
    if (level === "region") return regionValue(section);
    return section.dataset.level2 || "";
  }

  function regionValue(section) {
    return section.dataset.region || section.dataset.filterLabel || section.dataset.level2 || "";
  }

  applyFilters();
})();
