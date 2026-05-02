(function () {
  const imageConfig = window.RIKKA_IMAGE_CONFIG || {};

  function doubanProxy(url) {
    const direct = directImageUrl(url);
    return direct ? `/image/douban?url=${encodeURIComponent(direct)}` : "";
  }

  function normalizeUrl(url) {
    try {
      return new URL(url, window.location.origin).toString();
    } catch {
      return String(url || "");
    }
  }

  function uniqueUrls(urls) {
    const seen = new Set();
    return urls
      .filter(Boolean)
      .filter((url) => {
        const key = normalizeUrl(url);
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  function directImageUrl(url) {
    if (!url) return "";
    try {
      const parsed = new URL(url, window.location.origin);
      if (parsed.pathname === "/image/douban") {
        return parsed.searchParams.get("url") || "";
      }
      return parsed.toString();
    } catch {
      return String(url || "");
    }
  }

  function posterSrc(url) {
    const direct = directImageUrl(url);
    if (!direct) return "";
    let parsed;
    try {
      parsed = new URL(direct, window.location.origin);
    } catch {
      return direct;
    }
    if (!/^img\d+\.doubanio\.com$/i.test(parsed.hostname)) {
      return direct;
    }
    const proxyType = imageConfig.doubanImageProxyType || "cmliussss-cdn-ali";
    const proxyUrl = imageConfig.doubanImageProxyUrl || "";
    if (proxyType === "server") return doubanProxy(direct);
    if (proxyType === "img3") {
      parsed.hostname = "img3.doubanio.com";
      return parsed.toString();
    }
    if (proxyType === "cmliussss-cdn-tencent") {
      parsed.hostname = "img.doubanio.cmliussss.net";
      return parsed.toString();
    }
    if (proxyType === "cmliussss-cdn-ali") {
      parsed.hostname = "img.doubanio.cmliussss.com";
      return parsed.toString();
    }
    if (proxyType === "custom" && proxyUrl) {
      return `${proxyUrl}${encodeURIComponent(direct)}`;
    }
    return direct;
  }

  function doubanHostCandidates(url) {
    url = directImageUrl(url);
    let parsed;
    try {
      parsed = new URL(url, window.location.origin);
    } catch {
      return [];
    }
    if (!parsed.hostname.includes("doubanio.com")) return [];
    return [parsed.toString()];
  }

  function tryNextCandidate(image, candidates) {
    const tried = new Set((image.dataset.triedPosters || "").split("\n").filter(Boolean));
    tried.add(normalizeUrl(image.currentSrc || image.src));
    const next = uniqueUrls(candidates.map(directImageUrl)).find((candidate) => !tried.has(normalizeUrl(candidate)));
    if (!next) {
      image.dataset.triedPosters = Array.from(tried).join("\n");
      return false;
    }
    tried.add(normalizeUrl(next));
    image.dataset.triedPosters = Array.from(tried).join("\n");
    image.src = next;
    return true;
  }

  function retryDouban(image) {
    const rawPoster =
      image.dataset.rawPoster ||
      directImageUrl(image.dataset.proxyPoster || "") ||
      directImageUrl(image.currentSrc || image.src);
    if (!rawPoster) {
      retrySourcePoster(image);
      return;
    }
    const directCandidates = doubanHostCandidates(rawPoster);
    if (tryNextCandidate(image, directCandidates)) {
      return;
    }
    retrySourcePoster(image, directCandidates.map(doubanProxy));
  }

  function retrySourcePoster(image, extraCandidates = []) {
    const sourcePoster = directImageUrl(image.dataset.sourcePoster || "");
    if (tryNextCandidate(image, [sourcePoster, ...extraCandidates])) {
      return;
    }
    image.remove();
    image.parentElement?.classList.add("poster-missing");
  }

  function retryPoster(image) {
    if (image.dataset.rawPoster) {
      retryDouban(image);
      return;
    }
    retrySourcePoster(image);
  }

  window.RikkaImages = { retryDouban, retryPoster, posterSrc };
})();
