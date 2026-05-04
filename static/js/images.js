(function () {
  const imageConfig = window.RIKKA_IMAGE_CONFIG || {};

  function proxyType() {
    return String(imageConfig.doubanImageProxyType || "cmliussss-cdn-ali").trim().toLowerCase();
  }

  function proxyUrl() {
    return String(imageConfig.doubanImageProxyUrl || "").trim();
  }

  function replaceHost(url, host) {
    try {
      const parsed = new URL(url, window.location.origin);
      parsed.hostname = host;
      return parsed.toString();
    } catch {
      return String(url || "");
    }
  }

  function doubanProxy(url) {
    const direct = directImageUrl(url);
    return direct ? `/image/douban?url=${encodeURIComponent(direct)}` : "";
  }

  function configuredDoubanImage(url) {
    const direct = directImageUrl(url);
    if (!direct) return "";
    let parsed;
    try {
      parsed = new URL(direct, window.location.origin);
    } catch {
      return direct;
    }
    if (!parsed.hostname.includes("doubanio.com")) return direct;
    const type = proxyType();
    if (type === "server") return doubanProxy(direct);
    if (type === "cmliussss-cdn-tencent") return replaceHost(direct, "img.doubanio.cmliussss.net");
    if (type === "cmliussss-cdn-ali") return replaceHost(direct, "img.doubanio.cmliussss.com");
    if (type === "img3") return replaceHost(direct, "img3.doubanio.com");
    if (type === "custom" && proxyUrl()) return `${proxyUrl()}${encodeURIComponent(direct)}`;
    return direct;
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
    if (!parsed.hostname.includes("doubanio.com")) {
      return direct;
    }
    return configuredDoubanImage(direct);
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
    const hosts = [parsed.hostname];
    for (let index = 1; index < 10; index += 1) {
      hosts.push(`img${index}.doubanio.com`);
    }
    return uniqueUrls(hosts.map((host) => replaceHost(parsed.toString(), host)));
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
    const extraCandidates = proxyType() === "server" ? directCandidates.map(doubanProxy) : directCandidates.map(configuredDoubanImage);
    retrySourcePoster(image, extraCandidates);
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
