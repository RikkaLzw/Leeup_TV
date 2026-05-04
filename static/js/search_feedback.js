(function () {
  const progress = document.getElementById("searchProgress");
  const title = document.getElementById("searchProgressTitle");
  const detail = document.getElementById("searchProgressDetail");
  const forms = Array.from(document.querySelectorAll('form[action="/search"]'));
  const links = Array.from(document.querySelectorAll("[data-search-progress-link]"));
  let timer = 0;
  let elapsedTimer = 0;
  let startedAt = 0;

  function queryFromForm(form) {
    const data = new FormData(form);
    return String(data.get("q") || "").trim();
  }

  function showProgress(query, fullSearch) {
    if (!progress) return;
    if (title) title.textContent = fullSearch ? "正在搜索更多源" : "正在搜索";
    if (detail) {
      detail.textContent = query
        ? `正在搜索「${query}」，可能需要几秒`
        : "正在请求视频源，请稍等";
    }
    progress.hidden = false;
    document.body.classList.add("search-busy");
    startedAt = Date.now();
    window.clearTimeout(timer);
    window.clearInterval(elapsedTimer);
    timer = window.setTimeout(() => {
      if (detail && !progress.hidden) {
        detail.textContent = "部分视频源响应较慢，仍在等待结果";
      }
    }, 4500);
    elapsedTimer = window.setInterval(() => {
      if (!detail || progress.hidden || !startedAt) return;
      const seconds = Math.max(Math.round((Date.now() - startedAt) / 1000), 1);
      if (seconds >= 8) {
        detail.textContent = `已搜索 ${seconds} 秒，正在等待慢源返回`;
      }
    }, 1000);
  }

  function markSubmitting(button) {
    if (!button) return;
    button.dataset.originalText = button.textContent || "";
    button.textContent = "搜索中";
    button.disabled = true;
    button.classList.add("is-loading");
  }

  forms.forEach((form) => {
    form.addEventListener("submit", (event) => {
      const query = queryFromForm(form);
      if (!query) return;
      markSubmitting(form.querySelector("[data-search-submit]") || form.querySelector('button[type="submit"]'));
      showProgress(query, false);
    });
  });

  links.forEach((link) => {
    link.addEventListener("click", () => {
      const url = new URL(link.href, window.location.href);
      link.classList.add("is-loading");
      link.setAttribute("aria-disabled", "true");
      showProgress(url.searchParams.get("q") || "", true);
    });
  });

  window.addEventListener("pageshow", () => {
    if (progress) progress.hidden = true;
    document.body.classList.remove("search-busy");
    window.clearTimeout(timer);
    window.clearInterval(elapsedTimer);
    startedAt = 0;
    document.querySelectorAll("[data-search-submit]").forEach((button) => {
      if (button.dataset.originalText) button.textContent = button.dataset.originalText;
      button.disabled = false;
      button.classList.remove("is-loading");
    });
    links.forEach((link) => {
      link.classList.remove("is-loading");
      link.removeAttribute("aria-disabled");
    });
  });
})();
