(function () {
  const STORAGE_KEY = "rikka_continue_records";
  const section = document.getElementById("continueSection");
  const grid = document.getElementById("continueGrid");
  const clearButton = document.getElementById("clearContinueButton");

  if (!section || !grid) return;

  function readRecords() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      const value = raw ? JSON.parse(raw) : [];
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  }

  function writeRecords(records) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(records.slice(0, 30)));
  }

  function normalizeTitle(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[\s\-_·:：,，.。!！?？()[\]（）【】]+/g, "");
  }

  function recordIdentity(record) {
    return normalizeTitle(record.search_title || record.title || `${record.source || ""}+${record.id || ""}`);
  }

  function recordKey(record) {
    return `${record.source || ""}+${record.id || ""}`;
  }

  function applyPosterMap(record) {
    const poster = (window.RIKKA_POSTER_MAP || {})[recordIdentity(record)];
    if (!poster || !poster.poster) return record;
    if (poster.poster_source === "douban" || record.poster_source !== "douban") {
      return {
        ...record,
        cover: poster.poster,
        poster: poster.poster,
        raw_poster: poster.raw_poster || record.raw_poster || "",
        source_poster: record.source_poster || poster.source_poster || "",
        poster_source: poster.poster_source || record.poster_source || ""
      };
    }
    return record;
  }

  function mergeRecords(records) {
    const grouped = new Map();
    for (const record of records
      .filter((item) => item && item.source && item.id && item.title)
      .map(applyPosterMap)
      .sort((a, b) => (b.save_time || 0) - (a.save_time || 0))) {
      const identity = recordIdentity(record) || recordKey(record);
      const current = grouped.get(identity);
      if (!current) {
        grouped.set(identity, { ...record, _identity: identity });
        continue;
      }
      const currentCover = current.cover || current.poster || "";
      const recordCover = record.cover || record.poster || "";
      if ((record.poster_source === "douban" && current.poster_source !== "douban") || (!currentCover && recordCover)) {
        current.cover = recordCover;
        current.poster = recordCover;
        current.raw_poster = record.raw_poster || current.raw_poster || "";
        current.source_poster = record.source_poster || current.source_poster || "";
        current.poster_source = record.poster_source || current.poster_source || "";
      }
      if (!current.year && record.year) current.year = record.year;
      if (!current.total_episodes && record.total_episodes) current.total_episodes = record.total_episodes;
    }
    return Array.from(grouped.values()).sort((a, b) => (b.save_time || 0) - (a.save_time || 0));
  }

  function formatPercent(record) {
    if (!record.total_time) return "";
    const value = Math.max(0, Math.min(99, Math.round((record.play_time || 0) / record.total_time * 100)));
    return `${value}%`;
  }

  function buildCard(record) {
    const episode = Number(record.episode_index || 0);
    const href = `/play/${encodeURIComponent(record.source)}/${encodeURIComponent(record.id)}?episode=${episode}&prefer=1`;
    const cover = record.cover || record.poster || "";
    const percent = formatPercent(record);
    const article = document.createElement("article");
    article.className = "media-card local-continue-card";
    article.dataset.continueKey = record._identity || recordIdentity(record) || recordKey(record);
    article.innerHTML = `
      <a class="poster" href="${href}">
        ${cover ? `<img src="${escapeAttr(cover)}" alt="${escapeAttr(record.title || "")}" loading="lazy" data-raw-poster="${escapeAttr(record.raw_poster || "")}" data-source-poster="${escapeAttr(record.source_poster || "")}" onerror="window.RikkaImages ? window.RikkaImages.retryPoster(this) : (this.remove(), this.parentElement.classList.add('poster-missing'));"><span class="poster-fallback">${escapeHtml((record.title || "?").slice(0, 4))}</span>` : `<span class="poster-fallback">${escapeHtml((record.title || "?").slice(0, 4))}</span>`}
        ${percent ? `<span class="progress-badge">${percent}</span>` : ""}
      </a>
      <div class="card-body">
        <a class="card-title" href="${href}">${escapeHtml(record.title || "未命名")}</a>
        <div class="card-meta">
          <span>第 ${episode + 1} 集</span>
          ${record.source_name ? `<span>${escapeHtml(record.source_name)}</span>` : ""}
        </div>
      </div>
    `;
    return article;
  }

  function renderLocalRecords() {
    const existingKeys = new Set(Array.from(grid.querySelectorAll("[data-continue-key]")).map((node) => node.dataset.continueKey));
    const records = mergeRecords(readRecords());
    writeRecords(records);

    let added = 0;
    for (const record of records) {
      const key = record._identity || recordIdentity(record) || recordKey(record);
      if (existingKeys.has(key)) {
        updateExistingCard(key, record);
        continue;
      }
      grid.appendChild(buildCard(record));
      existingKeys.add(key);
      added += 1;
    }

    if (grid.children.length > 0 || added > 0) {
      section.hidden = false;
    }
  }

  function updateExistingCard(key, record) {
    const node = grid.querySelector(`[data-continue-key="${cssEscape(key)}"]`);
    const cover = record.cover || record.poster || "";
    if (!node || !cover) return;
    const image = node.querySelector(".poster img");
    if (image) {
      if (image.getAttribute("src") !== cover) image.setAttribute("src", cover);
      image.dataset.rawPoster = record.raw_poster || "";
      image.dataset.sourcePoster = record.source_poster || "";
      return;
    }
    const poster = node.querySelector(".poster");
    if (!poster) return;
    const fallback = poster.querySelector(".poster-fallback");
    const img = document.createElement("img");
    img.src = cover;
    img.alt = record.title || "";
    img.loading = "lazy";
    img.dataset.rawPoster = record.raw_poster || "";
    img.dataset.sourcePoster = record.source_poster || "";
    img.onerror = function () {
      if (window.RikkaImages) {
        window.RikkaImages.retryPoster(this);
      } else {
        this.remove();
        this.parentElement.classList.add("poster-missing");
      }
    };
    poster.insertBefore(img, fallback || poster.firstChild);
    poster.classList.remove("poster-missing");
  }

  async function clearRecords() {
    localStorage.removeItem(STORAGE_KEY);
    for (const node of Array.from(grid.querySelectorAll(".local-continue-card"))) {
      node.remove();
    }
    if (!grid.children.length) {
      section.hidden = true;
    }
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#039;"
    }[char]));
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#096;");
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  clearButton?.addEventListener("click", clearRecords);
  renderLocalRecords();

  window.RikkaContinue = {
    save(record) {
      const identity = recordIdentity(record) || recordKey(record);
      const records = readRecords().filter((item) => {
        const itemIdentity = recordIdentity(item) || recordKey(item);
        return itemIdentity !== identity;
      });
      records.unshift({ ...record, save_time: Date.now() });
      writeRecords(mergeRecords(records));
    },
    all: readRecords,
    clear: clearRecords
  };
})();
