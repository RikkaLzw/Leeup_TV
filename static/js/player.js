(function () {
  const cfg = window.PLAYER_CONFIG;
  let detail = cfg.detail;
  let currentEpisode = cfg.episode || 0;
  let currentSource = detail.source;
  let currentId = detail.id;
  let hls = null;
  let art = null;
  let loadTimer = null;
  let longPressTimer = null;
  let desktopClickTimer = null;
  let fullscreenPlaybackResumeUntil = 0;
  let lastPlaybackActiveAt = 0;
  let longPressActive = false;
  let longPressPointerId = null;
  let savedPlaybackRate = 1;
  let gestureStart = null;
  let activePlaybackKey = "";
  let introSkippedKey = "";
  let outroSkippedKey = "";
  let autoSwitchAllowed = true;
  let playerOptions = loadPlayerOptions();
  let castReady = false;
  const hlsProxyEnabled = Boolean(cfg.playerOptions?.hlsProxyEnabled);
  const hlsProxyBypassHosts = Array.isArray(cfg.playerOptions?.hlsProxyBypassHosts)
    ? cfg.playerOptions.hlsProxyBypassHosts.map((host) => String(host || "").trim().toLowerCase()).filter(Boolean)
    : [];

  const playerContainer = document.getElementById("player");
  const playerOverlay = document.getElementById("playerOverlay");
  const playerOverlayText = document.getElementById("playerOverlayText");
  const playerLoadingHint = document.getElementById("playerLoadingHint");
  const playerGestureHint = document.getElementById("playerGestureHint");
  const preferStatus = document.getElementById("preferStatus");
  const candidateList = document.getElementById("candidateList");
  const episodeList = document.getElementById("episodeList");
  const episodeCount = document.getElementById("episodeCount");
  const speedTestButton = document.getElementById("speedTestButton");
  const castButton = document.getElementById("castButton");
  const castPanelButton = document.getElementById("castPanelButton");
  const mobileTabs = Array.from(document.querySelectorAll("[data-mobile-panel]"));
  const preferPanel = document.querySelector(".prefer-panel");
  const episodePanel = document.querySelector(".episode-panel");
  const resumeRecord = findResumeRecord();
  configureArtPlayer();
  const player = createPlayer();

  function configureArtPlayer() {
    if (!window.Artplayer) return;
    Artplayer.PLAYBACK_RATE = [0.5, 1, 1.25, 1.5, 2, 3];
    Artplayer.FAST_FORWARD_VALUE = 3;
    Artplayer.FAST_FORWARD_TIME = 700;
    Artplayer.DBCLICK_FULLSCREEN = false;
  }

  function createPlayer() {
    if (window.Artplayer && playerContainer) {
      art = new Artplayer({
        container: playerContainer,
        url: "",
        type: "",
        autoplay: false,
        setting: true,
        hotkey: false,
        pip: true,
        fullscreen: true,
        fullscreenWeb: true,
        playbackRate: true,
        gesture: true,
        fastForward: false,
        miniProgressBar: true,
        mutex: true,
        autoSize: false,
        autoPlayback: false,
        playsInline: true,
        settings: playerSettings(),
        moreVideoAttr: {
          controls: false,
          preload: "auto",
          playsInline: true
        },
        customType: {
          m3u8(video, url) {
            if (hls) {
              hls.destroy();
              hls = null;
            }
            if (window.Hls && Hls.isSupported()) {
              hls = new Hls({ enableWorker: true, fragLoadingTimeOut: 8000, manifestLoadingTimeOut: 8000 });
              hls.loadSource(playbackHlsUrl(url));
              hls.attachMedia(video);
              hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
                resumeTime(video.__rikkaResumeTime || 0);
                maybeSkipOpening();
                clearLoadTimer();
                setPlayerOverlay("", false);
              });
              hls.on(Hls.Events.ERROR, (_event, data) => {
                if (data?.fatal) {
                  if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                    hls.startLoad();
                  } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                    hls.recoverMediaError();
                  } else {
                    setPlayerOverlay("当前源加载失败，可点击测速换源");
                  }
                }
              });
            } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
              video.src = url;
            }
          }
        }
      });
      return art.video;
    }

    const video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    playerContainer?.appendChild(video);
    return video;
  }

  function setupCastButton() {
    if (!player || (!castButton && !castPanelButton)) return;
    updateCastButton();
    castButton?.addEventListener("click", startCasting);
    castPanelButton?.addEventListener("click", startCasting);
    player.addEventListener?.("webkitplaybacktargetavailabilitychanged", updateCastButton);
    player.addEventListener?.("webkitcurrentplaybacktargetiswirelesschanged", () => {
      updateCastButton(player.webkitCurrentPlaybackTargetIsWireless ? "AirPlay 中" : "");
    });
    setupGoogleCast();
  }

  function updateCastButton(label) {
    if (!player) return;
    const mode = castMode();
    const supported = mode !== "";
    const text = label || castButtonText(mode);
    [castButton, castPanelButton].forEach((button) => {
      if (!button) return;
      button.hidden = false;
      button.classList.toggle("unsupported", !supported);
      button.setAttribute("aria-disabled", supported ? "false" : "true");
      button.title = supported ? castButtonTitle(mode) : "当前浏览器或设备不支持投屏";
      const labelNode = button.querySelector("span");
      if (labelNode) {
        labelNode.textContent = text;
      } else {
        button.textContent = text;
      }
    });
  }

  function castMode() {
    if (canUseAirPlay()) return "airplay";
    if (canUseGoogleCast()) return "chromecast";
    return "";
  }

  function castButtonText(mode) {
    if (mode === "airplay") return "AirPlay";
    if (mode === "chromecast") return "Cast";
    return "不可投屏";
  }

  function castButtonTitle(mode) {
    if (mode === "airplay") return "通过 AirPlay 投屏";
    if (mode === "chromecast") return "通过 Chromecast / Google TV 投屏";
    return "当前浏览器或设备不支持投屏";
  }

  function canUseAirPlay() {
    return Boolean(player && typeof player.webkitShowPlaybackTargetPicker === "function");
  }

  function canUseGoogleCast() {
    return Boolean(castReady && window.cast?.framework && window.chrome?.cast);
  }

  function setupGoogleCast() {
    if (!window.chrome) window.chrome = {};
    window.__rikkaOnCastApiAvailable = (isAvailable) => {
      if (!isAvailable || !window.cast?.framework || !window.chrome?.cast) {
        castReady = false;
        updateCastButton();
        return;
      }
      try {
        const context = window.cast.framework.CastContext.getInstance();
        context.setOptions({
          receiverApplicationId: window.chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
          autoJoinPolicy: window.chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED
        });
        castReady = true;
      } catch {
        castReady = false;
      }
      updateCastButton();
    };
    if (typeof window.__rikkaCastAvailability === "boolean") {
      window.__rikkaOnCastApiAvailable(window.__rikkaCastAvailability);
      return;
    }
    if (window.cast?.framework && window.chrome?.cast) {
      window.__rikkaOnCastApiAvailable(true);
    }
  }


  function startCasting() {
    if (!player) return;
    if (canUseAirPlay()) {
      try {
        prepareNativeAirPlaySource();
        player.webkitShowPlaybackTargetPicker();
        showPlayerNotice("请选择 AirPlay 设备");
        return;
      } catch {
        showPlayerNotice("AirPlay 未启动");
        return;
      }
    }
    if (canUseGoogleCast()) {
      startGoogleCast();
      return;
    }
    showPlayerNotice("当前浏览器或设备不支持投屏");
  }

  async function startGoogleCast() {
    const sourceUrl = castPlaybackUrl();
    if (!sourceUrl) {
      showPlayerNotice("无可投屏播放地址");
      return;
    }
    try {
      showPlayerNotice("正在连接 Cast 设备");
      const context = window.cast.framework.CastContext.getInstance();
      let session = context.getCurrentSession();
      if (!session) {
        session = await context.requestSession();
      }
      if (!session) {
        showPlayerNotice("未选择 Cast 设备");
        return;
      }
      const mediaInfo = new window.chrome.cast.media.MediaInfo(sourceUrl, castContentType(sourceUrl));
      mediaInfo.metadata = castMediaMetadata();
      const request = new window.chrome.cast.media.LoadRequest(mediaInfo);
      const currentTime = Number(player.currentTime || 0);
      if (Number.isFinite(currentTime) && currentTime > 0) request.currentTime = currentTime;
      request.autoplay = true;
      await session.loadMedia(request);
      showPlayerNotice("已发送到 Cast 设备");
      updateCastButton("Cast 中");
    } catch (error) {
      const code = String(error?.code || error?.message || error || "");
      showPlayerNotice(code === "cancel" ? "已取消 Cast" : "Cast 未启动");
    }
  }

  function prepareNativeAirPlaySource() {
    const sourceUrl = currentEpisodeUrl();
    if (!isHlsUrl(sourceUrl)) return;
    if (hls) {
      hls.destroy();
      hls = null;
    }
    if (art) {
      art.type = "";
    }
    player.src = sourceUrl;
    player.load();
  }

  function castPlaybackUrl() {
    const sourceUrl = currentEpisodeUrl();
    if (!sourceUrl) return "";
    const playbackUrl = isHlsUrl(sourceUrl) ? playbackHlsUrl(sourceUrl) : sourceUrl;
    return absolutePlaybackUrl(playbackUrl);
  }

  function absolutePlaybackUrl(url) {
    const value = String(url || "").trim();
    if (!value) return "";
    try {
      return new URL(value, window.location.href).href;
    } catch {
      return value;
    }
  }

  function castContentType(url) {
    if (isHlsUrl(url) || isHlsProxyUrl(url)) return "application/x-mpegURL";
    if (/\.mp4(?:[?#]|$)/i.test(String(url || ""))) return "video/mp4";
    return "video/mp4";
  }

  function isHlsProxyUrl(url) {
    try {
      const parsed = new URL(String(url || ""), window.location.href);
      return parsed.pathname === "/hls-proxy" && isHlsUrl(parsed.searchParams.get("url") || "");
    } catch {
      return false;
    }
  }

  function castMediaMetadata() {
    const metadata = new window.chrome.cast.media.GenericMediaMetadata();
    metadata.title = String(detail.title || "");
    metadata.subtitle = [detail.source_name, episodeTitle(currentEpisode)].filter(Boolean).join(" · ");
    const image = String(detail.raw_poster || detail.poster || detail.source_poster || "");
    if (image) metadata.images = [new window.chrome.cast.Image(absolutePlaybackUrl(image))];
    return metadata;
  }

  function setStatus(text) {
    if (preferStatus) preferStatus.textContent = text;
  }

  async function readJsonResponse(response, fallbackMessage) {
    const body = await response.text();
    const text = String(body || "").trim();
    let data = {};
    if (text) {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json") || /^[\[{]/.test(text)) {
        try {
          data = JSON.parse(text);
        } catch {
          throw new Error(`${fallbackMessage}：接口返回的 JSON 无法解析`);
        }
      } else {
        const snippet = responseTextSnippet(text);
        const status = response.status ? `HTTP ${response.status}` : "响应";
        throw new Error(`${fallbackMessage}：${status} 返回了非 JSON 内容${snippet ? `（${snippet}）` : ""}`);
      }
    }
    if (!response.ok) {
      throw new Error(payloadErrorMessage(data) || `${fallbackMessage}：HTTP ${response.status}`);
    }
    return data;
  }

  function payloadErrorMessage(data) {
    const message = data?.error || data?.message || data?.detail;
    if (!message) return "";
    if (Array.isArray(message)) {
      return message.map((item) => item?.msg || item?.type || "参数错误").join("；");
    }
    if (typeof message === "object") {
      return JSON.stringify(message).slice(0, 120);
    }
    return String(message);
  }

  function responseTextSnippet(text) {
    return String(text || "")
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 120);
  }

  function setPlayerOverlay(text, visible = true) {
    const shouldShow = Boolean(visible && text);
    if (playerOverlay && playerOverlayText) {
      playerOverlayText.textContent = text;
      playerOverlay.hidden = !shouldShow;
    }
    if (playerLoadingHint) {
      playerLoadingHint.textContent = text || "";
      playerLoadingHint.hidden = !shouldShow;
    }
    if (art) {
      try {
        art.loading.show = shouldShow;
      } catch {
        // ArtPlayer loading state is best-effort across versions.
      }
      if (shouldShow) showPlayerNotice(text);
    }
  }

  function showPlayerNotice(text) {
    if (!text || !art) return;
    try {
      art.notice.show = text;
    } catch {
      // Ignore notice failures; the custom hint remains visible.
    }
  }

  function episodeTitle(index) {
    return String(index + 1);
  }

  function playUrl(url, resume) {
    if (!url) {
      setStatus("无播放地址");
      setPlayerOverlay("无播放地址");
      return;
    }
    setPlayerOverlay("正在加载视频");
    activePlaybackKey = `${currentSource}+${currentId}+${currentEpisode}+${Date.now()}`;
    introSkippedKey = "";
    outroSkippedKey = "";
    clearLoadTimer();
    loadTimer = window.setTimeout(() => {
      setPlayerOverlay("当前源加载较慢，可点击测速换源");
      if (autoSwitchAllowed) {
        tryNextTestedCandidate();
      }
    }, 9000);
    const isHls = isHlsUrl(url);
    const finalUrl = isHls ? playbackHlsUrl(url) : url;
    if (hls) {
      hls.destroy();
      hls = null;
    }
    if (art) {
      player.__rikkaResumeTime = resume || 0;
      art.type = isHls ? "m3u8" : "";
      art.switchUrl(finalUrl);
      player.onloadedmetadata = () => {
        resumeTime(resume);
        maybeSkipOpening();
        clearLoadTimer();
        setPlayerOverlay("", false);
      };
    } else if (isHls && window.Hls && Hls.isSupported()) {
      hls = new Hls({ enableWorker: true, fragLoadingTimeOut: 8000, manifestLoadingTimeOut: 8000 });
      hls.loadSource(finalUrl);
      hls.attachMedia(player);
      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        resumeTime(resume);
        maybeSkipOpening();
        clearLoadTimer();
        setPlayerOverlay("", false);
      });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data?.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            hls.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          } else {
            setPlayerOverlay("当前源加载失败，可点击测速换源");
          }
        }
      });
    } else {
      player.src = finalUrl;
      player.onloadedmetadata = () => {
        resumeTime(resume);
        maybeSkipOpening();
        clearLoadTimer();
        setPlayerOverlay("", false);
      };
    }
  }

  function clearLoadTimer() {
    if (loadTimer) {
      window.clearTimeout(loadTimer);
      loadTimer = null;
    }
  }

  function resumeTime(resume) {
    if (resume && resume > 3 && Number.isFinite(player.duration)) {
      player.currentTime = Math.min(resume, Math.max(player.duration - 5, 0));
    }
  }

  function renderEpisodes() {
    if (!episodeList || !episodeCount) return;
    episodeList.innerHTML = "";
    episodeCount.textContent = `${detail.episodes.length} 集`;
    detail.episodes.forEach((url, index) => {
      const button = document.createElement("button");
      button.className = `episode-button${index === currentEpisode ? " active" : ""}`;
      button.textContent = episodeTitle(index);
      button.addEventListener("click", () => {
        currentEpisode = index;
        renderEpisodes();
        playUrl(detail.episodes[currentEpisode], 0);
      });
      episodeList.appendChild(button);
    });
  }

  function setMobilePanel(panel) {
    if (!episodePanel || !mobileTabs.length) {
      preferPanel?.classList.remove("mobile-hidden");
      return;
    }
    const isSources = panel === "sources";
    episodePanel?.classList.toggle("mobile-hidden", isSources);
    preferPanel?.classList.toggle("mobile-hidden", !isSources);
    mobileTabs.forEach((tab) => {
      const active = tab.dataset.mobilePanel === panel;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function isMobileViewport() {
    return window.matchMedia("(max-width: 860px)").matches;
  }

  function renderCandidates(candidates) {
    const list = Array.from(candidates || []);
    candidateList.__candidates = list;
    candidateList.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "candidate-empty";
      empty.textContent = cfg.preferEnabled ? "点击测速后显示候选源" : "当前为直连播放";
      candidateList.appendChild(empty);
      return;
    }
    list.forEach((candidate, candidateIndex) => {
      const test = candidate.test || {};
      const button = document.createElement("button");
      button.className = `candidate${candidate.source === currentSource && candidate.id === currentId ? " active" : ""}`;
      button.dataset.index = String(candidateIndex);
      button.dataset.key = `${candidate.source}+${candidate.id}`;
      button.innerHTML = `
        <span class="candidate-title">${escapeHtml(candidate.source_name || candidate.source)}</span>
        ${renderCandidateMetrics(test)}
      `;
      button.addEventListener("click", () => {
        applyCandidate(candidate, false);
      });
      candidateList.appendChild(button);
    });
  }

  function ensureCandidateVisible(candidate) {
    requestAnimationFrame(() => {
      const active = candidateList.querySelector(
        `.candidate[data-key="${cssEscape(`${candidate.source}+${candidate.id}`)}"]`
      );
      active?.scrollIntoView({ block: "nearest" });
    });
  }

  function applyCandidate(candidate, autoPlay) {
    const handoffTime = currentPlaybackTime();
    autoSwitchAllowed = Boolean(autoPlay);
    if (handoffTime > 1) saveProgress();
    detail = candidate;
    currentSource = candidate.source;
    currentId = candidate.id;
    currentEpisode = Math.min(candidate.selected_episode || currentEpisode, detail.episodes.length - 1);
    document.getElementById("activeTitle").textContent = detail.title;
    document.getElementById("activeMeta").innerHTML = `<span>${escapeHtml(detail.source_name)}</span>${detail.year ? `<span>${escapeHtml(detail.year)}</span>` : ""}`;
    const desc = document.getElementById("activeDesc");
    if (desc) desc.textContent = detail.desc || "";
    renderEpisodes();
    renderCandidates(Array.from(candidateList.__candidates || []));
    ensureCandidateVisible(candidate);
    playUrl(detail.episodes[currentEpisode], resumeTimeFor(currentSource, currentId, currentEpisode, handoffTime));
    if (autoPlay) {
      playMedia();
    }
  }

  function initPlayback() {
    const metric = (cfg.sourceMetrics || {})[detail.source];
    renderCandidates([withMetricTest(detail, metric)]);
    if (cfg.preferEnabled) {
      setStatus(detail.recommended_by_metrics ? "站内推荐" : "手动测速");
    } else {
      setStatus("直连播放");
    }
    playUrl(detail.episodes[currentEpisode], resumeTimeFor(currentSource, currentId, currentEpisode));
  }

  function playMedia() {
    lastPlaybackActiveAt = Date.now();
    if (art) {
      const promise = art.play();
      if (promise?.catch) promise.catch(() => {});
      return;
    }
    player.play().catch(() => {});
  }

  function playNextEpisode() {
    const episodes = Array.from(detail.episodes || []);
    if (currentEpisode >= episodes.length - 1) return;
    saveProgress();
    currentEpisode += 1;
    renderEpisodes();
    playUrl(episodes[currentEpisode], 0);
    playMedia();
  }

  function playerSettings() {
    return [
      skipSetting("intro"),
      skipSetting("outro")
    ];
  }

  function skipSetting(kind) {
    const isIntro = kind === "intro";
    const enabledKey = isIntro ? "skipIntroEnabled" : "skipOutroEnabled";
    const secondsKey = isIntro ? "skipIntroSeconds" : "skipOutroSeconds";
    const title = isIntro ? "跳过片头" : "跳过片尾";
    return {
      width: 240,
      name: `skip-${kind}`,
      html: title,
      tooltip: skipTooltip(kind),
      selector: [
        {
          name: `skip-${kind}-enabled`,
          html: "启用",
          tooltip: playerOptions[enabledKey] ? "开" : "关",
          switch: Boolean(playerOptions[enabledKey]),
          onSwitch(item) {
            const next = !Boolean(item.switch);
            playerOptions[enabledKey] = next;
            item.tooltip = next ? "开" : "关";
            updateSkipParentTooltip(item, kind);
            savePlayerOptions();
            if (next && isIntro) maybeSkipOpening();
            return next;
          }
        },
        {
          width: 240,
          name: `skip-${kind}-seconds`,
          html: "时间",
          tooltip: `${playerOptions[secondsKey]}s`,
          range: [playerOptions[secondsKey], 0, 600, 5],
          onChange(item) {
            playerOptions[secondsKey] = normalizeSkipSeconds(item.range[0]);
            item.range = [playerOptions[secondsKey], 0, 600, 5];
            updateSkipParentTooltip(item, kind);
            savePlayerOptions();
            if (isIntro) maybeSkipOpening();
            return `${playerOptions[secondsKey]}s`;
          }
        }
      ]
    };
  }

  function skipTooltip(kind) {
    const isIntro = kind === "intro";
    const enabled = isIntro ? playerOptions.skipIntroEnabled : playerOptions.skipOutroEnabled;
    const seconds = isIntro ? playerOptions.skipIntroSeconds : playerOptions.skipOutroSeconds;
    return enabled ? `${seconds}s` : "关闭";
  }

  function updateSkipParentTooltip(item, kind) {
    if (item?.$parent) item.$parent.tooltip = skipTooltip(kind);
  }

  function loadPlayerOptions() {
    const defaults = {
      skipIntroEnabled: Boolean(cfg.playerOptions?.skipIntroEnabled),
      skipOutroEnabled: Boolean(cfg.playerOptions?.skipOutroEnabled),
      skipIntroSeconds: normalizeSkipSeconds(cfg.playerOptions?.skipIntroSeconds || 0),
      skipOutroSeconds: normalizeSkipSeconds(cfg.playerOptions?.skipOutroSeconds || 0)
    };
    try {
      const saved = JSON.parse(localStorage.getItem("mewkotv_player_options") || "{}");
      return {
        skipIntroEnabled: Boolean(saved.skipIntroEnabled ?? defaults.skipIntroEnabled),
        skipOutroEnabled: Boolean(saved.skipOutroEnabled ?? defaults.skipOutroEnabled),
        skipIntroSeconds: normalizeSkipSeconds(saved.skipIntroSeconds ?? defaults.skipIntroSeconds),
        skipOutroSeconds: normalizeSkipSeconds(saved.skipOutroSeconds ?? defaults.skipOutroSeconds)
      };
    } catch {
      return defaults;
    }
  }

  function savePlayerOptions() {
    try {
      localStorage.setItem("mewkotv_player_options", JSON.stringify(playerOptions));
    } catch {
      // ignore local storage failures
    }
  }

  function normalizeSkipSeconds(value) {
    return Math.max(0, Math.min(600, Math.round(Number(value || 0))));
  }

  function isHlsUrl(url) {
    return /\.m3u8(?:[?#]|$)/i.test(String(url || ""));
  }

  function currentEpisodeUrl() {
    return String((detail.episodes || [])[currentEpisode] || "");
  }

  function playbackHlsUrl(url) {
    const value = String(url || "");
    if (!hlsProxyEnabled || !isHlsUrl(value) || value.startsWith("/hls-proxy?") || shouldBypassHlsProxy(value)) return value;
    return `/hls-proxy?url=${encodeURIComponent(value)}`;
  }

  function shouldBypassHlsProxy(url) {
    if (!hlsProxyBypassHosts.length) return false;
    try {
      const parsed = new URL(url, window.location.origin);
      const hostname = parsed.hostname.toLowerCase();
      return hlsProxyBypassHosts.some((host) => hostname === host || hostname.endsWith(`.${host}`));
    } catch {
      return false;
    }
  }

  function maybeSkipOpening() {
    if (!playerOptions.skipIntroEnabled) return;
    const seconds = normalizeSkipSeconds(playerOptions.skipIntroSeconds);
    if (!seconds || introSkippedKey === activePlaybackKey) return;
    if (!Number.isFinite(player.duration) || player.duration <= seconds + 3) return;
    if (player.currentTime > 3 || player.currentTime >= seconds) return;
    introSkippedKey = activePlaybackKey;
    player.currentTime = Math.min(seconds, Math.max(player.duration - 3, 0));
  }

  function maybeSkipEnding() {
    if (!playerOptions.skipOutroEnabled) return;
    const seconds = normalizeSkipSeconds(playerOptions.skipOutroSeconds);
    if (!seconds || outroSkippedKey === activePlaybackKey) return;
    if (!Number.isFinite(player.duration) || player.duration <= seconds + 3) return;
    const remaining = player.duration - player.currentTime;
    if (remaining > seconds || remaining <= 2) return;
    outroSkippedKey = activePlaybackKey;
    player.currentTime = Math.max(player.duration - 1, 0);
  }

  function setupLongPressFastForward() {
    if (!playerContainer) return;
    playerContainer.addEventListener("pointerdown", startLongPress);
    playerContainer.addEventListener("pointermove", moveLongPress, { passive: true });
    playerContainer.addEventListener("pointerup", stopLongPress, { passive: true });
    playerContainer.addEventListener("pointercancel", stopLongPress, { passive: true });
    playerContainer.addEventListener("pointerleave", stopLongPress, { passive: true });
    document.addEventListener("pointerup", stopLongPress, { passive: true });
    document.addEventListener("pointercancel", stopLongPress, { passive: true });
    window.addEventListener("blur", forceStopLongPress);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) forceStopLongPress();
    });
  }

  function startLongPress(event) {
    if (event.pointerType === "mouse") return;
    if (!isMobileViewport() && event.pointerType !== "touch" && event.pointerType !== "pen") return;
    if (isPlayerControlTarget(event.target)) return;
    if (longPressActive) stopLongPress(event);
    longPressPointerId = event.pointerId;
    gestureStart = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    clearLongPressTimer();
    longPressTimer = window.setTimeout(() => activateLongPress(), 450);
  }

  function moveLongPress(event) {
    if (longPressPointerId !== null && event.pointerId !== longPressPointerId) return;
    if (!gestureStart || longPressActive) return;
    const distance = Math.hypot(event.clientX - gestureStart.x, event.clientY - gestureStart.y);
    if (distance > 14) cancelPendingLongPress();
  }

  function activateLongPress() {
    if (!player) return;
    clearLongPressTimer();
    longPressActive = true;
    savedPlaybackRate = Number(player.playbackRate || 1);
    player.playbackRate = 3;
    showGestureHint("3x 快进中");
    if (player.paused) playMedia();
  }

  function stopLongPress(event) {
    if (event?.pointerId !== undefined && longPressPointerId !== null && event.pointerId !== longPressPointerId) {
      return;
    }
    clearLongPressTimer();
    gestureStart = null;
    longPressPointerId = null;
    if (!longPressActive) return;
    longPressActive = false;
    player.playbackRate = savedPlaybackRate || 1;
    hideGestureHint();
  }

  function forceStopLongPress() {
    stopLongPress();
  }

  function cancelPendingLongPress() {
    clearLongPressTimer();
    gestureStart = null;
    longPressPointerId = null;
  }

  function clearLongPressTimer() {
    if (!longPressTimer) return;
    window.clearTimeout(longPressTimer);
    longPressTimer = null;
  }

  function showGestureHint(text) {
    if (playerGestureHint) {
      playerGestureHint.textContent = text;
      playerGestureHint.hidden = false;
    }
    showPlayerNotice(text);
  }

  function hideGestureHint() {
    if (playerGestureHint) playerGestureHint.hidden = true;
  }

  function isPlayerControlTarget(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest(
      ".art-bottom, .art-controls, .art-progress, .art-settings, .art-contextmenus, .art-info, .art-layer"
    ));
  }

  function isPlayerSurfaceTarget(target) {
    if (!(target instanceof Element)) return false;
    if (isPlayerControlTarget(target)) return false;
    return Boolean(target.closest(".art-video, .art-video-player, .art-mask, .art-state, .art-poster"));
  }

  function shouldHandleDesktopPointer(event) {
    if (!art || isMobileViewport()) return false;
    if (event.button !== undefined && event.button !== 0) return false;
    return isPlayerSurfaceTarget(event.target);
  }

  function captureDesktopPlayerEvent(event) {
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  }

  function setupDesktopClickPlayback() {
    if (!playerContainer) return;
    playerContainer.addEventListener("click", handleDesktopPlayerClick, true);
    playerContainer.addEventListener("dblclick", handleDesktopPlayerDoubleClick, true);
    playerContainer.addEventListener("mousemove", wakeDesktopPlayerControls, true);
  }

  function setupDesktopKeyboardControls() {
    document.addEventListener("keydown", handleDesktopKeyboardControl, true);
  }

  function handleDesktopKeyboardControl(event) {
    if (!shouldHandleDesktopKeyboard(event)) return;
    const key = event.key;
    if (![" ", "Spacebar", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(key)) return;
    event.preventDefault();
    event.stopPropagation();
    if (key === " " || key === "Spacebar") {
      togglePlayback();
      return;
    }
    if (key === "ArrowLeft") {
      seekBy(-5);
      return;
    }
    if (key === "ArrowRight") {
      seekBy(5);
      return;
    }
    if (key === "ArrowUp") {
      changeVolume(0.05);
      return;
    }
    if (key === "ArrowDown") {
      changeVolume(-0.05);
    }
  }

  function shouldHandleDesktopKeyboard(event) {
    if (!player || isMobileViewport()) return false;
    if (event.altKey || event.ctrlKey || event.metaKey) return false;
    if (isEditableTarget(event.target)) return false;
    if (isPlayerMenuTarget(event.target)) return false;
    return document.body.contains(playerContainer);
  }

  function isEditableTarget(target) {
    if (!(target instanceof Element)) return false;
    const tag = target.tagName.toLowerCase();
    return Boolean(
      target.closest("input, textarea, select, [contenteditable='true'], [contenteditable='']")
      || tag === "button"
      || tag === "a"
    );
  }

  function isPlayerMenuTarget(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest(".art-settings, .art-contextmenus, .settings-menu, .about-modal, .notice-modal"));
  }

  function wakeDesktopPlayerControls(event) {
    if (!art || isMobileViewport()) return;
    if (!(event.target instanceof Element) || !event.target.closest(".art-video-player")) return;
    const playerRoot = playerContainer?.querySelector(".art-video-player");
    if (playerRoot) {
      playerRoot.classList.remove("art-hide-cursor");
      playerRoot.classList.add("art-hover");
    }
    try {
      if (art.controls) art.controls.show = true;
    } catch {
      // ArtPlayer control internals can vary by version.
    }
  }

  function handleDesktopPlayerClick(event) {
    if (!shouldHandleDesktopPointer(event)) return;
    captureDesktopPlayerEvent(event);
    if (event.detail > 1) {
      clearDesktopClickTimer();
      return;
    }
    emitArtPlayerEvent("click", event);
    clearDesktopClickTimer();
    desktopClickTimer = window.setTimeout(() => {
      desktopClickTimer = null;
      togglePlayback();
    }, 260);
  }

  function handleDesktopPlayerDoubleClick(event) {
    if (!shouldHandleDesktopPointer(event)) return;
    captureDesktopPlayerEvent(event);
    emitArtPlayerEvent("dblclick", event);
    clearDesktopClickTimer();
    const shouldResume = Boolean(player && !player.paused && !player.ended);
    toggleFullscreen();
    blurActivePlayerControl();
    if (shouldResume) keepPlaybackAfterFullscreen();
  }

  function blurActivePlayerControl() {
    const active = document.activeElement;
    if (active instanceof HTMLElement && playerContainer?.contains(active)) {
      active.blur();
    }
  }

  function emitArtPlayerEvent(name, event) {
    if (!art || typeof art.emit !== "function") return;
    try {
      art.emit(name, event);
    } catch {
      // Custom pointer handling should not fail playback if ArtPlayer changes internals.
    }
  }

  function clearDesktopClickTimer() {
    if (!desktopClickTimer) return;
    window.clearTimeout(desktopClickTimer);
    desktopClickTimer = null;
  }

  function togglePlayback() {
    if (!player) return;
    if (player.paused) {
      playMedia();
    } else {
      cancelFullscreenPlaybackResume();
      player.pause();
    }
  }

  function seekBy(seconds) {
    if (!player || !Number.isFinite(player.duration)) return;
    const nextTime = Math.min(Math.max(Number(player.currentTime || 0) + seconds, 0), Math.max(player.duration - 0.2, 0));
    player.currentTime = nextTime;
    showPlayerNotice(seconds > 0 ? "快进 5s" : "后退 5s");
  }

  function changeVolume(delta) {
    if (!player) return;
    const nextVolume = Math.min(Math.max(Number(player.volume || 0) + delta, 0), 1);
    player.volume = nextVolume;
    player.muted = nextVolume <= 0;
    const label = Math.round(nextVolume * 100);
    showPlayerNotice(`音量 ${label}%`);
    syncArtVolume(nextVolume);
  }

  function syncArtVolume(volume) {
    if (!art || !("volume" in art)) return;
    try {
      art.volume = volume;
    } catch {
      // The native video volume has already been updated.
    }
  }

  function toggleFullscreen() {
    if (art && "fullscreen" in art) {
      try {
        art.fullscreen = !art.fullscreen;
        return;
      } catch {
        // Fall through to the browser fullscreen API.
      }
    }
    const fullscreenTarget = playerContainer?.querySelector(".art-video-player") || playerContainer;
    if (!fullscreenTarget) return;
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else {
      fullscreenTarget.requestFullscreen?.();
    }
  }

  function keepPlaybackAfterFullscreen() {
    fullscreenPlaybackResumeUntil = Date.now() + 1600;
    const resumeToken = fullscreenPlaybackResumeUntil;
    [60, 220, 520, 1000, 1500].forEach((delay) => {
      window.setTimeout(() => resumePlaybackAfterFullscreen(resumeToken), delay);
    });
  }

  function cancelFullscreenPlaybackResume() {
    fullscreenPlaybackResumeUntil = 0;
  }

  function handleFullscreenChange() {
    if (player && !player.paused && !player.ended) {
      keepPlaybackAfterFullscreen();
    }
    resumePlaybackAfterFullscreen(fullscreenPlaybackResumeUntil);
  }

  function resumePlaybackAfterFullscreen(resumeToken) {
    if (!resumeToken || resumeToken !== fullscreenPlaybackResumeUntil) return;
    if (Date.now() > resumeToken) return;
    if (!player || player.ended || !player.paused) return;
    playMedia();
  }

  function notePlaybackActive() {
    if (player && !player.paused && !player.ended) {
      lastPlaybackActiveAt = Date.now();
    }
  }

  async function runPreference() {
    if (!cfg.preferEnabled || speedTestButton?.disabled) return;
    const originalText = speedTestButton.textContent;
    speedTestButton.disabled = true;
    speedTestButton.textContent = "测速中";
    setStatus("正在匹配可用候选源");
    try {
      const res = await fetch("/api/prefer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: (cfg.originalDetail || cfg.detail).title,
          source: (cfg.originalDetail || cfg.detail).source,
          id: (cfg.originalDetail || cfg.detail).id,
          episode: currentEpisode,
          year: (cfg.originalDetail || cfg.detail).year || "",
          kind: inferDetailKind(cfg.originalDetail || cfg.detail)
        })
      });
      const data = await readJsonResponse(res, "测速候选源获取失败");
      let candidates = data.candidates || [];
      const meta = data.meta || {};
      const total = Number(meta.prepared_count || candidates.length || 0);
      setStatus(total ? `找到 ${total} 个可用候选，正在测速` : "没有匹配到可用候选源");
      candidateList.__candidates = candidates;
      renderCandidates(candidates);
      candidates = await testCandidatesInBrowser(candidates);
      const ranked = rankCandidates(candidates);
      candidateList.__candidates = ranked;
      renderCandidates(ranked);
      postSpeedResults(ranked);
      const best = ranked.find((candidate) => candidate.test?.ok);
      if (best) {
        setStatus(`测速完成：推荐 ${best.source_name}`);
        if (player.paused && player.currentTime < 1 && `${best.source}+${best.id}` !== `${currentSource}+${currentId}`) {
          autoSwitchAllowed = true;
          applyCandidate(best, true);
        }
      } else {
        setStatus("没有可用候选源");
      }
    } catch (error) {
      setStatus(error.message || "测速失败");
    } finally {
      speedTestButton.disabled = false;
      speedTestButton.textContent = originalText || "测速";
    }
  }

  function inferDetailKind(item) {
    const typeName = String(item?.type_name || item?.class || "");
    if (/(电影|片|纪录|记录)/.test(typeName)) return "movie";
    if (/(剧|连续|综艺|动漫|番)/.test(typeName)) return "tv";
    const episodes = Array.isArray(item?.episodes) ? item.episodes.length : 0;
    if (episodes > 3) return "tv";
    if (episodes === 1) return "movie";
    return "";
  }

  async function testCandidatesInBrowser(candidates) {
    const list = Array.from(candidates || []);
    const batchSize = Math.max(1, Math.min(4, Number(cfg.speedTestConcurrency || 4)));
    const results = [];
    for (let start = 0; start < list.length; start += batchSize) {
      const batch = list.slice(start, start + batchSize);
      const tested = await Promise.all(batch.map(async (candidate) => {
        const selectedEpisode = Math.min(Math.max(currentEpisode, 0), Math.max((candidate.episodes || []).length - 1, 0));
        const url = candidate.selected_url || (candidate.episodes || [])[selectedEpisode] || "";
        const next = { ...candidate, selected_episode: selectedEpisode, selected_url: url };
        next.test = { ok: false, error_label: "测速中", quality: "未知", speed_label: "测速中", latency_ms: 0, speed_kbps: 0, score: 0 };
        updateCandidate(next);
        try {
          next.test = await browserMeasureStream(url);
        } catch {
          next.test = failTest();
        }
        updateCandidate(next);
        return next;
      }));
      results.push(...tested);
    }
    return results;
  }

  function browserMeasureStream(url) {
    const value = String(url || "").trim();
    if (!value) return Promise.resolve(failTest("empty_url"));
    if (isHlsUrl(value)) return measureHlsStream(value);
    return measureNativeStream(value);
  }

  function measureHlsStream(url) {
    const playbackUrl = playbackHlsUrl(url);
    if (!window.Hls || !Hls.isSupported()) {
      return measureMediaElementPlayback(playbackUrl, "browser_hls_native");
    }
    return new Promise((resolve) => {
      const started = performance.now();
      const video = document.createElement("video");
      video.muted = true;
      video.preload = "auto";
      video.playsInline = true;
      video.style.cssText = "position:fixed;width:1px;height:1px;left:-9999px;top:-9999px;opacity:0;pointer-events:none;";
      document.body.appendChild(video);
      let hlsTester = null;
      let fragStart = 0;
      let latencyMs = 0;
      let speedKbps = 0;
      let manifestWidth = 0;
      let manifestHeight = 0;
      let resolved = false;
      let manifestReady = false;
      let metadataReady = false;
      let probeStartedAt = 0;
      let probeBytes = 0;
      let probeFragments = 0;
      let recoveries = 0;
      let timer = 0;
      let playableTimer = 0;
      let playableDeadline = 0;
      const minProbeBytes = 384 * 1024;
      const maxProbeFragments = 4;

      const finish = (test) => {
        if (resolved) return;
        resolved = true;
        window.clearTimeout(timer);
        window.clearTimeout(playableTimer);
        if (hlsTester) hlsTester.destroy();
        try {
          video.pause();
          video.removeAttribute("src");
          video.load();
        } catch {
          // The hidden probe element is best-effort cleanup.
        }
        video.remove();
        resolve(test);
      };
      const finishPlayable = (confidence) => {
        const dimensions = probeDimensions(video, manifestWidth, manifestHeight);
        const latency = latencyMs || Math.round(performance.now() - started);
        finish(playableFallbackTest(dimensions.quality, dimensions.height, latency, confidence, "browser_hls_playable"));
      };
      const schedulePlayableFallback = (delayMs) => {
        if (resolved) return;
        const nextDeadline = performance.now() + delayMs;
        if (playableTimer && playableDeadline <= nextDeadline) return;
        window.clearTimeout(playableTimer);
        playableDeadline = nextDeadline;
        playableTimer = window.setTimeout(() => {
          finishPlayable(metadataReady || probeBytes > 0 ? "metadata" : "manifest");
        }, delayMs);
      };
      const maybeFinish = () => {
        if (!speedKbps) return;
        const dimensions = probeDimensions(video, manifestWidth, manifestHeight);
        finish(okTest(dimensions.quality, dimensions.height, latencyMs, speedKbps, { measuredBy: "browser_hls" }));
      };
      timer = window.setTimeout(() => {
        if (manifestReady || metadataReady || probeBytes > 0) {
          finishPlayable(metadataReady || probeBytes > 0 ? "metadata" : "manifest");
        } else {
          finish(failTest("hls_probe_timeout", "测速超时", "browser_hls"));
        }
      }, 11000);
      hlsTester = new Hls({
        enableWorker: true,
        fragLoadingTimeOut: 6000,
        manifestLoadingTimeOut: 6000,
        fragLoadingMaxRetry: 2,
        manifestLoadingMaxRetry: 2
      });
      hlsTester.on(Hls.Events.MANIFEST_LOADED, (_event, data) => {
        const loading = data?.stats?.loading || {};
        const first = Number(loading.first || loading.end || 0);
        const start = Number(loading.start || 0);
        if (first > start) latencyMs = Math.max(Math.round(first - start), 0);
      });
      hlsTester.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        manifestReady = true;
        if (!latencyMs) latencyMs = Math.round(performance.now() - started);
        const levels = Array.from(data?.levels || hlsTester?.levels || []);
        const bestLevel = levels
          .filter((level) => Number(level?.width || level?.height || 0) > 0)
          .sort((a, b) => Number(b.height || 0) - Number(a.height || 0))[0];
        if (bestLevel) {
          manifestWidth = Number(bestLevel.width || 0);
          manifestHeight = Number(bestLevel.height || 0);
        }
        schedulePlayableFallback(5500);
        try {
          hlsTester.startLoad(0);
          const playPromise = video.play?.();
          if (playPromise?.catch) playPromise.catch(() => {});
        } catch {
          // Loading the media buffer is still useful even when play() is blocked.
        }
      });
      hlsTester.on(Hls.Events.FRAG_LOADING, () => {
        fragStart = performance.now();
      });
      hlsTester.on(Hls.Events.FRAG_LOADED, (_event, data) => {
        const stats = data?.frag?.stats || data?.part?.stats || data?.stats || {};
        const loading = stats.loading || {};
        const loadStart = Number(loading.start || 0);
        const payloadSize = Number(data?.payload?.byteLength || data?.payload?.length || 0);
        const size = Number(stats.loaded || stats.total || payloadSize || 0);
        if (size > 0) {
          if (!probeStartedAt) {
            probeStartedAt = fragStart || performance.now();
          }
          probeBytes += size;
          probeFragments += 1;
          if (probeBytes < minProbeBytes && probeFragments < maxProbeFragments) {
            return;
          }
          const loadTime = Math.max(performance.now() - probeStartedAt, 1);
          speedKbps = (probeBytes / 1024) / (loadTime / 1000);
          if (loading.first && loadStart && !latencyMs) {
            latencyMs = Math.max(Math.round(Number(loading.first) - loadStart), 0);
          }
          maybeFinish();
        }
      });
      hlsTester.on(Hls.Events.ERROR, (_event, data) => {
        if (!data?.fatal) return;
        if (recoveries < 2 && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          recoveries += 1;
          hlsTester.startLoad();
          return;
        }
        if (recoveries < 2 && data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          recoveries += 1;
          hlsTester.recoverMediaError();
          return;
        }
        if (metadataReady || probeBytes > 0) {
          finishPlayable(metadataReady || probeBytes > 0 ? "metadata" : "manifest");
          return;
        }
        finish(failTest("hls_probe_failed", "测速失败", "browser_hls"));
      });
      video.onloadedmetadata = () => {
        metadataReady = true;
        if (!latencyMs) latencyMs = Math.round(performance.now() - started);
        schedulePlayableFallback(1200);
        maybeFinish();
      };
      video.oncanplay = () => {
        metadataReady = true;
        if (!latencyMs) latencyMs = Math.round(performance.now() - started);
        schedulePlayableFallback(600);
        maybeFinish();
      };
      video.onerror = () => {
        if (metadataReady || probeBytes > 0) {
          finishPlayable("metadata");
          return;
        }
        finish(failTest("hls_media_error", "测速失败", "browser_hls"));
      };
      hlsTester.loadSource(playbackUrl);
      hlsTester.attachMedia(video);
    });
  }

  async function measureNativeStream(url) {
    const started = performance.now();
    const controller = window.AbortController ? new AbortController() : null;
    const timeoutMs = 9000;
    const timer = window.setTimeout(() => controller?.abort(), timeoutMs);
    try {
      const response = await timedPromise(
        fetch(url, {
          method: "GET",
          headers: { Range: "bytes=0-524287" },
          ...(controller ? { signal: controller.signal } : {})
        }),
        timeoutMs,
        () => controller?.abort()
      );
      if (!response.ok && response.status !== 206) throw new Error("bad_status");
      const latencyMs = Math.round(performance.now() - started);
      const remainingMs = Math.max(timeoutMs - (performance.now() - started), 1000);
      const bytes = await timedPromise(
        readResponseSample(response, 512 * 1024),
        remainingMs,
        () => controller?.abort()
      );
      if (!bytes) throw new Error("empty_response");
      const elapsed = Math.max(performance.now() - started, 1);
      const speedKbps = (bytes / 1024) / (elapsed / 1000);
      return okTest("未知", 0, latencyMs, speedKbps, { measuredBy: "browser_range" });
    } catch {
      return measureMediaElementPlayback(url, "browser_media_element");
    } finally {
      window.clearTimeout(timer);
    }
  }

  function measureMediaElementPlayback(url, measuredBy) {
    return new Promise((resolve) => {
      const started = performance.now();
      const video = document.createElement("video");
      video.muted = true;
      video.preload = "auto";
      video.playsInline = true;
      video.style.cssText = "position:fixed;width:1px;height:1px;left:-9999px;top:-9999px;opacity:0;pointer-events:none;";
      document.body.appendChild(video);
      let resolved = false;
      const finish = (test) => {
        if (resolved) return;
        resolved = true;
        window.clearTimeout(timer);
        try {
          video.pause();
          video.removeAttribute("src");
          video.load();
        } catch {
          // Ignore cleanup issues from detached probe media.
        }
        video.remove();
        resolve(test);
      };
      const finishPlayable = () => {
        const dimensions = probeDimensions(video, 0, 0);
        const latencyMs = Math.round(performance.now() - started);
        finish(playableFallbackTest(dimensions.quality, dimensions.height, latencyMs, "metadata", measuredBy));
      };
      const timer = window.setTimeout(() => {
        if (video.readyState >= 1) {
          finishPlayable();
          return;
        }
        finish(failTest("media_probe_timeout", "测速超时", measuredBy));
      }, 9000);
      video.addEventListener("loadedmetadata", finishPlayable, { once: true });
      video.addEventListener("canplay", finishPlayable, { once: true });
      video.addEventListener("playing", finishPlayable, { once: true });
      video.onerror = () => finish(failTest("media_probe_failed", "测速失败", measuredBy));
      video.src = url;
      video.load();
      try {
        const playPromise = video.play?.();
        if (playPromise?.catch) playPromise.catch(() => {});
      } catch {
        // load() can still complete without autoplay.
      }
    });
  }

  function timedPromise(promise, timeoutMs, onTimeout) {
    let timer = 0;
    const timeout = new Promise((_resolve, reject) => {
      timer = window.setTimeout(() => {
        if (typeof onTimeout === "function") onTimeout();
        reject(new Error("timeout"));
      }, Math.max(Number(timeoutMs || 0), 1));
    });
    return Promise.race([promise, timeout]).finally(() => window.clearTimeout(timer));
  }

  async function readResponseSample(response, sampleBytes) {
    const limit = Math.max(Number(sampleBytes || 0), 1);
    if (!response.body || typeof response.body.getReader !== "function") {
      const buffer = await response.arrayBuffer();
      return Math.min(buffer.byteLength, limit);
    }
    const reader = response.body.getReader();
    let total = 0;
    try {
      while (total < limit) {
        const { value, done } = await reader.read();
        if (done) break;
        total += Number(value?.byteLength || value?.length || 0);
      }
    } finally {
      try {
        await reader.cancel();
      } catch {
        // The stream may already be closed.
      }
    }
    return Math.min(total, limit);
  }

  function probeDimensions(video, fallbackWidth, fallbackHeight) {
    const width = Number(video?.videoWidth || fallbackWidth || 0);
    const height = Number(video?.videoHeight || fallbackHeight || widthToHeight(width));
    return {
      width,
      height,
      quality: resolutionToQuality(width, height)
    };
  }

  function updateCandidate(candidate) {
    const list = Array.from(candidateList.__candidates || []);
    const key = `${candidate.source}+${candidate.id}`;
    const index = list.findIndex((item) => `${item.source}+${item.id}` === key);
    if (index >= 0) {
      list[index] = candidate;
      candidateList.__candidates = list;
      renderCandidates(list);
    }
  }

  function rankCandidates(candidates) {
    const list = Array.from(candidates || []);
    const okItems = list.filter((candidate) => candidate.test?.ok);
    const maxSpeed = Math.max(...okItems.map((candidate) => Number(candidate.test.speed_kbps || 0)), 1);
    const latencies = okItems.map((candidate) => Number(candidate.test.latency_ms || 0)).filter((value) => value > 0);
    const minLatency = latencies.length ? Math.min(...latencies) : 0;
    const maxLatency = latencies.length ? Math.max(...latencies) : 1;
    list.forEach((candidate) => {
      const test = candidate.test || {};
      if (!test.ok) {
        test.score = 0;
        return;
      }
      const speedScore = Math.min(100, (Number(test.speed_kbps || 0) / maxSpeed) * 100);
      const qualityScore = qualityScoreValue(test.quality);
      const latencyScore = maxLatency === minLatency
        ? 100
        : ((maxLatency - Number(test.latency_ms || maxLatency)) / (maxLatency - minLatency)) * 100;
      test.score = Math.round((speedScore * 0.7 + qualityScore * 0.2 + latencyScore * 0.1) * 100) / 100;
    });
    return list.sort((a, b) => Number(b.test?.score || 0) - Number(a.test?.score || 0));
  }

  function postSpeedResults(candidates) {
    fetch("/api/source-metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidates })
    }).catch(() => {});
  }

  function tryNextTestedCandidate() {
    const currentKey = `${currentSource}+${currentId}`;
    const next = Array.from(candidateList.__candidates || [])
      .filter((candidate) => candidate.test?.ok)
      .find((candidate) => `${candidate.source}+${candidate.id}` !== currentKey);
    if (next) {
      setStatus(`当前源较慢，切换 ${next.source_name}`);
      applyCandidate(next, true);
    }
  }

  async function saveProgress() {
    if (!player.duration || player.currentTime < 1) return;
    const poster = pickRecordPoster();
    const record = {
      source: currentSource,
      id: currentId,
      title: detail.title,
      source_name: detail.source_name,
      year: detail.year,
      cover: poster.poster,
      poster: poster.poster,
      raw_poster: poster.raw_poster,
      source_poster: poster.source_poster,
      poster_source: poster.poster_source,
      episode_index: currentEpisode,
      total_episodes: detail.episodes.length,
      play_time: Math.floor(player.currentTime),
      total_time: Math.floor(player.duration),
      search_title: cfg.detail.title,
      save_time: Date.now()
    };
    saveLocalProgress(record);
  }

  function pickRecordPoster() {
    const items = [cfg.originalDetail, cfg.detail, detail].filter(Boolean);
    const doubanItem = items.find((item) => (
      item.raw_poster ||
      item.poster_source === "douban" ||
      String(item.poster || item.cover || "").includes("doubanio.com")
    ));
    const posterItem = doubanItem || items.find((item) => item.poster || item.cover) || {};
    const sourcePoster = posterItem.source_poster || items.map((item) => item.source_poster || "").find(Boolean) || "";
    const poster = posterItem.poster || posterItem.cover || sourcePoster || "";
    const rawPoster = posterItem.raw_poster || "";
    return {
      poster,
      raw_poster: rawPoster,
      source_poster: sourcePoster,
      poster_source: posterItem.poster_source || (rawPoster || poster.includes("doubanio.com") ? "douban" : "")
    };
  }

  function saveLocalProgress(record) {
    try {
      const key = "rikka_continue_records";
      const raw = localStorage.getItem(key);
      const records = raw ? JSON.parse(raw) : [];
      const filtered = Array.isArray(records)
        ? records.filter((item) => `${item.source}+${item.id}` !== `${record.source}+${record.id}`)
        : [];
      filtered.unshift(record);
      localStorage.setItem(key, JSON.stringify(filtered.slice(0, 30)));
    } catch {
      // ignore local storage failures
    }
  }

  function findResumeRecord() {
    const fallbackRecord = cfg.record || null;
    try {
      const raw = localStorage.getItem("rikka_continue_records");
      const records = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(records)) return fallbackRecord;
      const original = cfg.originalDetail || cfg.detail || {};
      const originalKey = `${original.source || ""}+${original.id || ""}`;
      const activeKey = `${detail.source || ""}+${detail.id || ""}`;
      return records
        .filter((record) => record && Number(record.play_time || 0) > 3)
        .sort((a, b) => Number(b.save_time || 0) - Number(a.save_time || 0))
        .find((record) => {
          const key = `${record.source || ""}+${record.id || ""}`;
          return key === activeKey || key === originalKey || normalizeTitle(record.search_title || record.title) === normalizeTitle(original.title || detail.title);
        }) || fallbackRecord;
    } catch {
      return fallbackRecord;
    }
  }

  function currentPlaybackTime() {
    const value = Number(player?.currentTime || 0);
    return Number.isFinite(value) ? value : 0;
  }

  function resumeTimeFor(source, id, episode, handoffTime = 0) {
    if (handoffTime > 1) return handoffTime;
    if (!resumeRecord) return 0;
    const sameSource = `${resumeRecord.source || ""}+${resumeRecord.id || ""}` === `${source || ""}+${id || ""}`;
    const sameTitle = normalizeTitle(resumeRecord.search_title || resumeRecord.title) === normalizeTitle((cfg.originalDetail || cfg.detail || {}).title || detail.title);
    const sameEpisode = Number(resumeRecord.episode_index || 0) === Number(episode || 0);
    return (sameSource || sameTitle) && sameEpisode ? Number(resumeRecord.play_time || 0) : 0;
  }

  function normalizeTitle(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[\s\-_·:：,，.。!！?？()[\]（）【】]+/g, "");
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

  function renderCandidateMetrics(test) {
    if (test && test.kind === "metric") {
      if (!test.tests_total) {
        return '<span class="candidate-metrics">暂无源评分</span>';
      }
      const score = Number.isFinite(Number(test.source_score)) ? Number(test.source_score).toFixed(0) : "0";
      const rate = Number.isFinite(Number(test.success_rate)) ? `${Math.round(Number(test.success_rate) * 100)}%` : "0%";
      return `<span class="candidate-metrics">源评分 ${score} · 成功 ${rate} · ${Number(test.tests_total)} 次</span>`;
    }
    if (!test.ok) {
      const label = escapeHtml(test.error_label || "测速失败");
      return `<span class="candidate-metrics error"><span class="status-dot"></span>${label}</span>`;
    }
    const quality = escapeHtml(test.quality || "未知");
    const speed = escapeHtml(test.speed_label || "未知");
    const latency = Number.isFinite(Number(test.latency_ms)) ? `${Number(test.latency_ms)}ms` : "未知";
    const score = Number.isFinite(Number(test.score)) ? Number(test.score).toFixed(0) : "0";
    return `<span class="candidate-metrics">${quality} · ${speed} · ${latency} · ${score}</span>`;
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function withMetricTest(candidate, metric) {
    return {
      ...candidate,
      selected_episode: currentEpisode,
      test: metric ? { kind: "metric", ...metric } : { kind: "metric", tests_total: 0 }
    };
  }

  function playableFallbackTest(quality, height, latencyMs, confidence, measuredBy) {
    const fallbackSpeed = confidence === "manifest" ? 256 : 1024;
    const test = okTest(quality, height, latencyMs, fallbackSpeed, {
      measuredBy,
      playableOnly: true,
      speedLabel: "可播放"
    });
    test.probe_confidence = confidence;
    return test;
  }

  function okTest(quality, height, latencyMs, speedKbps, options = {}) {
    const cappedSpeedKbps = normalizedSpeedKbps(speedKbps);
    return {
      ok: true,
      error: "",
      error_label: "",
      quality: quality || "未知",
      height: height || 0,
      latency_ms: Math.max(Math.round(Number(latencyMs || 0)), 0),
      speed_kbps: cappedSpeedKbps,
      raw_speed_kbps: Math.round(Number(speedKbps || 0) * 10) / 10,
      speed_label: options.speedLabel || speedLabel(cappedSpeedKbps),
      score: 0,
      measured_by: options.measuredBy || "browser_probe",
      playable_only: Boolean(options.playableOnly)
    };
  }

  function failTest(error = "browser_probe_failed", errorLabel = "测速失败", measuredBy = "browser_probe") {
    return {
      ok: false,
      error,
      error_label: errorLabel,
      quality: "未知",
      height: 0,
      latency_ms: 0,
      speed_kbps: 0,
      speed_label: "失败",
      score: 0,
      measured_by: measuredBy
    };
  }

  function speedLabel(speedKbps) {
    if (speedKbps >= 1024) return `${(speedKbps / 1024).toFixed(1)} MB/s`;
    return `${Math.round(speedKbps)} KB/s`;
  }

  function normalizedSpeedKbps(speedKbps) {
    const speed = Math.max(Number(speedKbps || 0), 0);
    const cap = Math.max(Number(cfg.browserSpeedCapKbps || 12288), 1024);
    return Math.round(Math.min(speed, cap) * 10) / 10;
  }

  function widthToQuality(width) {
    if (width >= 3840) return "4K";
    if (width >= 2560) return "2K";
    if (width >= 1920) return "1080p";
    if (width >= 1280) return "720p";
    if (width >= 854) return "480p";
    if (width > 0) return "SD";
    return "未知";
  }

  function resolutionToQuality(width, height) {
    const resolvedHeight = Number(height || 0);
    if (resolvedHeight >= 2160) return "4K";
    if (resolvedHeight >= 1440) return "2K";
    if (resolvedHeight >= 1080) return "1080p";
    if (resolvedHeight >= 720) return "720p";
    if (resolvedHeight >= 480) return "480p";
    return widthToQuality(Number(width || 0));
  }

  function widthToHeight(width) {
    if (width >= 3840) return 2160;
    if (width >= 2560) return 1440;
    if (width >= 1920) return 1080;
    if (width >= 1280) return 720;
    if (width >= 854) return 480;
    return 0;
  }

  function qualityScoreValue(quality) {
    if (quality === "4K") return 100;
    if (quality === "2K") return 85;
    if (quality === "1080p") return 75;
    if (quality === "720p") return 60;
    if (quality === "480p") return 40;
    if (quality === "SD") return 20;
    return 0;
  }

  speedTestButton?.addEventListener("click", runPreference);
  setupCastButton();
  mobileTabs.forEach((tab) => {
    tab.addEventListener("click", () => setMobilePanel(tab.dataset.mobilePanel || "episodes"));
  });
  setupLongPressFastForward();
  setupDesktopClickPlayback();
  setupDesktopKeyboardControls();
  player?.addEventListener("pause", saveProgress);
  player?.addEventListener("loadstart", () => setPlayerOverlay("正在加载视频"));
  player?.addEventListener("loadedmetadata", () => {
    maybeSkipOpening();
  });
  player?.addEventListener("timeupdate", () => {
    notePlaybackActive();
    maybeSkipEnding();
  });
  player?.addEventListener("playing", () => {
    notePlaybackActive();
    clearLoadTimer();
    setPlayerOverlay("", false);
  });
  player?.addEventListener("canplay", () => {
    clearLoadTimer();
    setPlayerOverlay("", false);
  });
  player?.addEventListener("error", () => setPlayerOverlay("当前源加载失败，可点击测速换源"));
  document.addEventListener("fullscreenchange", handleFullscreenChange);
  document.addEventListener("webkitfullscreenchange", handleFullscreenChange);
  if (art) {
    art.on("pause", saveProgress);
    art.on("fullscreen", handleFullscreenChange);
    art.on("video:playing", () => {
      notePlaybackActive();
      clearLoadTimer();
      setPlayerOverlay("", false);
    });
    art.on("video:canplay", () => {
      clearLoadTimer();
      setPlayerOverlay("", false);
    });
    art.on("video:error", () => setPlayerOverlay("当前源加载失败，可点击测速换源"));
    art.on("video:ended", playNextEpisode);
  } else {
    player?.addEventListener("ended", playNextEpisode);
  }
  window.addEventListener("beforeunload", saveProgress);
  setInterval(saveProgress, 5000);

  renderEpisodes();
  setMobilePanel("episodes");
  initPlayback();
})();
