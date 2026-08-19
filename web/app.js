(function startProfAgentDemo() {
  "use strict";

  const fixtures = window.PROFAGENT_FIXTURES;
  const dialogueRuntime = window.PROFAGENT_DIALOGUE_RUNTIME;
  const imageAssetRuntime = window.PROFAGENT_IMAGE_ASSET_RUNTIME;
  const recommendationPreviewRuntime = window.PROFAGENT_RECOMMENDATION_PREVIEW_RUNTIME;
  if (!dialogueRuntime) throw new Error("Dialogue runtime 未加载");
  if (!imageAssetRuntime) throw new Error("Image asset runtime 未加载");
  if (!recommendationPreviewRuntime) throw new Error("Recommendation preview runtime 未加载");
  const dialogueGuard = dialogueRuntime.createInFlightGuard();
  const dialogueTraceGuard = dialogueRuntime.createInFlightGuard();
  const recommendationPreviewGuard = dialogueRuntime.createInFlightGuard();
  const userId = "u01";
  const UI_BUILD_VERSION = "s14-recommendation-preview-accordion-20260819";
  const DEFAULT_REQUEST_TIMEOUT_MS = 12000;
  const DIALOGUE_TURN_TIMEOUT_MS = 125000;
  const PREVIEW_2D_TIMEOUT_MS = 50000;
  const RECOMMENDATION_PREVIEW_TIMEOUT_MS = 125000;
  const state = {
    source: "checking",
    health: null,
    team: null,
    wardrobe: null,
    wardrobeCatalogAssets: new Map(),
    wardrobeCatalogManifestState: "loading",
    scene: null,
    recommendation: null,
    trace: null,
    activeOutfitId: null,
    debug: false,
    stylingSessionId: null,
    conversationMode: null,
    pendingQuestionStatus: "none",
    suggestedReplies: [],
    pendingDialogueRetry: null,
    turnIndex: 0,
    historyVersion: 0,
    dialogueProvider: null,
    localReplacements: [],
    directionFeedback: new Map(),
    recommendationPreviews: new Map(),
    activeLook: null,
    lookVersions: [],
    lookComparisons: new Map(),
    lookDecisionLedger: [],
    lookDecisionLedgerComplete: true,
    assets: [],
    unboundAssets: [],
    deletedAssetIds: new Set(),
    assetPreviewUrl: null,
    assetPreviewAssetId: null,
    preview2d: null,
    preview2dPending: false,
    scorecard: null,
    adjustments: [],
    rejectedCanonical: new Set(),
    finalized: false,
    finalization: null,
    modifyOptions: [],
    memoryProposals: [],
    memoryRecords: [],
    memoryAvailable: false,
    traceHistory: []
  };
  let dialogueWaitTimer = null;
  let dialogueWaitStartedAt = 0;

  const scoreDimensions = [
    ["occasion_fit", "场景适配"],
    ["expression_match", "表达目标匹配"],
    ["overall_harmony", "整体协调"],
    ["silhouette_layering", "衣物轮廓与层次"],
    ["comfort_practicality", "舒适与实用"],
    ["detail_finish", "细节完成度"]
  ];
  const canonicalAdjustmentLabels = Object.freeze({
    trouser_cuff_single: "裤脚单次窄卷边",
    layer_alignment: "整理上下装与外层的衔接",
    accessory_simplify: "简化重点配饰",
    shoe_finish_cleanup: "整理鞋履细节",
    user_modified: "用户自行修改"
  });
  const adjustmentDecisionLabels = Object.freeze({
    accept: "接受",
    partial: "部分接受",
    reject: "拒绝",
    user_modified: "用户修改"
  });

  const slotLabels = {
    outer: "外套", top: "上装", bottom: "下装", dress: "连衣裙",
    shoes: "鞋", bag: "包", accessory: "配饰"
  };
  const statusLabels = {
    available: "可用", laundry: "清洗中", reserved: "已预留", unavailable: "不可用"
  };
  const evidenceRegionLabels = {
    outer: "外套区域", top: "上装区域", bottom: "下装区域", dress: "连衣裙区域",
    shoes: "鞋履区域", bag: "包袋区域", accessory: "配饰区域", layering: "层次区域",
    overall: "整体穿搭区域", silhouette: "衣物轮廓区域", details: "细节区域"
  };
  const approvedMemoryTemplates = Object.freeze({
    constraint: Object.freeze(["不穿高跟鞋", "不穿裙装", "久走或长时间站立时需要舒适鞋履"]),
    comfort_constraint: Object.freeze(["久走或长时间站立时需要舒适鞋履"]),
    preference: Object.freeze(["偏爱直筒裤", "偏爱简洁风格", "偏爱低调配色", "长时间站立时优先选择适合久走的鞋"]),
    profile_stable: Object.freeze(["不穿高跟鞋", "不穿裙装", "偏爱直筒裤"]),
    feedback: Object.freeze(["偏好久走与长时间站立时选择舒适鞋履"])
  });
  const fixedDimensionEvidence = Object.freeze({
    occasion_fit: "仅核对衣物元数据与场合要求。",
    expression_match: "仅核对搭配风格标签与本次表达目标。",
    overall_harmony: "仅核对衣物之间的颜色与风格组合。",
    silhouette_layering: "仅核对上装、下装、鞋履与外层的组合结构。",
    comfort_practicality: "仅核对材质、松量、天气与活动需求元数据。",
    detail_finish: "仅核对鞋履、包袋、配饰与外层的搭配完整度。"
  });
  const seasonLabels = { spring: "春", summer: "夏", autumn: "秋", winter: "冬", all: "四季" };
  const occasionLabels = {
    interview: "面试", meeting: "会议", commute: "通勤", travel: "旅行",
    date: "约会", party: "聚会", outdoor: "户外", sports: "运动", daily: "日常", home: "居家"
  };
  const horizonLabels = { now: "马上", today: "今天", soon: "近期", planned: "计划中", unknown: "未知（按高急切度）" };
  const urgencyLabels = { high: "高 · 禁止购物", medium: "中", low: "低" };
  const colorValues = {
    black: "#2b3032", white: "#dfe3df", gray: "#8d9693", navy: "#26394d",
    beige: "#c7b99d", brown: "#79604f", khaki: "#a49a73", red: "#985044",
    pink: "#c29096", orange: "#bb7449", yellow: "#c5a448", green: "#547461",
    blue: "#54768f", purple: "#726181", multi: "#7f8c84"
  };

  const byId = (id) => document.getElementById(id);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  function showToast(message) {
    const toast = byId("toast");
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 2800);
  }

  function controlledRequestError(code, message) {
    const error = new Error(message);
    error.name = "ProfAgentRequestError";
    error.code = code;
    return error;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, signal: externalSignal, ...requestOptions } = options;
    const isFormData = requestOptions.body instanceof FormData;
    let timedOut = false;
    const abortFromExternalSignal = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener("abort", abortFromExternalSignal, { once: true });
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      const response = await fetch(path, {
        ...requestOptions,
        headers: { ...(isFormData ? {} : { "Content-Type": "application/json" }), ...(requestOptions.headers || {}) },
        signal: controller.signal
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = payload?.error?.message || payload?.detail || `API ${response.status}`;
        throw new Error(message);
      }
      return payload;
    } catch (error) {
      if (timedOut || error?.name === "TimeoutError") {
        throw controlledRequestError("REQUEST_TIMEOUT", "等待服务端响应超时，本次未采用任何未验证结果。");
      }
      if (error?.name === "AbortError") {
        throw controlledRequestError("REQUEST_ABORTED", "请求已中止，本次未采用任何未验证结果。");
      }
      if (error instanceof TypeError) {
        throw controlledRequestError("NETWORK_UNAVAILABLE", "本次请求暂时无法连接服务端，未采用任何未验证结果。");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
      externalSignal?.removeEventListener("abort", abortFromExternalSignal);
    }
  }

  function setSource(source, detail) {
    state.source = source;
    const badge = byId("source-badge");
    const title = byId("source-title");
    const description = byId("source-detail");
    const retry = byId("retry-api");
    badge.dataset.state = source;
    if (source === "api") {
      badge.textContent = "服务在线";
      title.textContent = "Stylist 已准备好";
      description.textContent = detail || "每轮回复都会标明是 CPA 生成还是本地回复。";
      retry.hidden = true;
    } else if (source === "fixture") {
      badge.textContent = "仅浏览";
      title.textContent = "Stylist 暂时无法回应";
      description.textContent = detail || "衣橱演示仍可浏览；恢复服务后可继续同一任务。";
      retry.hidden = false;
    } else {
      badge.textContent = "正在连接";
      title.textContent = "正在联系 Stylist";
      description.textContent = "连接状态确认后即可开始对话。";
      retry.hidden = true;
    }
  }

  async function connect() {
    setSource("checking");
    state.wardrobeCatalogAssets = new Map();
    state.wardrobeCatalogManifestState = "loading";
    if (state.wardrobe) renderWardrobe();
    try {
      const [health, team, wardrobe, catalogManifestRaw] = await Promise.all([
        api("/health"),
        api(`/team/home?user_id=${encodeURIComponent(userId)}`),
        api(`/wardrobe?user_id=${encodeURIComponent(userId)}`),
        api(`/wardrobe/catalog-assets?user_id=${encodeURIComponent(userId)}`).catch(() => null)
      ]);
      state.health = health;
      state.team = team;
      state.wardrobe = wardrobe;
      try {
        const manifest = imageAssetRuntime.normalizeCatalogManifest(
          catalogManifestRaw,
          userId,
          (wardrobe.items || []).map((item) => item.garment_id)
        );
        state.wardrobeCatalogAssets = manifest.assets;
        state.wardrobeCatalogManifestState = "ready";
      } catch (_error) {
        state.wardrobeCatalogAssets = new Map();
        state.wardrobeCatalogManifestState = "unavailable";
      }
      const llm = health.providers?.llm;
      const cpaModelReady = dialogueRuntime.hasExactCpaModel(llm)
        && llm?.enabled === true
        && llm.available === true
        && llm.chat_model_verified === true;
      let detail = "服务在线；每轮回复会单独标明生成来源。";
      if (llm?.enabled === false) {
        detail = "CPA 对话当前关闭；对话仍可使用本地回复。";
      } else if (llm?.enabled === true && llm.available !== true) {
        detail = "CPA 对话当前未就绪；对话仍可使用本地回复。";
      } else if (
        llm?.enabled === true
        && !cpaModelReady
      ) {
        detail = "CPA 对话模型尚未验证；对话仍可使用本地回复。";
      } else if (
        cpaModelReady
      ) {
        detail = "CPA 智能对话可用；每轮生成来源都会显示在消息下方。";
      }
      setSource("api", detail);
      byId("conversation-origin").textContent = cpaModelReady
        ? "CPA 对话已就绪"
        : "本地回复已就绪";
    } catch (error) {
      state.health = fixtures.health;
      state.team = fixtures.teamHome;
      state.wardrobe = fixtures.wardrobe;
      state.wardrobeCatalogAssets = new Map();
      state.wardrobeCatalogManifestState = "unavailable";
      setSource("fixture", "服务暂时不可用；当前只开放衣橱演示浏览，不生成未验证回复。");
      byId("conversation-origin").textContent = "对话服务未连接";
    }
    renderTeam();
    renderWardrobe();
    renderDebug();
    if (state.source === "api") await loadMemory();
    else setMemoryAvailability(false, "离线 fixture 只读，记忆 API 不可用");
  }

  function renderTeam() {
    const team = state.team || fixtures.teamHome;
    const member = team.members?.[0] || {};
    byId("team-name").textContent = team.team?.name || team.team?.team_id || "personal_team";
    byId("team-description").textContent = team.team?.description || "围绕你的具体任务协作；R1 当前只上线一位专业成员。";
    byId("team-member-count").textContent = `${(team.members || []).filter((item) => item.status === "available").length} 位已上线`;
    byId("member-name").textContent = member.name || "你的私人明星穿搭师";
    byId("member-boundary").textContent = member.boundary || "只处理穿搭、衣橱与造型共创。";
    const capabilityList = byId("member-capabilities");
    capabilityList.replaceChildren(...(member.capabilities || []).map((item) => make("span", "chip", item.replaceAll("_", " "))));
    const boundary = byId("unavailable-capabilities");
    const unavailable = team.unavailable_capabilities || [];
    boundary.replaceChildren(...unavailable.map((item) => {
      const name = typeof item === "string" ? item : item.name;
      return make("span", "chip", name.includes("未上线") ? name : `${name} · 未上线`);
    }));
  }

  function wardrobeItems() {
    return state.wardrobe?.items || [];
  }

  function garmentMap() {
    return new Map(wardrobeItems().map((item) => [item.garment_id, item]));
  }

  function renderWardrobe() {
    const slot = byId("filter-slot").value;
    const status = byId("filter-status").value;
    const season = byId("filter-season").value;
    const items = wardrobeItems().filter((item) =>
      (slot === "all" || item.slot === slot) &&
      (status === "all" || item.status === status) &&
      (season === "all" || (item.seasons || []).includes(season) || (item.seasons || []).includes("all"))
    );
    byId("wardrobe-visible-count").textContent = String(items.length);
    const catalogStatus = byId("wardrobe-catalog-status");
    const readyCount = items.filter((item) => state.wardrobeCatalogAssets.get(item.garment_id)?.displayable === true).length;
    catalogStatus.textContent = state.wardrobeCatalogManifestState === "loading"
      ? "正在核对 AI 目录图清单；衣物元数据可正常浏览。"
      : state.wardrobeCatalogManifestState === "ready"
        ? `当前筛选有 ${readyCount} 件可显示的 AI 目录参考；其余继续显示元数据卡。`
        : "目录图清单暂不可用；当前继续显示完整衣物元数据卡。";
    const grid = byId("wardrobe-grid");
    if (!items.length) {
      grid.replaceChildren(make("p", "empty-state", "当前筛选下没有衣物；系统不会用非法单品补位。"));
      return;
    }
    const createGarmentCard = (item) => {
      const card = make("article", "garment-card");
      const visual = make("div", "garment-visual");
      const swatch = make("div", "garment-swatch");
      swatch.style.setProperty("--garment-color", colorValues[item.color] || colorValues.multi);
      swatch.append(make("span", "garment-slot-icon", (slotLabels[item.slot] || item.slot).slice(0, 1)));
      visual.append(swatch);
      const catalogAsset = state.wardrobeCatalogAssets.get(item.garment_id);
      let catalogStateText = "暂无目录图 · 显示元数据";
      if (catalogAsset?.status === "failed") catalogStateText = "目录图未完成 · 显示元数据";
      let catalogImage = null;
      let catalogBadge = null;
      if (catalogAsset?.displayable === true) {
        catalogImage = make("img", "garment-catalog-image");
        catalogBadge = make("span", "garment-catalog-badge", "AI 生成目录参考");
        catalogImage.alt = `${item.name} · AI 生成目录参考`;
        catalogImage.decoding = "async";
        catalogImage.referrerPolicy = "no-referrer";
        visual.append(catalogImage, catalogBadge);
        catalogStateText = "目录图正在读取 · 元数据可用";
      }
      const content = make("div", "garment-content");
      content.append(make("h3", "", item.name));
      const meta = make("div", "garment-meta");
      meta.append(
        make("span", "", slotLabels[item.slot] || item.slot),
        make("span", "", statusLabels[item.status] || item.status),
        make("span", "", (item.seasons || []).map((value) => seasonLabels[value] || value).join(" / "))
      );
      const catalogState = make("p", "garment-catalog-state", catalogStateText);
      content.append(meta, catalogState);
      if (state.debug) content.append(make("code", "garment-id", item.garment_id));
      card.append(visual, content);
      if (catalogImage && catalogBadge) {
        imageAssetRuntime.bindCatalogImageLifecycle({
          card,
          image: catalogImage,
          badge: catalogBadge,
          swatch,
          statusNode: catalogState,
          imageUrl: catalogAsset.image_url,
          successText: `AI 生成目录参考 · ${catalogAsset.provenance_label}`,
          failureText: "目录图读取失败 · 显示元数据"
        });
      }
      return card;
    };
    const slotOrder = Object.keys(slotLabels);
    const grouped = slotOrder
      .map((slotName) => [slotName, items.filter((item) => item.slot === slotName)])
      .filter(([, categoryItems]) => categoryItems.length > 0);
    grid.replaceChildren(...grouped.map(([slotName, categoryItems]) => {
      const details = make("details", "wardrobe-category");
      details.dataset.slot = slotName;
      const summary = make("summary", "wardrobe-category-summary");
      summary.append(
        make("span", "wardrobe-category-name", slotLabels[slotName]),
        make("span", "wardrobe-category-count", `${categoryItems.length} 件`)
      );
      const categoryGrid = make("div", "wardrobe-category-grid");
      categoryGrid.dataset.rendered = "false";
      details.append(summary, categoryGrid);
      details.addEventListener("toggle", () => {
        if (!details.open || categoryGrid.dataset.rendered === "true") return;
        categoryGrid.replaceChildren(...categoryItems.map(createGarmentCard));
        categoryGrid.dataset.rendered = "true";
      });
      return details;
    }));
  }

  function isCompleteIds(ids) {
    const map = garmentMap();
    const slots = ids.map((id) => map.get(id)?.slot).filter(Boolean);
    const dressPath = slots.includes("dress") && !slots.includes("top") && !slots.includes("bottom");
    const separatesPath = !slots.includes("dress") && slots.includes("top") && slots.includes("bottom");
    return slots.includes("shoes") && (dressPath || separatesPath);
  }

  function hardenScene(scene) {
    const highHorizon = ["now", "today", "unknown"].includes(scene?.event_horizon || "unknown");
    const urgencyGuard = highHorizon || scene?.urgency === "high";
    return {
      ...scene,
      urgency: highHorizon ? "high" : scene.urgency,
      shopping_allowed: urgencyGuard ? false : scene.shopping_allowed === true,
      ui_capabilities: { ...(scene.ui_capabilities || {}), shopping_cta: urgencyGuard ? false : scene.ui_capabilities?.shopping_cta === true }
    };
  }

  function isShoppingGuarded(scene) {
    return ["now", "today", "unknown"].includes(scene?.event_horizon) || scene?.urgency === "high" || scene?.shopping_allowed !== true || scene?.ui_capabilities?.shopping_cta !== true;
  }

  function tabooColors(scene) {
    const aliases = { "绿色": "green", "绿": "green", "紫色": "purple", "紫": "purple", "红色": "red", "红": "red", "黄色": "yellow", "黄": "yellow", "黑色": "black", "黑": "black", "白色": "white", "白": "white", "蓝色": "blue", "蓝": "blue", "粉色": "pink", "粉": "pink", "棕色": "brown", "棕": "brown", "灰色": "gray", "灰": "gray" };
    return new Set((scene?.constraints?.taboo_colors || []).map((value) => aliases[String(value).toLowerCase()] || String(value).toLowerCase()));
  }

  function isAllowedGarment(id, scene) {
    const item = garmentMap().get(id);
    const excluded = new Set(scene?.constraints?.excluded_items || []);
    return Boolean(item) && item.user_id === userId && item.status === "available" && !excluded.has(id) && !tabooColors(scene).has(String(item.color).toLowerCase());
  }

  function alternativeIds(value) {
    const values = Array.isArray(value) ? value : [value];
    return values.map((item) => typeof item === "string" ? item : item?.garment_id || item?.item_id || item?.id).filter(Boolean);
  }

  function sanitizeRecommendation(scene, recommendation) {
    const map = garmentMap();
    const safeOutfits = (recommendation.outfits || []).slice(0, 3).map((outfit) => {
      const rawIds = (outfit.items || []).map((item) => typeof item === "string" ? item : item?.garment_id || item?.item_id || item?.id).filter(Boolean);
      const items = rawIds.filter((id) => isAllowedGarment(id, scene));
      const alternatives = {};
      Object.entries(outfit.alternatives || {}).forEach(([from, values]) => {
        if (!items.includes(from)) return;
        const fromSlot = map.get(from)?.slot;
        const safe = alternativeIds(values).filter((id) => {
          if (!isAllowedGarment(id, scene) || map.get(id)?.slot !== fromSlot || id === from) return false;
          const candidate = items.map((current) => current === from ? id : current);
          return isCompleteIds(candidate);
        });
        if (safe.length) alternatives[from] = [...new Set(safe)];
      });
      return { ...outfit, items, alternatives };
    }).filter((outfit) => outfit.validation?.all_ids_grounded !== false && outfit.validation?.hard_constraints_passed !== false && outfit.items.length > 0 && isCompleteIds(outfit.items));
    const highGuard = isShoppingGuarded(scene);
    const rawSuggestions = recommendation.shopping_suggestions || [];
    const shoppingCtaAllowed = !highGuard && scene.shopping_allowed === true && scene.ui_capabilities?.shopping_cta === true && recommendation.ui_capabilities?.shopping_cta === true && rawSuggestions.length > 0;
    return {
      ...recommendation,
      outfits: safeOutfits,
      shopping_suggestions: shoppingCtaAllowed ? rawSuggestions : [],
      ui_capabilities: { ...(recommendation.ui_capabilities || {}), shopping_cta: shoppingCtaAllowed }
    };
  }

  function isCpaDialogueReply(provider) {
    return dialogueRuntime.isStrictCpaReply(provider);
  }

  function dialogueReplyLabel(provider, conversationMode) {
    if (conversationMode === "safety_response") return "安全回应";
    return isCpaDialogueReply(provider) ? "CPA 生成" : "本地回复";
  }

  function appendMessage(author, text, user = false, meta = null) {
    const message = make("article", `message ${user ? "message-user" : "message-assistant"}`);
    message.append(make("span", "message-author", author), make("p", "", text));
    if (!user && meta?.provider) {
      const footer = make("div", "message-meta");
      const safetyResponse = meta.conversationMode === "safety_response";
      const generated = !safetyResponse && isCpaDialogueReply(meta.provider);
      const originClass = safetyResponse ? "origin-safety" : generated ? "origin-cpa" : "origin-local";
      const origin = make("span", `origin-badge ${originClass}`, dialogueReplyLabel(meta.provider, meta.conversationMode));
      footer.append(origin);
      if (Number.isInteger(meta.turnIndex)) footer.append(make("span", "turn-label", `第 ${meta.turnIndex} 轮`));
      message.append(footer);
    }
    byId("conversation").append(message);
    message.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function setDialogueInFlightControls(locked) {
    const closed = state.conversationMode === "task_closed";
    byId("scene-input").disabled = locked || closed;
    byId("scene-submit").disabled = locked || closed;
    document.querySelectorAll("[data-example], #suggested-replies button").forEach((control) => {
      control.disabled = locked || closed;
    });
  }

  const dialogueModes = new Set(["stylist_chat", "styling_active", "support_pause", "safety_response", "task_closed"]);
  const dialogueActions = new Set(["chat", "support", "clarify", "recommend", "acknowledge"]);
  const pendingQuestionStates = new Set(["none", "active", "suspended", "resolved", "cancelled"]);
  const sceneHorizons = new Set(["now", "today", "soon", "planned", "unknown"]);
  const sceneUrgencies = new Set(["high", "medium", "low"]);
  const sceneOccasions = new Set(Object.keys(occasionLabels));
  const dialogueCombinationRules = Object.freeze({
    stylist_chat: Object.freeze({
      chat: Object.freeze({ paused: true, pending: Object.freeze(["none"]) })
    }),
    styling_active: Object.freeze({
      clarify: Object.freeze({ paused: true, pending: Object.freeze(["active"]) }),
      recommend: Object.freeze({ paused: false, pending: Object.freeze(["none", "resolved", "cancelled"]) })
    }),
    support_pause: Object.freeze({
      support: Object.freeze({ paused: true, pending: Object.freeze(["none", "suspended", "resolved"]) }),
      acknowledge: Object.freeze({ paused: true, pending: Object.freeze(["none", "suspended", "resolved"]) })
    }),
    safety_response: Object.freeze({
      support: Object.freeze({ paused: true, pending: Object.freeze(["none", "cancelled"]) })
    }),
    task_closed: Object.freeze({
      acknowledge: Object.freeze({ paused: true, pending: Object.freeze(["none", "cancelled"]) })
    })
  });
  const forbiddenTopLevelToolFields = Object.freeze([
    "tool_calls", "tools", "catalog", "catalog_results", "directions", "outfits",
    "shopping_suggestions", "shopping_cta", "look", "scorecard", "adjustments",
    "generated_images", "vision_results"
  ]);
  const controlledSuggestedReplies = new Set(["继续聊聊", "现在开始搭配", "结束"]);
  const suggestedReplyAliases = Object.freeze({
    "继续聊聊": "继续聊聊",
    "现在开始搭": "现在开始搭配",
    "现在开始搭配": "现在开始搭配",
    "结束": "结束",
    "结束本次任务": "结束"
  });
  const suggestedReplyMessages = Object.freeze({
    "继续聊聊": "继续聊聊",
    "现在开始搭配": "现在开始搭配",
    "结束": "结束本次任务"
  });

  function renderSuggestedReplies() {
    const container = byId("suggested-replies");
    const replies = state.suggestedReplies.filter((reply) => controlledSuggestedReplies.has(reply));
    container.hidden = replies.length === 0;
    container.replaceChildren(...replies.map((reply) => {
      const button = make("button", "text-chip suggested-reply", reply);
      button.type = "button";
      button.dataset.reply = reply;
      button.disabled = dialogueGuard.inFlight || state.conversationMode === "task_closed";
      button.addEventListener("click", () => {
        if (dialogueGuard.inFlight) return;
        const input = byId("scene-input");
        input.value = suggestedReplyMessages[reply];
        input.focus();
      });
      return button;
    }));
  }

  function setDialogueMode(mode) {
    const labels = {
      stylist_chat: "正在和 Stylist 聊天",
      styling_active: "穿搭对话进行中",
      support_pause: "已暂停穿搭建议",
      safety_response: "安全回应 · 已暂停穿搭建议",
      task_closed: "本次任务已结束"
    };
    state.conversationMode = mode;
    byId("session-state-label").textContent = labels[mode] || "等待输入";
    const closed = mode === "task_closed";
    setDialogueInFlightControls(dialogueGuard.inFlight || closed);
  }

  function clearRecommendationPresentation() {
    recommendationPreviewGuard.cancel();
    state.recommendationPreviews = new Map();
    state.recommendation = null;
    state.activeOutfitId = null;
    byId("cocreation").hidden = true;
    renderRecommendations();
  }

  function renderScene() {
    const scene = state.scene;
    if (!scene) return;
    byId("scene-panel").hidden = false;
    byId("scene-panel").closest(".studio-layout").classList.remove("is-chat-only");
    const summary = byId("scene-summary");
    const pairs = [
      ["场合", occasionLabels[scene.occasion] || scene.occasion || "待确认"],
      ["发生时间", horizonLabels[scene.event_horizon] || scene.event_horizon],
      ["紧迫度", urgencyLabels[scene.urgency] || scene.urgency],
      ["使用范围", isShoppingGuarded(scene) ? "仅使用现有衣橱" : "先衣橱，明确缺口后可选 Mock 补购"]
    ];
    summary.replaceChildren(...pairs.map(([term, value]) => {
      const row = make("div"); row.append(make("dt", "", term), make("dd", "", value)); return row;
    }));
    byId("scene-source").textContent = "对话中已更新";
    const constraints = scene.constraints || {};
    const chips = [
      ...(constraints.required_slots || []).map((item) => `需要 ${slotLabels[item] || item}`),
      ...(constraints.taboo_colors || []).map((item) => `禁忌色 ${item}`),
      ...(constraints.comfort_notes || []),
      ...(scene.assumptions || [])
    ];
    byId("constraint-chips").replaceChildren(...(chips.length ? chips : ["暂无额外约束"]).map((item) => make("span", "chip", item)));
    const guarded = isShoppingGuarded(scene);
    byId("local-guard").hidden = !guarded;
    byId("session-state-label").textContent = guarded ? "高急切度 · 仅衣橱" : "场景已解析";
    byId("current-task-title").textContent = `${occasionLabels[scene.occasion] || "穿搭"} · ${urgencyLabels[scene.urgency] || scene.urgency}`;
    byId("current-task-detail").textContent = (scene.goals || []).length ? `目标：${scene.goals.join(" · ")}` : "目标待补充";
    byId("session-ownership").textContent = `Stylist 会话 · 已连续 ${state.turnIndex || 1} 轮`;
    byId("task-progress").style.width = "52%";
  }

  function itemChip(id) {
    const item = garmentMap().get(id);
    const label = item?.name || "未解析衣物";
    return make("span", "outfit-item", state.debug ? `${label} · ${id}` : label);
  }

  function listBlock(title, items, className = "direction-block") {
    const block = make("div", className);
    block.append(make("strong", "", title));
    const list = make("ul");
    (items || []).forEach((item) => list.append(make("li", "", item)));
    block.append(list);
    return block;
  }

  function replacementControl(outfit) {
    const wrapper = make("div", "direction-block alternatives-panel");
    const entries = Object.entries(outfit.alternatives || {}).flatMap(([fromId, values]) =>
      alternativeIds(values).map((toId) => ({ fromId, toId }))
    );
    const toggle = make("button", "button button-quiet button-small", entries.length ? `替换入口 · ${entries.length} 个衣橱选项` : "替换入口 · 暂无合法选项");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    const panel = make("div", "alternatives-panel");
    panel.hidden = true;
    if (!entries.length) {
      panel.append(make("p", "", "当前没有同时满足白名单、可用状态、同槽位与整套完整性的替换。"));
    } else {
      entries.forEach(({ fromId, toId }) => {
        const from = garmentMap().get(fromId);
        const to = garmentMap().get(toId);
        const replace = make("button", "button button-secondary button-small", `${from?.name || "当前单品"} → ${to?.name || "衣橱替换"}`);
        replace.type = "button";
        replace.addEventListener("click", () => applyAlternative(outfit.outfit_id, fromId, toId));
        panel.append(replace);
      });
    }
    toggle.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      toggle.setAttribute("aria-expanded", String(!panel.hidden));
    });
    wrapper.append(toggle, panel);
    return wrapper;
  }

  function applyAlternative(outfitId, fromId, toId) {
    const outfits = state.recommendation?.outfits || [];
    const outfit = outfits.find((item) => item.outfit_id === outfitId);
    const offered = alternativeIds(outfit?.alternatives?.[fromId]);
    const from = garmentMap().get(fromId);
    const to = garmentMap().get(toId);
    const candidate = outfit?.items?.map((id) => id === fromId ? toId : id) || [];
    const safe = outfit?.items?.includes(fromId) && offered.includes(toId) && isAllowedGarment(toId, state.scene) && from?.slot === to?.slot && isCompleteIds(candidate);
    if (!safe) {
      showToast("替换未通过衣橱白名单与槽位复核");
      return;
    }
    const updatedRecommendation = {
      ...state.recommendation,
      outfits: outfits.map((item) => item.outfit_id === outfitId ? {
        ...item,
        items: candidate,
        alternatives: {},
        local_replacement_note: `已选择：${from.name} → ${to.name}`
      } : item)
    };
    state.recommendation = sanitizeRecommendation(state.scene, updatedRecommendation);
    state.localReplacements.push({ outfit_id: outfitId, from_id: fromId, to_id: toId, source: "server_alternative_client_validated" });
    appendMessage("Stylist", `已在方向预览中选择：${from.name}改为${to.name}。选定方向时才会由服务端写入新的 Look 版本；其余单品保持不变。`);
    renderRecommendations();
  }

  function directionFeedbackControl(outfit) {
    const wrapper = make("div", "direction-feedback");
    const recorded = state.directionFeedback.get(outfit.outfit_id);
    const clientReplaced = Boolean(outfit.local_replacement_note)
      || state.localReplacements.some((entry) => entry.outfit_id === outfit.outfit_id);
    if (clientReplaced) {
      if (recorded) wrapper.append(make("p", "feedback-success", "服务端反馈仅对应替换前的原始方向。"));
      const actions = make("div", "feedback-actions");
      ["喜欢", "不喜欢"].forEach((label) => {
        const button = make("button", "button button-quiet button-small", label);
        button.type = "button";
        button.disabled = true;
        actions.append(button);
      });
      wrapper.append(
        actions,
        make("p", "feedback-blocked", "此卡包含仅客户端预览的衣橱替换，反馈已禁用。请重新加载原始方向，或先建立服务端 Look 后再反馈；不会发送客户端单品 ID。")
      );
      return wrapper;
    }
    if (recorded) {
      wrapper.append(make("p", "feedback-success", recorded.decision === "like"
        ? "服务端已记录：喜欢这个方向。"
        : `服务端已记录：不喜欢 · ${recorded.reasonLabel}${recorded.memoryScope === "propose" ? "；长期记忆仍待确认。" : "；仅本次会话。"}`));
      return wrapper;
    }

    wrapper.append(make("strong", "feedback-heading", "这个方向对你有帮助吗？"));
    const actions = make("div", "feedback-actions");
    const like = make("button", "button button-quiet button-small", "喜欢");
    like.type = "button";
    const dislike = make("button", "button button-quiet button-small", "不喜欢");
    dislike.type = "button";
    actions.append(like, dislike);

    const panel = make("form", "feedback-form");
    panel.hidden = true;
    const reasonLabel = make("label", "", "主要原因");
    const reason = make("select", "feedback-select");
    [["long_walk_shoes", "鞋不适合久走"]].forEach(([value, label]) => {
      const option = make("option", "", label); option.value = value; reason.append(option);
    });
    reasonLabel.append(reason);
    const scopeLabel = make("label", "", "这条反馈如何使用");
    const scope = make("select", "feedback-select");
    [["session", "仅本次"], ["propose", "长期记住（先进入待确认）"]].forEach(([value, label]) => {
      const option = make("option", "", label); option.value = value; scope.append(option);
    });
    if (!state.memoryAvailable) scope.querySelector('option[value="propose"]').disabled = true;
    scopeLabel.append(scope);
    const submit = make("button", "button button-secondary button-small", "提交不喜欢");
    submit.type = "submit";
    const status = make("p", "feedback-status");
    status.setAttribute("role", "status");
    panel.append(reasonLabel, scopeLabel, submit);

    like.addEventListener("click", () => submitDirectionFeedback({
      outfit,
      decision: "like",
      reasonCode: null,
      reasonLabel: null,
      memoryScope: "session",
      controls: [like, dislike],
      status
    }));
    dislike.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      dislike.setAttribute("aria-expanded", String(!panel.hidden));
    });
    dislike.setAttribute("aria-expanded", "false");
    panel.addEventListener("submit", (event) => {
      event.preventDefault();
      submitDirectionFeedback({
        outfit,
        decision: "dislike",
        reasonCode: reason.value,
        reasonLabel: reason.selectedOptions[0]?.textContent || reason.value,
        memoryScope: scope.value,
        controls: [like, dislike, reason, scope, submit],
        status
      });
    });
    wrapper.append(actions, panel, status);
    return wrapper;
  }

  async function submitDirectionFeedback({ outfit, decision, reasonCode, reasonLabel, memoryScope, controls, status }) {
    if (state.source !== "api" || !state.scene?.request_id || !state.stylingSessionId) return;
    if (outfit.local_replacement_note || state.localReplacements.some((entry) => entry.outfit_id === outfit.outfit_id)) {
      status.textContent = "此卡包含客户端替换，未发送反馈。请重新加载原始方向或先建立服务端 Look。";
      return;
    }
    controls.forEach((control) => { control.disabled = true; });
    status.textContent = "正在提交到服务端……";
    try {
      const response = await api("/recommend/feedback", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          request_id: state.scene.request_id,
          outfit_id: outfit.outfit_id,
          decision,
          reason_code: reasonCode || undefined,
          memory_scope: memoryScope
        })
      });
      const receipt = response.feedback || response.record || response;
      const acknowledged = typeof receipt.feedback_id === "string" && receipt.feedback_id.length > 0;
      if (!acknowledged
        || receipt.decision !== decision
        || receipt.reason_code !== (reasonCode || null)
        || receipt.memory_scope !== memoryScope
        || receipt.request_id !== state.scene.request_id
        || receipt.outfit_id !== outfit.outfit_id
        || receipt.user_id !== userId
        || receipt.styling_session_id !== state.stylingSessionId) {
        throw new Error("反馈回执未通过方向、用户与会话复核");
      }

      const rawProposal = response.memory_proposal || (response.proposal?.proposal_id ? response.proposal : null);
      const committedMemory = response.memory_record || (response.record?.memory_id ? response.record : null);
      let proposal = null;
      if (memoryScope === "propose") {
        proposal = normalizeMemoryProposal(rawProposal);
        if (!proposal || proposal.status !== "proposed" || committedMemory) {
          throw new Error("长期反馈必须只返回待确认 MemoryProposal");
        }
        state.memoryProposals = [...state.memoryProposals.filter((item) => item.proposal_id !== proposal.proposal_id), proposal];
        renderMemory();
      } else if (rawProposal || committedMemory) {
        throw new Error("仅本次反馈不得创建记忆对象");
      }

      state.directionFeedback.set(outfit.outfit_id, { decision, reasonCode, reasonLabel, memoryScope });
      renderRecommendations();
      await fetchTrace(response.trace_id || receipt.trace_id || proposal?.trace_id);
    } catch (error) {
      status.textContent = `提交未完成：${error.message}`;
      controls.forEach((control) => { control.disabled = false; });
    }
  }

  function renderRecommendations() {
    const recommendation = state.recommendation;
    const section = byId("recommendation-section");
    const grid = byId("direction-grid");
    const gap = byId("gap-explanation");
    const active = byId("active-look");
    const shopping = byId("allowed-shopping-zone");
    if (!recommendation) {
      section.hidden = true;
      grid.hidden = true;
      grid.replaceChildren();
      gap.hidden = true;
      gap.textContent = "";
      active.hidden = true;
      shopping.replaceChildren();
      byId("direction-count-note").textContent = "当前没有可展示的合法方向。";
      return;
    }
    section.hidden = false;
    const outfits = recommendation.outfits || [];
    byId("direction-count-note").textContent = outfits.length
      ? `展示 ${outfits.length} 个经前端白名单复核的合法方向。`
      : "当前没有可展示的合法方向。";
    gap.hidden = !recommendation.gap_explanation;
    gap.textContent = recommendation.gap_explanation || "";
    grid.hidden = Boolean(state.activeOutfitId);
    grid.replaceChildren(...outfits.map((outfit, index) => {
      const card = make("article", `direction-card${outfit.is_primary ? " is-primary" : ""}`);
      card.append(make("span", "direction-number", String(index + 1).padStart(2, "0")));
      if (outfit.is_primary) card.append(make("span", "primary-label", "首选"));
      card.append(make("h4", "", outfit.strategy_label || `方向 ${index + 1}`));
      const items = make("div", "outfit-items");
      items.append(...(outfit.items || []).map(itemChip));
      card.append(items, renderRecommendationPreview(outfit), listBlock("为什么适合", outfit.reasons), listBlock("真实权衡", outfit.risks, "direction-block risk-block"));
      if (outfit.local_replacement_note) card.append(make("p", "trust-note", outfit.local_replacement_note));
      card.append(replacementControl(outfit), directionFeedbackControl(outfit));
      const footer = make("div", "direction-footer");
      footer.append(make("span", "", outfit.trust_statement || "全部来自现有衣橱"));
      const choose = make("button", "button button-primary", "选这个方向");
      choose.type = "button";
      choose.addEventListener("click", () => selectOutfit(outfit.outfit_id, choose));
      footer.append(choose); card.append(footer);
      return card;
    }));
    renderActiveLook();
    shopping.replaceChildren();
    const highGuard = isShoppingGuarded(state.scene);
    if (!highGuard && recommendation.ui_capabilities?.shopping_cta && recommendation.shopping_suggestions?.length) {
      const panel = make("article", "shopping-suggestion-panel");
      panel.append(make("h4", "", "可选 Mock 补购（演示数据）"), make("p", "", "现有衣橱方案优先；以下仅在明确购物意图下展示。"));
      recommendation.shopping_suggestions.forEach((item) => panel.append(make("p", "", `${item.title} — ${item.reason}`)));
      shopping.append(panel);
    }
  }

  function recommendationPreviewImageUrl(preview) {
    return recommendationPreviewRuntime.imageUrl(preview, userId);
  }

  function normalizeRecommendationPreviewBatch(raw, expected) {
    return recommendationPreviewRuntime.normalizeBatch(raw, { ...expected, userId });
  }

  function renderRecommendationPreview(outfit) {
    const entry = state.recommendationPreviews.get(outfit.outfit_id);
    const panel = make("figure", "direction-preview");
    panel.dataset.outfitId = outfit.outfit_id;
    panel.dataset.state = entry?.status || "idle";
    if (!entry || entry.status === "idle") {
      panel.append(make("p", "direction-preview-status", "2D 视觉参考将在方向卡显示后生成。"));
      return panel;
    }
    if (entry.status === "generating") {
      panel.append(make("p", "direction-preview-status", "CPA 正在生成这套无身份 2D 组合参考…"));
      return panel;
    }
    const preview = entry.preview;
    if (entry.status === "succeeded" && preview && imageAssetRuntime.staticImageSuccessBasis(preview.provider) !== null) {
      const image = make("img", "direction-preview-image");
      image.alt = `${outfit.strategy_label || "穿搭方向"} · AI 生成的 2D 视觉参考`;
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.src = recommendationPreviewImageUrl(preview);
      const caption = make("figcaption", "direction-preview-caption", "AI 生成的 2D 视觉参考；不代表真实试穿、精确尺码、面料或垂坠。");
      image.addEventListener("error", () => {
        image.remove();
        panel.dataset.state = "degraded";
        caption.textContent = "2D 图片暂时无法按当前 owner/session 安全读取；文字方向与衣橱单品仍然有效。";
      }, { once: true });
      panel.append(image, caption);
      return panel;
    }
    panel.append(make("p", "direction-preview-status", "这套 2D 图片未完成；文字方向与衣橱单品仍可继续使用。"));
    return panel;
  }

  function createRecommendationPreviewRequestId() {
    if (window.crypto?.randomUUID) return `recommend-preview-${window.crypto.randomUUID()}`;
    return `recommend-preview-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function generateRecommendationPreviews(response) {
    const recommendation = state.recommendation;
    const outfits = recommendation?.outfits || [];
    if (state.source !== "api" || response.action !== "recommend" || !outfits.length || !state.stylingSessionId) return;
    recommendationPreviewGuard.cancel();
    const previewRequestId = createRecommendationPreviewRequestId();
    const controller = new AbortController();
    const token = recommendationPreviewGuard.begin(previewRequestId, controller);
    if (!token) return;
    const expected = {
      previewRequestId,
      requestId: response.request_id,
      stylingSessionId: state.stylingSessionId,
      outfits: outfits.map((outfit) => ({ outfit_id: outfit.outfit_id, items: [...outfit.items] }))
    };
    state.recommendationPreviews = new Map(outfits.map((outfit) => [outfit.outfit_id, { status: "generating", preview: null }]));
    renderRecommendations();
    try {
      const raw = await api("/recommend/previews/static-2d", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: expected.stylingSessionId,
          request_id: expected.requestId,
          outfit_ids: expected.outfits.map((outfit) => outfit.outfit_id),
          preview_request_id: expected.previewRequestId
        }),
        timeoutMs: RECOMMENDATION_PREVIEW_TIMEOUT_MS,
        signal: controller.signal
      });
      if (!recommendationPreviewGuard.isCurrent(token)
        || state.stylingSessionId !== expected.stylingSessionId
        || state.recommendation?.request_id !== expected.requestId) return;
      const entries = normalizeRecommendationPreviewBatch(raw, expected);
      state.recommendationPreviews = new Map(expected.outfits.map((outfit, index) => [outfit.outfit_id, entries[index]]));
      renderRecommendations();
    } catch (error) {
      if (!recommendationPreviewGuard.isCurrent(token)) return;
      state.recommendationPreviews = new Map(outfits.map((outfit) => [outfit.outfit_id, {
        status: "failed",
        preview: null,
        reason: error.message
      }]));
      renderRecommendations();
    } finally {
      recommendationPreviewGuard.finish(token);
    }
  }

  function lookVersionId(look) {
    return look?.look_version_id || look?.version_id || null;
  }

  function lookItemIds(look) {
    return (look?.item_ids || look?.items || []).map((item) => typeof item === "string" ? item : item?.garment_id || item?.item_id || item?.id).filter(Boolean);
  }

  function lookAssetIds(look = state.activeLook) {
    const raw = Array.isArray(look?.asset_ids) ? look.asset_ids : [];
    return [...new Set(raw.filter((id) => typeof id === "string" && id.trim()))];
  }

  function availableLookAssetIds(look = state.activeLook) {
    return lookAssetIds(look).filter((assetId) => !state.deletedAssetIds.has(assetId));
  }

  function sameIdSet(left, right) {
    const normalizedLeft = [...new Set(left)].sort();
    const normalizedRight = [...new Set(right)].sort();
    return normalizedLeft.length === normalizedRight.length && normalizedLeft.every((id, index) => id === normalizedRight[index]);
  }

  function clearAssetPreview() {
    if (state.assetPreviewUrl) URL.revokeObjectURL(state.assetPreviewUrl);
    state.assetPreviewUrl = null;
    state.assetPreviewAssetId = null;
    byId("asset-preview").removeAttribute("src");
    byId("asset-preview-wrap").hidden = true;
    byId("asset-file").value = "";
    byId("asset-status").textContent = "仅在本机以临时 Blob URL 预览，不写入 Debug 或浏览器存储。";
  }

  function showAssetOperation(message) {
    const status = byId("asset-operation-status");
    status.hidden = !message;
    status.textContent = message || "";
  }

  function syncAssetsToLook(look, additionalAssets = []) {
    const ids = availableLookAssetIds(look);
    const known = new Map([...state.assets, ...additionalAssets]
      .filter((asset) => asset?.asset_id && asset.styling_session_id === state.stylingSessionId)
      .map((asset) => [asset.asset_id, asset]));
    state.assets = ids.map((assetId) => known.get(assetId) || {
      asset_id: assetId,
      styling_session_id: state.stylingSessionId,
      visual_quality: "unreported"
    });
    if (state.assetPreviewAssetId && !ids.includes(state.assetPreviewAssetId)) clearAssetPreview();
  }

  function resetSessionScopedState(stylingSessionId) {
    dialogueTraceGuard.cancel();
    dialogueGuard.cancel();
    clearDialogueWaitTimer();
    recommendationPreviewGuard.cancel();
    clearAssetPreview();
    showAssetOperation("");
    state.stylingSessionId = stylingSessionId;
    state.pendingDialogueRetry = null;
    state.conversationMode = null;
    state.pendingQuestionStatus = "none";
    state.suggestedReplies = [];
    state.turnIndex = 0;
    state.historyVersion = 0;
    state.dialogueProvider = null;
    state.recommendation = null;
    state.activeOutfitId = null;
    state.localReplacements = [];
    state.directionFeedback = new Map();
    state.recommendationPreviews = new Map();
    state.activeLook = null;
    state.lookVersions = [];
    state.lookComparisons = new Map();
    state.lookDecisionLedger = [];
    state.lookDecisionLedgerComplete = true;
    state.assets = [];
    state.unboundAssets = [];
    state.deletedAssetIds = new Set();
    state.preview2d = null;
    state.preview2dPending = false;
    state.scorecard = null;
    state.adjustments = [];
    state.rejectedCanonical = new Set();
    state.finalized = false;
    state.finalization = null;
    state.traceHistory = [];
    setDialogueInFlightControls(false);
    byId("cocreation").hidden = true;
    byId("active-look").hidden = true;
  }

  function versionLabel(look) {
    const index = Number(look?.version_index);
    return Number.isFinite(index) ? `v${index}` : "Look";
  }

  function validateLook(raw) {
    const look = raw?.look || raw;
    const ids = lookItemIds(look);
    if (!look || !lookVersionId(look) || !ids.length || ids.some((id) => !isAllowedGarment(id, state.scene)) || !isCompleteIds(ids)) return null;
    if (look.user_id && look.user_id !== userId) return null;
    if (look.styling_session_id && state.stylingSessionId && look.styling_session_id !== state.stylingSessionId) return null;
    if ((look.unresolved_items || []).length) return null;
    return { ...look, item_ids: ids };
  }

  function isFinalized() {
    return state.finalized || state.activeLook?.status === "final";
  }

  function setCocreationControls() {
    const ready = state.source === "api" && Boolean(state.activeLook) && !isFinalized();
    ["asset-file", "asset-angle", "asset-submit", "score-request", "user-modify-submit"].forEach((id) => { byId(id).disabled = !ready; });
    byId("preview2d-generate").disabled = state.source !== "api" || !state.activeLook || state.preview2dPending;
    byId("satisfaction").disabled = !state.activeLook || isFinalized();
    byId("final-reason").disabled = !state.activeLook || isFinalized();
    byId("finalize-submit").disabled = !state.activeLook || isFinalized();
  }

  async function selectOutfit(outfitId, button) {
    if (state.source !== "api") {
      showToast("离线仅浏览，无法建立 Active Look");
      return;
    }
    const outfit = state.recommendation?.outfits?.find((item) => item.outfit_id === outfitId);
    if (!outfit || !state.scene?.request_id || !state.stylingSessionId) {
      showToast("方向或会话状态不完整，已安全停止");
      return;
    }
    button.disabled = true;
    byId("session-state-label").textContent = "正在建立不可变 Look v1";
    try {
      const response = await api("/look", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          created_from: "selected_outfit",
          request_id: state.scene.request_id,
          outfit_id: outfitId,
          asset_ids: []
        })
      });
      const initialLook = validateLook(response);
      if (!initialLook || lookAssetIds(initialLook).length !== 0) throw new Error("Look 响应未通过用户、衣橱、槽位与资产集合复核");
      let look = initialLook;
      let replacementWarning = null;
      if (!sameIdSet(initialLook.item_ids, outfit.items)) {
        try {
          const revisionResponse = await api("/look", {
            method: "POST",
            body: JSON.stringify({
              user_id: userId,
              styling_session_id: state.stylingSessionId,
              created_from: "user_revision",
              parent_version_id: lookVersionId(initialLook),
              item_ids: [...outfit.items],
              asset_ids: []
            })
          });
          const revisedLook = validateLook(revisionResponse);
          if (!revisedLook
            || revisedLook.parent_version_id !== lookVersionId(initialLook)
            || !sameIdSet(revisedLook.item_ids, outfit.items)
            || lookAssetIds(revisedLook).length !== 0
            || Number(revisedLook.version_index) <= Number(initialLook.version_index)) {
            throw new Error("替换版本响应未通过版本链与白名单复核");
          }
          look = revisedLook;
        } catch (error) {
          replacementWarning = error.message;
        }
      }
      clearAssetPreview();
      state.activeOutfitId = outfitId;
      state.activeLook = look;
      state.lookVersions = look === initialLook ? [initialLook] : [initialLook, look];
      state.assets = [];
      syncAssetsToLook(look);
      state.rejectedCanonical = new Set();
      state.finalized = look.status === "final";
      state.scorecard = null;
      state.adjustments = [];
      byId("direction-grid").hidden = true;
      byId("task-progress").style.width = "72%";
      await refreshLooks();
      renderActiveLook();
      renderCocreation();
      await fetchTrace(look.trace_id || response.trace_id);
      appendMessage("Stylist", replacementWarning
        ? `原方向 ${versionLabel(initialLook)} 已建立，但衣橱替换未绑定：${replacementWarning}。界面没有假装替换成功，可在共创区再次修改。`
        : look === initialLook
          ? `已建立 ${versionLabel(look)}，接下来只围绕这一套穿搭共创。`
          : `已建立 ${versionLabel(initialLook)}，并将你选定的衣橱替换绑定为 ${versionLabel(look)}；两版都可追溯。`);
      byId("cocreation").scrollIntoView({ block: "start", behavior: "smooth" });
    } catch (error) {
      appendMessage("系统", `Active Look 未建立：${error.message}。方向仍保留，可在服务恢复后重试。`);
      showToast("未建立未验证的 Look");
    } finally {
      button.disabled = false;
    }
  }

  function renderActiveLook() {
    const active = byId("active-look");
    const look = state.activeLook;
    const outfit = state.recommendation?.outfits?.find((item) => item.outfit_id === state.activeOutfitId);
    if (!look || !outfit) { active.hidden = true; active.replaceChildren(); return; }
    active.hidden = false;
    active.replaceChildren();
    active.append(make("span", "active-look-mark", isFinalized() ? "FINAL" : versionLabel(look)));
    active.append(make("h4", "", outfit.strategy_label));
    const items = make("div", "outfit-items"); items.append(...look.item_ids.map(itemChip));
    active.append(items, make("p", "", isFinalized() ? "这套穿搭已按你的满意决定定稿。" : "已聚焦单一 Active Look；后续变化只创建新版本，不覆盖旧版本。"));
  }

  function normalizeLookList(payload) {
    const raw = payload?.versions || payload?.look_versions || payload?.looks || [];
    const list = Array.isArray(raw) ? raw : [];
    const activeCandidate = payload?.active || payload?.active_look || payload?.active_version;
    const candidates = activeCandidate ? [...list, activeCandidate] : list;
    if (state.activeLook) candidates.push(state.activeLook);
    const unique = new Map();
    candidates.forEach((candidate) => {
      const look = validateLook(candidate);
      if (look) unique.set(lookVersionId(look), look);
    });
    return [...unique.values()].sort((a, b) => Number(a.version_index || 0) - Number(b.version_index || 0));
  }

  async function refreshLooks() {
    if (state.source !== "api" || !state.stylingSessionId) return;
    try {
      const query = new URLSearchParams({ styling_session_id: state.stylingSessionId, user_id: userId });
      const response = await api(`/look?${query.toString()}`);
      if (response?.user_id !== userId) throw new Error("版本链归属用户不匹配");
      if (response?.styling_session_id !== state.stylingSessionId) throw new Error("版本链归属会话不匹配");
      state.lookVersions = normalizeLookList(response);
      const ledger = normalizeAdjustmentDecisionLedger(response.adjustment_decisions, state.lookVersions, response.user_id, response.styling_session_id);
      state.lookDecisionLedger = ledger.entries;
      state.lookDecisionLedgerComplete = ledger.complete;
      const newest = [...state.lookVersions].reverse().find((look) => look.status === "active" || look.status === "final");
      if (newest) {
        state.activeLook = newest;
        syncAssetsToLook(newest);
      }
      if (state.lookVersions.some((look) => look.status === "final")) state.finalized = true;
      await refreshLookComparisons(state.lookVersions);
    } catch (error) {
      if (state.activeLook) state.lookVersions = [state.activeLook];
      state.lookComparisons = new Map();
      showToast(`版本链暂不可用：${error.message}`);
    }
    renderVersionTrail();
  }

  function renderCocreation() {
    const section = byId("cocreation");
    if (!state.activeLook) { section.hidden = true; return; }
    section.hidden = false;
    const look = state.activeLook;
    byId("cocreation-state").textContent = isFinalized() ? "Final · 已定稿" : `${versionLabel(look)} · 共创中`;
    byId("look-overview-heading").textContent = isFinalized() ? "Final Look" : `${versionLabel(look)} · Active Look`;
    byId("look-overview-items").replaceChildren(...look.item_ids.map(itemChip));
    byId("look-overview-note").textContent = isFinalized() ? "已停止继续调整；历史版本仍可比较。" : "这套穿搭在当前目标下继续评估；任何变化都会建立新版本。";
    const status = byId("look-overview-status");
    status.replaceChildren(make("span", "look-version-badge", isFinalized() ? "Final" : versionLabel(look)));
    if (availableLookAssetIds(look).length) {
      status.append(make("span", "mini-badge", `${availableLookAssetIds(look).length} 张静态图`));
      if (state.assets.some((asset) => asset.visual_quality === "limited")) {
        status.append(make("span", "mini-badge", "视觉质量 limited · 仅定性评分"));
      }
    }
    byId("final-status").textContent = isFinalized() ? "已定稿" : "始终可用";
    setCocreationControls();
    renderAssetList();
    renderScorecard();
    renderAdjustments();
    renderModifyFields();
    renderVersionTrail();
    renderFinalizeState();
    renderPreview2d();
  }

  function versionParent(look) {
    return state.lookVersions.find((candidate) => lookVersionId(candidate) === look.parent_version_id);
  }

  function strictIdList(value) {
    if (!Array.isArray(value)) return null;
    const ids = value.map((id) => typeof id === "string" ? id.trim() : "");
    if (ids.some((id) => !id || id.length > 160) || new Set(ids).size !== ids.length) return null;
    return ids;
  }

  function idChanges(beforeIds, afterIds) {
    const before = new Set(beforeIds);
    const after = new Set(afterIds);
    return {
      added: [...after].filter((id) => !before.has(id)).sort(),
      removed: [...before].filter((id) => !after.has(id)).sort(),
      retained: [...after].filter((id) => before.has(id)).sort()
    };
  }

  function verifiedIdChanges(raw, beforeIds, afterIds) {
    if (!raw || typeof raw !== "object") return null;
    const reported = {
      added: strictIdList(raw.added),
      removed: strictIdList(raw.removed),
      retained: strictIdList(raw.retained)
    };
    if (Object.values(reported).some((value) => value === null)) return null;
    const expected = idChanges(beforeIds, afterIds);
    return Object.keys(expected).every((key) => sameIdSet(reported[key], expected[key])) ? expected : null;
  }

  function normalizeCanonicalList(value) {
    if (!Array.isArray(value)) return null;
    const keys = value.map((item) => typeof item === "string" ? item.trim() : "");
    if (keys.some((key) => !Object.prototype.hasOwnProperty.call(canonicalAdjustmentLabels, key))) return null;
    return [...new Set(keys)];
  }

  function normalizeAdjustmentDecisionRecord(raw, versions, ownerId, sessionId) {
    if (!raw || typeof raw !== "object" || ownerId !== userId || sessionId !== state.stylingSessionId) return null;
    const sourceVersionId = typeof raw.look_version_id === "string" ? raw.look_version_id.trim() : "";
    const source = versions.find((look) => lookVersionId(look) === sourceVersionId);
    const decision = typeof raw.decision === "string" ? raw.decision.trim() : "";
    const canonicalKey = typeof raw.canonical_key === "string" ? raw.canonical_key.trim() : "";
    const canonicalAction = typeof raw.canonical_action === "string" ? raw.canonical_action.trim() : "";
    const occurredAt = typeof raw.created_at === "string" ? Date.parse(raw.created_at) : NaN;
    if (!source
      || source.user_id !== ownerId
      || source.styling_session_id !== sessionId
      || raw.styling_session_id !== sessionId
      || !Object.prototype.hasOwnProperty.call(adjustmentDecisionLabels, decision)
      || !Object.prototype.hasOwnProperty.call(canonicalAdjustmentLabels, canonicalKey)
      || canonicalAction !== canonicalKey
      || (decision === "user_modified") !== (canonicalKey === "user_modified")
      || !Number.isFinite(occurredAt)) return null;
    return Object.freeze({ sourceVersionId, decision, canonicalKey, occurredAt });
  }

  function normalizeAdjustmentDecisionLedger(raw, versions, ownerId, sessionId) {
    if (!Array.isArray(raw)) return { entries: [], complete: false };
    const normalized = raw.map((record) => normalizeAdjustmentDecisionRecord(record, versions, ownerId, sessionId));
    const seen = new Set();
    const entries = normalized.filter(Boolean).filter((entry) => {
      const fingerprint = `${entry.sourceVersionId}|${entry.decision}|${entry.canonicalKey}|${entry.occurredAt}`;
      if (seen.has(fingerprint)) return false;
      seen.add(fingerprint);
      return true;
    }).sort((left, right) => left.occurredAt - right.occurredAt);
    return { entries, complete: normalized.every(Boolean) };
  }

  function mergeAdjustmentDecisionRecord(raw) {
    const update = normalizeAdjustmentDecisionLedger(
      raw ? [raw] : null,
      state.lookVersions,
      userId,
      state.stylingSessionId
    );
    if (!update.complete) state.lookDecisionLedgerComplete = false;
    if (!update.entries.length) return;
    const combined = normalizeAdjustmentDecisionLedger(
      [...state.lookDecisionLedger, ...update.entries].map((entry) => ({
        styling_session_id: state.stylingSessionId,
        look_version_id: entry.sourceVersionId,
        canonical_key: entry.canonicalKey,
        canonical_action: entry.canonicalKey,
        decision: entry.decision,
        created_at: new Date(entry.occurredAt).toISOString()
      })),
      state.lookVersions,
      userId,
      state.stylingSessionId
    );
    state.lookDecisionLedger = combined.entries;
  }

  function normalizeDimensionComparison(raw) {
    const allowed = new Set(scoreDimensions.map(([key]) => key));
    const fields = {
      improved: raw?.improved_dimensions,
      declined: raw?.declined_dimensions,
      unchanged: raw?.unchanged_dimensions,
      uncertain: raw?.uncertain_dimensions
    };
    const normalized = {};
    for (const [key, value] of Object.entries(fields)) {
      if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !allowed.has(item))) return null;
      normalized[key] = [...new Set(value)];
      if (normalized[key].length !== value.length) return null;
    }
    const partition = Object.values(normalized).flat();
    if (partition.length !== allowed.size || new Set(partition).size !== allowed.size) return null;
    return normalized;
  }

  function normalizeLookComparison(raw, parent, look) {
    if (!raw || typeof raw !== "object"
      || raw.styling_session_id !== state.stylingSessionId
      || raw.from_version_id !== lookVersionId(parent)
      || raw.to_version_id !== lookVersionId(look)) return null;
    const parentAssets = strictIdList(parent.asset_ids);
    const lookAssets = strictIdList(look.asset_ids);
    if (parentAssets === null || lookAssets === null) return null;
    const itemChanges = verifiedIdChanges(raw.item_changes, parent.item_ids, look.item_ids);
    const assetChanges = verifiedIdChanges(raw.asset_changes, parentAssets, lookAssets);
    if (!itemChanges || !assetChanges) return null;
    const totalScoreDelta = typeof raw.total_score_delta === "number"
      && Number.isFinite(raw.total_score_delta)
      && Math.abs(raw.total_score_delta) <= 100
      ? Number(raw.total_score_delta.toFixed(2))
      : null;
    return {
      fromVersionId: raw.from_version_id,
      toVersionId: raw.to_version_id,
      itemChanges,
      assetChanges,
      acceptedAdjustments: normalizeCanonicalList(raw.accepted_adjustments),
      partiallyAcceptedAdjustments: normalizeCanonicalList(raw.partially_accepted_adjustments),
      rejectedAdjustments: normalizeCanonicalList(raw.rejected_adjustments),
      totalScoreDelta,
      dimensionComparison: normalizeDimensionComparison(raw)
    };
  }

  async function fetchLookComparison(look, parent) {
    const query = new URLSearchParams({
      styling_session_id: state.stylingSessionId,
      user_id: userId,
      look_version_id: lookVersionId(look)
    });
    const response = await api(`/look?${query.toString()}`);
    if (response?.user_id !== userId || response?.styling_session_id !== state.stylingSessionId) {
      throw new Error("版本比较归属不匹配");
    }
    const selected = validateLook(response.look_version);
    const returnedParent = validateLook(response.parent);
    const selectedAssets = strictIdList(selected?.asset_ids);
    const returnedParentAssets = strictIdList(returnedParent?.asset_ids);
    const lookAssets = strictIdList(look.asset_ids);
    const parentAssets = strictIdList(parent.asset_ids);
    if (!selected || !returnedParent
      || lookVersionId(selected) !== lookVersionId(look)
      || lookVersionId(returnedParent) !== lookVersionId(parent)
      || selected.parent_version_id !== lookVersionId(parent)
      || !sameIdSet(selected.item_ids, look.item_ids)
      || !sameIdSet(returnedParent.item_ids, parent.item_ids)
      || selectedAssets === null || returnedParentAssets === null || lookAssets === null || parentAssets === null
      || !sameIdSet(selectedAssets, lookAssets)
      || !sameIdSet(returnedParentAssets, parentAssets)) {
      throw new Error("版本比较未通过父子链与快照复核");
    }
    const comparison = normalizeLookComparison(response.comparison, returnedParent, selected);
    if (!comparison) throw new Error("版本比较字段不完整");
    return comparison;
  }

  async function refreshLookComparisons(versions) {
    const next = new Map();
    const pairs = versions.map((look) => ({ look, parent: versionParent(look) })).filter(({ parent }) => Boolean(parent));
    const results = await Promise.all(pairs.map(async ({ look, parent }) => {
      try {
        return [lookVersionId(look), await fetchLookComparison(look, parent)];
      } catch (_error) {
        return [lookVersionId(look), null];
      }
    }));
    results.forEach(([versionId, comparison]) => next.set(versionId, comparison));
    state.lookComparisons = next;
  }

  async function refreshLookComparison(look) {
    const parent = versionParent(look);
    if (!parent) return;
    try {
      state.lookComparisons.set(lookVersionId(look), await fetchLookComparison(look, parent));
    } catch (_error) {
      state.lookComparisons.set(lookVersionId(look), null);
    }
    renderVersionTrail();
  }

  function canonicalListText(value) {
    if (value === null) return "记录暂不可用；未显示未识别文本";
    if (!value.length) return "无";
    return value.map((key) => canonicalAdjustmentLabels[key]).join("、");
  }

  function decisionTimeText(timestamp) {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
    }).format(new Date(timestamp));
  }

  function createdVersionForDecision(entry) {
    if (entry.decision === "reject") return { status: "not_created", look: null };
    const source = state.lookVersions.find((look) => lookVersionId(look) === entry.sourceVersionId);
    if (!source) return { status: "unavailable", look: null };
    const children = state.lookVersions.filter((look) => look.parent_version_id === entry.sourceVersionId);
    let created = null;
    if (entry.decision === "accept") {
      const sourceKeys = normalizeCanonicalList(source.accepted_adjustments);
      created = sourceKeys === null || sourceKeys.includes(entry.canonicalKey) ? null : children.find((look) => {
        const keys = normalizeCanonicalList(look.accepted_adjustments);
        return look.created_from === "adjustment" && keys?.includes(entry.canonicalKey);
      });
    } else if (entry.decision === "partial") {
      const sourceKeys = normalizeCanonicalList(source.partially_accepted_adjustments);
      created = sourceKeys === null || sourceKeys.includes(entry.canonicalKey) ? null : children.find((look) => {
        const keys = normalizeCanonicalList(look.partially_accepted_adjustments);
        return look.created_from === "adjustment" && keys?.includes(entry.canonicalKey);
      });
    } else if (entry.decision === "user_modified") {
      created = children.find((look) => look.created_from === "user_revision");
    }
    return created ? { status: "created", look: created } : { status: "unavailable", look: null };
  }

  function decisionCreationText(entry) {
    const result = createdVersionForDecision(entry);
    if (result.status === "not_created") return "未创建新版本（拒绝仅记账）";
    if (result.status === "created") return `已创建 ${versionLabel(result.look)}`;
    return "新版本关联未确认（未作推断）";
  }

  function appendDecisionLedger(container, look) {
    const entries = state.lookDecisionLedger.filter((entry) => entry.sourceVersionId === lookVersionId(look));
    const showValidationWarning = !state.lookDecisionLedgerComplete && lookVersionId(look) === lookVersionId(state.activeLook);
    if (!entries.length && !showValidationWarning) return;
    const block = make("section", "decision-ledger");
    block.setAttribute("aria-label", `${versionLabel(look)} 的调整决策记录`);
    block.append(make("h5", "", "此版本的调整决策记录"));
    if (entries.length) {
      const list = make("ul", "decision-ledger-list");
      entries.forEach((entry) => list.append(make("li", "", [
        decisionTimeText(entry.occurredAt),
        adjustmentDecisionLabels[entry.decision],
        canonicalAdjustmentLabels[entry.canonicalKey],
        decisionCreationText(entry)
      ].join(" · "))));
      block.append(list);
    }
    if (showValidationWarning) {
      block.append(make("p", "comparison-unavailable", "部分决策记录未通过归属或闭集校验，已隐藏；未显示自由原因或内部 ID。"));
    }
    container.append(block);
  }

  function itemChangeText(changes) {
    if (!changes.added.length && !changes.removed.length) return "单品：未变化（服务端已核对）";
    const names = (ids) => ids.map((id) => garmentMap().get(id)?.name).filter(Boolean);
    const added = names(changes.added);
    const removed = names(changes.removed);
    const addedText = added.length === changes.added.length ? added.join("、") : `${changes.added.length} 件`;
    const removedText = removed.length === changes.removed.length ? removed.join("、") : `${changes.removed.length} 件`;
    return `单品：新增 ${addedText || "无"}；移除 ${removedText || "无"}`;
  }

  function assetChangeText(changes) {
    if (!changes.added.length && !changes.removed.length) return "证据图：未变化（服务端已核对）";
    return `证据图：新增 ${changes.added.length} 张；移除 ${changes.removed.length} 张`;
  }

  function scoreDeltaText(comparison) {
    if (comparison.totalScoreDelta === null) return "穿搭总分变化：证据不足，未按 0 分处理";
    const value = Object.is(comparison.totalScoreDelta, -0) ? 0 : comparison.totalScoreDelta;
    return `穿搭总分变化：${value > 0 ? "+" : ""}${value}`;
  }

  function dimensionDeltaText(comparison) {
    const dimensions = comparison.dimensionComparison;
    if (!dimensions) return "有变化的维度：比较字段不可用，未作推断";
    const labelFor = (key) => scoreDimensions.find(([allowed]) => allowed === key)?.[1];
    const changed = [];
    if (dimensions.improved.length) changed.push(`提升：${dimensions.improved.map(labelFor).join("、")}`);
    if (dimensions.declined.length) changed.push(`下降：${dimensions.declined.map(labelFor).join("、")}`);
    if (!changed.length) changed.push(dimensions.uncertain.length ? "暂无可确认变化" : "无");
    if (dimensions.uncertain.length) changed.push(`证据不足：${dimensions.uncertain.map(labelFor).join("、")}`);
    return `有变化的维度：${changed.join("；")}`;
  }

  function appendLookComparison(container, look) {
    const parent = versionParent(look);
    if (!parent) {
      container.append(make("p", "comparison-caption", look.parent_version_id
        ? "父版本快照缺失，无法比较；未将缺失数据当作 0 变化。"
        : "起始版本没有父版本差异。"));
      return;
    }
    const block = make("section", "version-change-summary");
    block.setAttribute("aria-label", `${versionLabel(look)} 相对 ${versionLabel(parent)} 的变化`);
    block.append(make("h5", "", `相对 ${versionLabel(parent)} 的服务端比较`));
    const comparison = state.lookComparisons.get(lookVersionId(look));
    if (!comparison) {
      block.append(make("p", "comparison-unavailable", "比较暂不可用；未将缺失字段或请求错误显示成 0 变化。"));
      container.append(block);
      return;
    }
    const list = make("ul", "comparison-summary-list");
    [
      itemChangeText(comparison.itemChanges),
      assetChangeText(comparison.assetChanges),
      `已接受的规范调整：${canonicalListText(comparison.acceptedAdjustments)}`,
      `部分接受的规范调整：${canonicalListText(comparison.partiallyAcceptedAdjustments)}`,
      `已拒绝的规范调整：${canonicalListText(comparison.rejectedAdjustments)}`,
      scoreDeltaText(comparison),
      dimensionDeltaText(comparison)
    ].forEach((text) => list.append(make("li", "", text)));
    block.append(list);
    container.append(block);
  }

  function renderVersionTrail() {
    const timeline = byId("version-timeline");
    const versions = state.lookVersions;
    if (!versions.length) {
      timeline.replaceChildren(make("p", "empty-state compact", "建立 Look 后显示不可变版本链。"));
      byId("compare-left").disabled = true;
      byId("compare-right").disabled = true;
      byId("version-comparison").replaceChildren();
      return;
    }
    timeline.replaceChildren(...versions.map((look) => {
      const node = make("article", `version-node${lookVersionId(look) === lookVersionId(state.activeLook) ? " is-active" : ""}${look.status === "final" ? " is-final" : ""}`);
      node.append(make("h4", "", look.status === "final" ? `${versionLabel(look)} · Final` : versionLabel(look)));
      const parent = versionParent(look);
      node.append(make("p", "", parent ? `父版本 ${versionLabel(parent)}` : "起始版本"));
      const items = make("div", "outfit-items"); items.append(...look.item_ids.map(itemChip)); node.append(items);
      appendLookComparison(node, look);
      appendDecisionLedger(node, look);
      if (!isFinalized() && lookVersionId(look) !== lookVersionId(state.activeLook)) {
        const rollback = make("button", "button button-quiet button-small", "回退到此版本");
        rollback.type = "button";
        rollback.addEventListener("click", () => rollbackLook(look, rollback));
        node.append(rollback, make("p", "", "回退会创建新版本，不覆盖历史。"));
      }
      return node;
    }));
    const left = byId("compare-left");
    const right = byId("compare-right");
    const previousLeft = Number(left.value);
    const previousRight = Number(right.value);
    const options = versions.map((look, index) => {
      const option = make("option", "", look.status === "final" ? `${versionLabel(look)} · Final` : versionLabel(look));
      option.value = String(index);
      return option;
    });
    left.replaceChildren(...options.map((option) => option.cloneNode(true)));
    right.replaceChildren(...options.map((option) => option.cloneNode(true)));
    left.value = String(Number.isInteger(previousLeft) && versions[previousLeft] ? previousLeft : Math.max(0, versions.length - 2));
    right.value = String(Number.isInteger(previousRight) && versions[previousRight] ? previousRight : versions.length - 1);
    left.disabled = versions.length < 2;
    right.disabled = versions.length < 2;
    renderVersionComparison();
  }

  function renderVersionComparison() {
    const target = byId("version-comparison");
    if (state.lookVersions.length < 2) { target.replaceChildren(); return; }
    const versions = [state.lookVersions[Number(byId("compare-left").value)], state.lookVersions[Number(byId("compare-right").value)]];
    const context = make("p", "comparison-context", "并排查看两个不可变版本；每个版本的分数与维度变化只采用其相对父版本的服务端比较，非相邻版本不推算分差。");
    target.replaceChildren(context, ...versions.map((look) => {
      const pane = make("article", "comparison-pane");
      pane.setAttribute("aria-label", `${versionLabel(look)} 版本快照与变化`);
      pane.append(make("h4", "", look.status === "final" ? `${versionLabel(look)} · Final` : versionLabel(look)));
      const items = make("div", "outfit-items"); items.append(...look.item_ids.map(itemChip)); pane.append(items);
      appendLookComparison(pane, look);
      appendDecisionLedger(pane, look);
      return pane;
    }));
  }

  async function rollbackLook(target, button) {
    if (!state.activeLook || isFinalized()) return;
    button.disabled = true;
    try {
      const response = await api("/look", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          created_from: "rollback",
          parent_version_id: lookVersionId(state.activeLook),
          rollback_target_version_id: lookVersionId(target)
        })
      });
      const look = validateLook(response);
      if (!look) throw new Error("回退版本未通过白名单与槽位复核");
      state.activeLook = look;
      syncAssetsToLook(look);
      state.scorecard = null;
      state.adjustments = [];
      await refreshLooks();
      renderCocreation();
      appendMessage("Stylist", `已从 ${versionLabel(target)} 创建新的 ${versionLabel(look)}；历史版本保持不变。`);
      if (availableLookAssetIds().length) await requestScorecard();
      await fetchTrace(look.trace_id || response.trace_id);
    } catch (error) {
      showToast(`回退未完成：${error.message}`);
    } finally {
      button.disabled = false;
    }
  }

  function normalizeAsset(response) {
    const asset = response?.asset || response;
    if (!asset?.asset_id || asset.user_id !== userId || asset.styling_session_id !== state.stylingSessionId) return null;
    if (!String(asset.media_type || "").match(/^image\/(png|jpeg|webp)$/)) return null;
    if (!Number.isInteger(asset.width) || asset.width <= 0 || !Number.isInteger(asset.height) || asset.height <= 0) return null;
    if (!["usable", "limited"].includes(asset.visual_quality)
      || asset.storage !== "memory_ephemeral"
      || asset.consent_obtained !== true
      || asset.purpose !== "styling_assessment"
      || asset.retention !== "process_lifetime"
      || asset.status !== "available") return null;
    return {
      asset_id: asset.asset_id,
      user_id: asset.user_id,
      styling_session_id: asset.styling_session_id,
      angle: asset.angle,
      media_type: asset.media_type,
      size_bytes: asset.size_bytes,
      width: asset.width,
      height: asset.height,
      visual_quality: asset.visual_quality,
      storage: asset.storage,
      consent_obtained: asset.consent_obtained,
      purpose: asset.purpose,
      retention: asset.retention,
      status: asset.status,
      trace_id: asset.trace_id
    };
  }

  function assetStatusText(asset) {
    const dimensions = `${asset.width} × ${asset.height}`;
    if (asset.visual_quality === "limited") {
      return `已绑定到新 Look · ${dimensions} · 视觉质量 limited：证据有限，不进行精确数值评分 · memory_ephemeral / process_lifetime`;
    }
    return `已绑定到新 Look · ${dimensions} · 视觉质量 usable · memory_ephemeral / process_lifetime`;
  }

  function renderAssetList() {
    const target = byId("asset-list");
    const assetIds = availableLookAssetIds();
    const unbound = state.unboundAssets.filter((asset) => asset.styling_session_id === state.stylingSessionId);
    if (!assetIds.length && !unbound.length) {
      target.replaceChildren(make("p", "empty-state compact", "当前 Look 尚未绑定可用图片；历史版本只保留不可回显的资产 ID 审计。"));
      return;
    }
    const known = new Map([...state.assets, ...unbound].map((asset) => [asset.asset_id, asset]));
    const entries = [
      ...assetIds.map((assetId) => ({ assetId, bound: true })),
      ...unbound.filter((asset) => !assetIds.includes(asset.asset_id)).map((asset) => ({ assetId: asset.asset_id, bound: false }))
    ];
    target.replaceChildren(...entries.map(({ assetId, bound }, index) => {
      const asset = known.get(assetId);
      const item = make("article", "asset-list-item");
      const details = make("div");
      details.append(make("strong", "", `静态图 ${index + 1} · ${asset?.angle || "角度未报告"}${bound ? "" : " · 未绑定"}`));
      const dimensions = Number.isInteger(asset?.width) && Number.isInteger(asset?.height) ? `${asset.width} × ${asset.height}` : "尺寸未报告";
      const quality = asset?.visual_quality === "limited"
        ? "limited · 仅定性评分"
        : asset?.visual_quality === "usable" ? "usable" : "质量未报告 · 不显示精确评分";
      details.append(make("p", "", `${dimensions} · ${quality} · ${asset?.storage || "临时保存策略未报告"}`));
      const remove = make("button", "button button-danger button-small", "删除图片");
      remove.type = "button";
      remove.addEventListener("click", () => deleteAsset(assetId, remove));
      item.append(details, remove);
      return item;
    }));
  }

  async function deleteAsset(assetId, button) {
    const parentLook = state.activeLook;
    const bound = Boolean(parentLook && availableLookAssetIds(parentLook).includes(assetId));
    const unboundAsset = state.unboundAssets.find((asset) => asset.asset_id === assetId);
    if (!bound && !unboundAsset) return;
    const ownerSessionId = unboundAsset?.styling_session_id || state.stylingSessionId;
    button.disabled = true;
    let deletionConfirmed = false;
    let deleteResponse = null;
    try {
      const query = new URLSearchParams({ user_id: userId, styling_session_id: ownerSessionId });
      deleteResponse = await api(`/assets/${encodeURIComponent(assetId)}?${query.toString()}`, { method: "DELETE" });
      const deletedId = deleteResponse.deleted_id || deleteResponse.asset_id;
      if (deletedId !== assetId) throw new Error("删除回执未通过资产归属复核");
      deletionConfirmed = true;
      state.deletedAssetIds.add(assetId);
      state.assets = state.assets.filter((asset) => asset.asset_id !== assetId);
      state.unboundAssets = state.unboundAssets.filter((asset) => asset.asset_id !== assetId);
      if (state.assetPreviewAssetId === assetId) clearAssetPreview();
      state.scorecard = null;
      state.adjustments = [];
      renderAssetList();
      renderScorecard();
      renderAdjustments();
      await fetchTrace(deleteResponse.trace_id);

      if (!bound) {
        byId("asset-status").textContent = "未绑定图片已从临时存储删除；它从未进入评分或 Look 历史。";
        showAssetOperation("未绑定图片已从临时存储删除；它从未进入评分或 Look 历史。");
        renderAssetList();
        return;
      }

      if (isFinalized()) {
        byId("asset-status").textContent = "图片已删除；Final Look 历史仍仅保留不可回显的资产 ID 审计，不再提供图片或精确评分。";
        showAssetOperation("图片已删除；Final Look 历史仍仅保留不可回显的资产 ID 审计。");
        renderCocreation();
        await requestScorecard({ allowFinalized: true });
        return;
      }

      const remainingAssetIds = availableLookAssetIds(parentLook);
      const revisionResponse = await api("/look", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          created_from: "user_revision",
          parent_version_id: lookVersionId(parentLook),
          item_ids: [...parentLook.item_ids],
          asset_ids: remainingAssetIds
        })
      });
      const look = validateLook(revisionResponse);
      const versionAdvanced = Number(look?.version_index) > Number(parentLook.version_index);
      if (!look
        || look.parent_version_id !== lookVersionId(parentLook)
        || !sameIdSet(look.item_ids, parentLook.item_ids)
        || !sameIdSet(lookAssetIds(look), remainingAssetIds)
        || !versionAdvanced) {
        throw new Error("删除后的资产集合未能绑定到合法新版本");
      }
      state.activeLook = look;
      syncAssetsToLook(look);
      await refreshLooks();
      renderCocreation();
      byId("asset-status").textContent = `图片已删除并建立 ${versionLabel(look)}；旧版本仅保留不可回显的资产 ID 审计。`;
      showAssetOperation(`图片已删除并建立 ${versionLabel(look)}；旧版本仅保留不可回显的资产 ID 审计。`);
      await requestScorecard();
      await fetchTrace(look.trace_id || revisionResponse.trace_id);
    } catch (error) {
      if (deletionConfirmed) {
        state.scorecard = null;
        state.adjustments = [];
        renderCocreation();
        byId("asset-status").textContent = `图片已删除，但版本链同步未完成：${error.message}。已停止精确评分。`;
        showAssetOperation(`图片已删除，但版本链同步未完成：${error.message}。已停止精确评分。`);
        await requestScorecard({ allowFinalized: true });
        showToast("图片已删除；版本同步未完成");
      } else {
        button.disabled = false;
        showAssetOperation(`图片删除未完成：${error.message}。图片仍保留在当前 Look。`);
        showToast(`图片删除未完成：${error.message}`);
      }
    }
  }

  function createPreviewRequestId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `web-preview-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function fidelityLevel(value, allowed) {
    const raw = typeof value === "object" && value ? value.level : value;
    return typeof raw === "string" && allowed.has(raw) ? raw : "unknown";
  }

  function fidelityLabel(value) {
    const labels = {
      high: "高",
      medium: "中",
      low: "低",
      unknown: "未知",
      not_assessed: "未评估",
      not_applicable: "不适用",
      style_color_reference_only: "仅风格与配色参考"
    };
    return labels[value] || "未知";
  }

  function validateImageProvider(raw) {
    if (!raw || typeof raw !== "object" || (raw.status !== "ok" && raw.status !== "fallback")) throw new Error("图片 Provider 状态不完整");
    if (typeof raw.attempted !== "boolean") throw new Error("图片 Provider 尝试状态不完整");
    if (raw.requested_model !== imageAssetRuntime.IMAGE_MODEL || raw.transport_model !== imageAssetRuntime.IMAGE_MODEL) {
      throw new Error("图片 Provider 模型合同不匹配");
    }
    const successBasis = raw.status === "ok" ? imageAssetRuntime.staticImageSuccessBasis(raw) : null;
    if (raw.status === "ok" && successBasis === null) {
      throw new Error("图片 Provider 成功状态缺少可信来源证据");
    }
    if (
      raw.status === "fallback"
      && (
        raw.degraded !== true
        || raw.model_verified !== false
        || raw.resolved_model !== null
        || raw.verification_basis !== null
        || typeof raw.request_model_pinned !== "boolean"
        || typeof raw.cpa_trace_verified !== "boolean"
        || typeof raw.model_reported !== "boolean"
        || typeof raw.reason_code !== "string"
        || raw.reason_code.length === 0
      )
    ) {
      throw new Error("图片 Provider 失败状态不完整");
    }
    return {
      status: raw.status,
      attempted: raw.attempted,
      requested_model: raw.requested_model,
      transport_model: raw.transport_model,
      resolved_model: typeof raw.resolved_model === "string" ? raw.resolved_model : null,
      model_reported: raw.model_reported === true,
      model_verified: raw.model_verified === true,
      request_model_pinned: raw.request_model_pinned === true,
      cpa_trace_verified: raw.cpa_trace_verified === true,
      verification_basis: typeof raw.verification_basis === "string" ? raw.verification_basis : null,
      provenance_label: successBasis === null ? null : imageAssetRuntime.imageProvenanceLabel(successBasis),
      degraded: raw.degraded === true,
      reason_code: typeof raw.reason_code === "string" ? raw.reason_code : null
    };
  }

  function normalizeStatic2dPreview(raw, expected) {
    if (!raw || typeof raw !== "object") throw new Error("2D 端点未返回对象");
    if (!raw.preview_id || raw.request_id !== expected.requestId) throw new Error("2D 回执标识不匹配");
    if (raw.user_id !== userId || raw.styling_session_id !== expected.stylingSessionId || raw.look_version_id !== expected.lookVersionId) {
      throw new Error("2D 回执归属不匹配");
    }
    if (raw.identity_asset_id !== null) throw new Error("2D 回执不得绑定身份图像");
    if (raw.render_mode !== "static_2d" || !["succeeded", "degraded", "failed"].includes(raw.status)) throw new Error("2D 回执模式或状态不合法");
    const ownedGarmentIds = strictIdList(raw.owned_garment_ids);
    const externalItemIds = strictIdList(raw.external_item_ids);
    if (ownedGarmentIds === null || !sameIdSet(ownedGarmentIds, expected.itemIds) || externalItemIds === null || externalItemIds.length !== 0) {
      throw new Error("2D 回执衣物集合未通过白名单复核");
    }
    const provider = validateImageProvider(raw.provider);
    if (raw.status === "succeeded" && provider.status !== "ok") throw new Error("2D 成功回执不得使用失败 Provider");
    if (raw.status !== "succeeded" && provider.status !== "fallback") throw new Error("2D 失败回执不得冒充 Provider 成功");
    if (!raw.scene_id || !raw.trace_id || !raw.ai_label || typeof raw.ai_label !== "object") throw new Error("2D 回执缺少场景、Trace 或 AI 标识");
    if (!raw.fidelity || typeof raw.fidelity !== "object") throw new Error("2D 回执缺少可信度");
    if (raw.fidelity.identity !== "not_assessed") throw new Error("2D 回执不得宣称身份一致性");
    if (raw.status === "succeeded" && !raw.asset_id) throw new Error("2D 成功回执缺少图像资产");
    if (raw.status !== "succeeded" && raw.asset_id) throw new Error("2D 降级或失败回执不得冒充成功图像");
    if (raw.status === "degraded" && !raw.fallback) throw new Error("2D 降级回执缺少 fallback");
    return {
      preview_id: raw.preview_id,
      request_id: raw.request_id,
      user_id: raw.user_id,
      styling_session_id: raw.styling_session_id,
      look_version_id: raw.look_version_id,
      render_mode: raw.render_mode,
      owned_garment_ids: ownedGarmentIds,
      external_item_ids: [],
      scene_id: raw.scene_id,
      asset_id: raw.asset_id || null,
      status: raw.status,
      provider,
      fidelity: {
        identity: fidelityLevel(raw.fidelity.identity, new Set(["high", "medium", "low", "unknown", "not_assessed", "not_applicable"])),
        garment: fidelityLevel(raw.fidelity.garment, new Set(["high", "medium", "low", "unknown", "style_color_reference_only"])),
        fit: fidelityLevel(raw.fidelity.fit, new Set(["high", "medium", "low", "unknown"])),
        material: fidelityLevel(raw.fidelity.material, new Set(["high", "medium", "low", "unknown"])),
        drape: fidelityLevel(raw.fidelity.drape, new Set(["high", "medium", "low", "unknown"]))
      },
      ai_label: raw.ai_label,
      fallback: raw.fallback || null,
      trace_id: raw.trace_id
    };
  }

  function preview2dImageUrl(preview) {
    const query = new URLSearchParams({
      user_id: userId,
      styling_session_id: preview.styling_session_id,
      v: preview.request_id
    });
    return `/preview/static-2d/${encodeURIComponent(preview.preview_id)}/image?${query.toString()}`;
  }

  function renderPreview2d() {
    const status = byId("preview2d-status");
    const result = byId("preview2d-result");
    const image = byId("preview2d-image");
    const caption = byId("preview2d-caption");
    const origin = byId("preview2d-origin");
    const generate = byId("preview2d-generate");
    const remove = byId("preview2d-delete");
    generate.disabled = state.source !== "api" || !state.activeLook || state.preview2dPending;
    result.hidden = true;
    remove.hidden = true;
    status.removeAttribute("data-state");

    if (state.preview2dPending) {
      status.dataset.state = "generating";
      status.textContent = "CPA 正在生成中性模特/平铺的 2D 静态参考图；不会使用身份图像。";
      origin.textContent = "CPA 生成中";
      generate.textContent = "正在生成…";
      return;
    }
    generate.textContent = state.preview2d ? "为当前 Look 重新生成" : "生成 2D 静态参考图";
    const preview = state.preview2d;
    if (!preview) {
      origin.textContent = "等待生成";
      status.textContent = state.activeLook ? "已准备好；只使用当前不可变 Look，生成中性模特/平铺参考。" : "建立 Look 后即可生成。";
      image.removeAttribute("src");
      return;
    }
    if (preview.status === "deleted") {
      origin.textContent = "已删除";
      status.textContent = "生成图已删除；当前 Look 与历史版本仍保留。";
      image.removeAttribute("src");
      return;
    }

    const provenanceBasis = imageAssetRuntime.staticImageSuccessBasis(preview.provider);
    const generated = provenanceBasis !== null;
    const provenanceLabel = imageAssetRuntime.imageProvenanceLabel(provenanceBasis);
    origin.textContent = generated ? `CPA 生成 · ${provenanceLabel}` : "本地参考";
    const stale = state.activeLook && preview.look_version_id !== lookVersionId(state.activeLook);
    if (preview.preview_id) remove.hidden = false;
    if (preview.status === "succeeded" && generated) {
      status.textContent = stale ? "中性模特/平铺参考图已完成，但它属于较早 Look；可为当前版本重新生成。" : "中性模特/平铺 2D 静态参考图已完成；未评估身份一致性。";
      result.hidden = false;
      const url = preview2dImageUrl(preview);
      image.onerror = () => {
        image.removeAttribute("src");
        result.hidden = true;
        status.dataset.state = "degraded";
        status.textContent = "生成记录已完成，但图像暂时无法安全读取；当前 Look 和文字建议仍可使用。";
      };
      if (image.getAttribute("src") !== url) image.src = url;
      caption.textContent = `${stale ? "较早 Look" : "当前 Look"} · ${provenanceLabel} · 中性模特/平铺，未评估身份一致性 · 衣物呈现 ${fidelityLabel(preview.fidelity.garment)} · 尺码、面料与垂坠仅作参考。`;
      return;
    }
    image.removeAttribute("src");
    status.dataset.state = preview.status;
    status.textContent = preview.status === "degraded"
      ? "CPA 未返回可用的中性模特/平铺图，当前改为显示衣物组合与文字解释；未评估身份一致性。"
      : "本次中性模特/平铺 2D 生成未完成；未评估身份一致性，当前 Look 和文字建议仍可继续使用。";
  }

  async function generateStatic2dPreview() {
    const look = state.activeLook;
    if (state.source !== "api" || !look || !state.stylingSessionId || state.preview2dPending) return;
    const requestId = createPreviewRequestId();
    const lookVersion = lookVersionId(look);
    const expected = { requestId, stylingSessionId: state.stylingSessionId, lookVersionId: lookVersion, itemIds: [...look.item_ids] };
    const payload = {
      user_id: userId,
      styling_session_id: state.stylingSessionId,
      look_version_id: lookVersion,
      render_mode: "static_2d",
      consent: true,
      identity_asset_id: null,
      request_id: requestId
    };
    state.preview2dPending = true;
    renderPreview2d();
    try {
      state.preview2d = normalizeStatic2dPreview(await api("/preview/static-2d", {
        method: "POST",
        body: JSON.stringify(payload),
        timeoutMs: PREVIEW_2D_TIMEOUT_MS
      }), expected);
      await fetchTrace(state.preview2d.trace_id);
    } catch (error) {
      state.preview2d = {
        status: "failed",
        look_version_id: lookVersion,
        provider: {
          status: "fallback",
          attempted: true,
          requested_model: imageAssetRuntime.IMAGE_MODEL,
          transport_model: imageAssetRuntime.IMAGE_MODEL,
          resolved_model: null,
          model_reported: false,
          model_verified: false,
          request_model_pinned: false,
          cpa_trace_verified: false,
          verification_basis: null,
          degraded: true
        }
      };
      showToast("2D 生成未完成，已保留当前 Look");
    } finally {
      state.preview2dPending = false;
      renderPreview2d();
      setCocreationControls();
    }
  }

  async function deleteStatic2dPreview() {
    const preview = state.preview2d;
    if (!preview?.preview_id || preview.styling_session_id !== state.stylingSessionId) return;
    if (!window.confirm("删除这张 AI 生成图吗？当前 Look 与历史版本不会被删除。")) return;
    const button = byId("preview2d-delete");
    button.disabled = true;
    try {
      const query = new URLSearchParams({ user_id: userId, styling_session_id: state.stylingSessionId });
      const response = await api(`/preview/static-2d/${encodeURIComponent(preview.preview_id)}?${query.toString()}`, { method: "DELETE" });
      const deletedId = response.deleted_id || response.preview_id;
      if (deletedId && deletedId !== preview.preview_id) throw new Error("2D 删除回执不匹配");
      state.preview2d = { status: "deleted", look_version_id: preview.look_version_id };
      renderPreview2d();
      if (response.trace_id) await fetchTrace(response.trace_id);
    } catch (error) {
      showToast("生成图删除未完成，请稍后重试");
    } finally {
      button.disabled = false;
    }
  }

  function previewAssetFile(file, statusText) {
    if (state.assetPreviewUrl) URL.revokeObjectURL(state.assetPreviewUrl);
    state.assetPreviewUrl = URL.createObjectURL(file);
    state.assetPreviewAssetId = null;
    byId("asset-preview").src = state.assetPreviewUrl;
    byId("asset-preview-wrap").hidden = false;
    byId("asset-status").textContent = statusText || "本机临时预览；尚未上传。";
  }

  async function handleAssetSubmit(event) {
    event.preventDefault();
    if (!state.activeLook || state.source !== "api" || isFinalized()) return;
    const parentLook = state.activeLook;
    const parentVersionId = lookVersionId(parentLook);
    const currentAssetIds = availableLookAssetIds(parentLook);
    if (currentAssetIds.length >= 8) {
      showToast("当前 Look 已达到 8 张静态图上限");
      return;
    }
    const file = byId("asset-file").files?.[0];
    const allowedTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
    if (!file || !allowedTypes.has(file.type) || file.size > 5 * 1024 * 1024) {
      showToast("仅接受 PNG/JPEG/WebP 静态图片，且不超过 5 MB");
      return;
    }
    showAssetOperation("");
    previewAssetFile(file, "正在上传静态图；原图不会进入 Debug。" );
    const button = byId("asset-submit");
    button.disabled = true;
    const form = new FormData();
    form.append("user_id", userId);
    form.append("styling_session_id", state.stylingSessionId);
    form.append("angle", byId("asset-angle").value);
    form.append("consent", "true");
    form.append("purpose", "styling_assessment");
    form.append("file", file, file.name);
    let uploadedAsset = null;
    let assetBound = false;
    try {
      const response = await api("/assets", { method: "POST", body: form, timeoutMs: 20000 });
      const asset = normalizeAsset(response);
      if (!asset) throw new Error("资产响应未通过归属、会话、显式同意、用途与保留策略复核");
      uploadedAsset = asset;
      const nextAssetIds = [...new Set([...currentAssetIds, asset.asset_id])];
      const revisionResponse = await api("/look", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          created_from: "user_revision",
          parent_version_id: parentVersionId,
          item_ids: [...parentLook.item_ids],
          asset_ids: nextAssetIds
        })
      });
      const look = validateLook(revisionResponse);
      const versionAdvanced = Number(look?.version_index) > Number(parentLook.version_index);
      if (!look
        || look.parent_version_id !== parentVersionId
        || !sameIdSet(look.item_ids, parentLook.item_ids)
        || !sameIdSet(lookAssetIds(look), nextAssetIds)
        || !versionAdvanced) {
        throw new Error("图片未能绑定到符合版本链约束的新 Look");
      }
      state.activeLook = look;
      state.assetPreviewAssetId = asset.asset_id;
      syncAssetsToLook(look, [asset]);
      assetBound = true;
      state.scorecard = null;
      state.adjustments = [];
      byId("asset-file").value = "";
      byId("asset-status").textContent = assetStatusText(asset);
      showAssetOperation(`图片已绑定到 ${versionLabel(look)}，用途仅限本次穿搭评估。`);
      await fetchTrace(asset.trace_id || response.trace_id);
      await refreshLooks();
      renderCocreation();
      await fetchTrace(look.trace_id || revisionResponse.trace_id);
      await requestScorecard();
      appendMessage("Stylist", `静态图已绑定到 ${versionLabel(look)}；旧版本保持不变，评分只针对新版本。`);
    } catch (error) {
      if (uploadedAsset && !assetBound) {
        try {
          const cleanupQuery = new URLSearchParams({ user_id: userId, styling_session_id: uploadedAsset.styling_session_id });
          const cleanup = await api(`/assets/${encodeURIComponent(uploadedAsset.asset_id)}?${cleanupQuery.toString()}`, { method: "DELETE" });
          if (cleanup.deleted_id !== uploadedAsset.asset_id) throw new Error("清理回执的资产 ID 不匹配");
          state.deletedAssetIds.add(uploadedAsset.asset_id);
          clearAssetPreview();
          byId("asset-status").textContent = `图片未能绑定到 Look：${error.message}。临时资产已由服务端清除。`;
          showAssetOperation(`图片未能绑定到 Look：${error.message}。临时资产已由服务端清除。`);
          await fetchTrace(cleanup.trace_id);
        } catch (cleanupError) {
          state.assetPreviewAssetId = uploadedAsset.asset_id;
          state.unboundAssets = [...state.unboundAssets.filter((asset) => asset.asset_id !== uploadedAsset.asset_id), uploadedAsset];
          byId("asset-file").value = "";
          byId("asset-status").textContent = `图片未能绑定到 Look：${error.message}。自动清理也未完成（${cleanupError.message}）；临时资产将在服务进程结束时清除，可用下方“删除图片”重试。`;
          showAssetOperation(`图片未绑定，自动清理未完成；临时资产将在服务进程结束时清除，可用“删除图片”重试。`);
          renderAssetList();
        }
      } else {
        state.assetPreviewAssetId = null;
        byId("asset-status").textContent = `上传未完成：${error.message}。本地预览不会被当作服务端资产。`;
        showAssetOperation(`上传未完成：${error.message}。本地预览未进入服务端评估。`);
      }
      showToast("静态图未进入评估链路");
    } finally {
      setCocreationControls();
    }
  }

  function safeStylingText(value, fallback = "穿搭评估内容已按安全边界省略。") {
    const text = String(value || "").trim();
    if (!text) return fallback;
    return /(颜值|身材|年龄|性吸引力|长相|脸蛋|身体评分|性感分)/i.test(text) ? fallback : text;
  }

  function asList(value) {
    if (Array.isArray(value)) return value;
    return value === undefined || value === null || value === "" ? [] : [value];
  }

  function dimensionMap(scorecard) {
    const source = scorecard?.dimensions || scorecard?.dimension_scores || {};
    if (Array.isArray(source)) return new Map(source.map((item) => [item.dimension || item.key || item.name, item]));
    return new Map(Object.entries(source));
  }

  function fixedEvidenceForDimension(key) {
    if (key === "occasion_fit") {
      return state.scene?.occasion ? `仅核对衣物元数据与 ${state.scene.occasion} 场合要求。` : null;
    }
    return fixedDimensionEvidence[key] || null;
  }

  function prohibitedScorecard(scorecard) {
    const checks = scorecard?.prohibited_subject_checks || {};
    return Object.values(checks).some((value) => value === true);
  }

  function normalizeAdjustments(raw) {
    const shoppingBlocked = state.scene?.urgency === "high" || state.scene?.shopping_allowed === false;
    return (raw || [])
      .filter((item) => item?.adjustment_id && (item.action || item.canonical_action))
      .filter((item) => {
        const regions = asList(item.evidence_regions);
        return item.evidence_source === "verified_visual"
          && regions.length > 0
          && regions.every((region) => Object.prototype.hasOwnProperty.call(evidenceRegionLabels, region));
      })
      .filter((item) => !state.rejectedCanonical.has(adjustmentKey(item)))
      .filter((item) => !(shoppingBlocked && item.cost_level === "optional_purchase"))
      .slice(0, 2);
  }

  async function requestScorecard(options = {}) {
    const allowFinalized = options?.allowFinalized === true;
    if (!state.activeLook || state.source !== "api" || (isFinalized() && !allowFinalized)) return;
    const button = byId("score-request");
    button.disabled = true;
    byId("scorecard-summary").replaceChildren(make("p", "empty-state compact", "正在核对这套穿搭在当前目标下的证据……"));
    try {
      const response = await api("/scorecard", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          look_version_id: lookVersionId(state.activeLook),
          asset_ids: availableLookAssetIds()
        }),
        timeoutMs: 20000
      });
      const scorecard = response?.scorecard || response;
      if (!scorecard || prohibitedScorecard(scorecard)) throw new Error("评分卡违反固定穿搭评估边界");
      if (scorecard.look_version_id && scorecard.look_version_id !== lookVersionId(state.activeLook)) throw new Error("评分卡未关联当前 Look");
      state.scorecard = scorecard;
      state.adjustments = normalizeAdjustments(response.priority_adjustments || scorecard.priority_adjustments || []);
      renderScorecard();
      renderAdjustments();
      await refreshLookComparison(state.activeLook);
      await fetchTrace(scorecard.trace_id || response.trace_id);
    } catch (error) {
      state.scorecard = null;
      state.adjustments = [];
      byId("scorecard-summary").replaceChildren(make("p", "qualitative-banner", `评分暂不可用：${safeStylingText(error.message, "服务端未返回安全评分卡。")}`));
      renderAdjustments();
    } finally {
      setCocreationControls();
    }
  }

  function renderScorecard() {
    const summary = byId("scorecard-summary");
    const grid = byId("scorecard-grid");
    const evidence = byId("missing-evidence");
    const scorecard = state.scorecard;
    grid.replaceChildren();
    evidence.hidden = true;
    evidence.replaceChildren();
    if (!scorecard) {
      summary.replaceChildren(make("p", "empty-state compact", state.activeLook ? "可上传静态图片后评估；没有视觉证据时只给定性结果。" : "建立 Look 后可评估。"));
      return;
    }
    const activeAssetIds = availableLookAssetIds();
    const usableVisualEvidence = activeAssetIds.length > 0
      && state.assets.length === activeAssetIds.length
      && state.assets.every((asset) => asset.visual_quality === "usable");
    const numeric = usableVisualEvidence
      && scorecard.numeric_score_available === true
      && Number.isFinite(Number(scorecard.total_score));
    const keepPointSource = scorecard.keep_point;
    const keepPointBasis = keepPointSource?.evidence_basis;
    const keepPointStatement = typeof keepPointSource?.statement === "string"
      && ["grounded_metadata", "verified_visual"].includes(keepPointBasis)
      ? safeStylingText(keepPointSource.statement, "")
      : "";
    const keepPointCard = keepPointStatement ? make("article", "keep-point") : null;
    if (keepPointCard) {
      keepPointCard.append(
        make("strong", "", safeStylingText(keepPointSource.label, "值得保留")),
        make("p", "", keepPointStatement),
        make("span", "", keepPointBasis === "verified_visual" ? "依据：已验证视觉证据" : "依据：衣橱与场景元数据")
      );
    }
    if (numeric) {
      const total = make("div", "score-total");
      total.append(make("strong", "", Number(scorecard.total_score)), make("span", "", "这套穿搭在当前目标下的情境总分"));
      summary.replaceChildren(...(keepPointCard ? [keepPointCard, total] : [total]));
    } else {
      const qualitative = make("p", "qualitative-banner", "视觉证据不足、质量 limited 或质量元数据未报告，本轮不显示精确数值；以下只保留可见部分的定性判断。");
      summary.replaceChildren(...(keepPointCard ? [keepPointCard, qualitative] : [qualitative]));
    }
    const dimensions = dimensionMap(scorecard);
    grid.replaceChildren(...scoreDimensions.map(([key, label]) => {
      const raw = dimensions.get(key) || {};
      const card = make("article", "score-dimension");
      card.append(make("h4", "", `这套穿搭在当前目标下 · ${label}`));
      const expectedEvidence = fixedEvidenceForDimension(key);
      const returnedEvidence = asList(raw.evidence);
      const evidenceText = expectedEvidence && returnedEvidence.length === 1 && returnedEvidence[0] === expectedEvidence
        ? expectedEvidence
        : "固定衣物元数据证据暂不可用；未展示未知视觉观察。";
      card.append(make("p", "", evidenceText));
      const value = raw.score ?? raw.value;
      if (numeric && Number.isFinite(Number(value))) card.append(make("span", "dimension-score", String(Number(value))));
      return card;
    }));
    const missing = [...asList(scorecard.missing_evidence), ...asList(scorecard.capture_guidance || scorecard.retake_guidance), ...asList(scorecard.guidance)].map((item) => safeStylingText(item)).filter(Boolean);
    if (missing.length) {
      evidence.hidden = false;
      evidence.append(make("strong", "", "补充证据建议"));
      const list = make("ul"); missing.forEach((item) => list.append(make("li", "", item))); evidence.append(list);
    }
  }

  function adjustmentKey(adjustment) {
    return String(adjustment?.canonical_key || adjustment?.canonical_action || adjustment?.action || adjustment?.adjustment_id || "").trim().toLowerCase();
  }

  function renderAdjustments() {
    const list = byId("adjustment-list");
    const adjustments = normalizeAdjustments(state.adjustments);
    state.adjustments = adjustments;
    byId("adjustment-count").textContent = `${adjustments.length} 项`;
    if (isFinalized()) {
      list.replaceChildren(make("p", "finalized-note", "已按你的满意决定定稿，不再继续提出调整。"));
      return;
    }
    if (!adjustments.length) {
      list.replaceChildren(make("p", "empty-state compact", state.scorecard ? "本轮没有需要继续推进的高价值调整。" : "获得评分卡后显示可验证调整。"));
      return;
    }
    list.replaceChildren(...adjustments.map((adjustment) => {
      const card = make("article", "adjustment-item");
      card.append(make("h4", "", safeStylingText(adjustment.action || adjustment.canonical_action)));
      card.append(make("p", "", safeStylingText(adjustment.reason, "这项调整只围绕当前场景目标。")));
      const meta = make("div", "adjustment-meta");
      (adjustment.expected_dimensions || []).filter((key) => scoreDimensions.some(([allowed]) => allowed === key)).forEach((key) => meta.append(make("span", "", scoreDimensions.find(([allowed]) => allowed === key)?.[1])));
      const regions = asList(adjustment.evidence_regions)
        .filter((region) => Object.prototype.hasOwnProperty.call(evidenceRegionLabels, region))
        .map((region) => evidenceRegionLabels[region]);
      if (adjustment.evidence_source === "verified_visual" && regions.length) meta.append(make("span", "", `可见证据：${regions.join("、")}`));
      if (adjustment.cost_level) meta.append(make("span", "", `操作成本：${safeStylingText(adjustment.cost_level)}`));
      card.append(meta);
      const reason = make("textarea", "adjustment-reason");
      reason.rows = 2;
      reason.maxLength = 240;
      reason.placeholder = "可选：说明接受、拒绝或部分接受的原因";
      card.append(reason);
      const row = make("div", "decision-row");
      [["accept", "接受", "button button-primary"], ["partial", "部分接受", "button button-secondary"], ["reject", "拒绝", "button button-danger"]].forEach(([decision, label, className]) => {
        const button = make("button", className, label);
        button.type = "button";
        button.addEventListener("click", () => submitAdjustment(adjustment, decision, reason.value, button));
        row.append(button);
      });
      card.append(row);
      return card;
    }));
  }

  async function submitAdjustment(adjustment, decision, reason, button) {
    if (!state.activeLook || isFinalized()) return;
    const key = adjustmentKey(adjustment);
    const previousAdjustments = [...state.adjustments];
    const wasAlreadyRejected = state.rejectedCanonical.has(key);
    if (decision === "reject") {
      state.rejectedCanonical.add(key);
      state.adjustments = state.adjustments.filter((item) => adjustmentKey(item) !== key);
      renderAdjustments();
    } else {
      button.disabled = true;
    }
    try {
      const payload = {
        user_id: userId,
        styling_session_id: state.stylingSessionId,
        look_version_id: lookVersionId(state.activeLook),
        adjustment_id: adjustment.adjustment_id,
        decision,
        reason: reason || undefined
      };
      if (decision !== "reject") payload.asset_ids = availableLookAssetIds();
      const response = await api("/adjust", {
        method: "POST",
        body: JSON.stringify(payload)
      });
      mergeAdjustmentDecisionRecord(response.decision_record);
      const returnedLook = response.look_version || response.look;
      const responseLook = returnedLook ? validateLook(returnedLook) : null;
      if (returnedLook && !responseLook) throw new Error("调整后的 Look 未通过白名单与槽位复核");
      if (decision !== "reject" && !responseLook) throw new Error("服务端未返回调整后的新 Look");
      if (responseLook) {
        state.activeLook = responseLook;
        syncAssetsToLook(responseLook);
      }
      const returnedScorecard = response.scorecard || null;
      if (returnedScorecard && !prohibitedScorecard(returnedScorecard)) state.scorecard = returnedScorecard;
      state.adjustments = normalizeAdjustments(response.priority_adjustments || returnedScorecard?.priority_adjustments || state.adjustments);
      await refreshLooks();
      renderCocreation();
      if (responseLook && !returnedScorecard) await requestScorecard();
      await fetchTrace(response.trace_id || responseLook?.trace_id || returnedScorecard?.trace_id);
      appendMessage("Stylist", decision === "reject" ? "已记录拒绝；本会话不会换一种说法重复这项建议。" : `已记录${decision === "accept" ? "接受" : "部分接受"}，并刷新当前版本。`);
    } catch (error) {
      showToast(`调整记录未完成：${error.message}`);
      if (decision === "reject") {
        if (!wasAlreadyRejected) state.rejectedCanonical.delete(key);
        state.adjustments = previousAdjustments;
        renderAdjustments();
      } else {
        button.disabled = false;
      }
    }
  }

  function renderModifyFields() {
    const target = byId("user-modify-fields");
    target.replaceChildren();
    state.modifyOptions = [];
    if (!state.activeLook) return;
    const currentIds = state.activeLook.item_ids;
    const currentItems = currentIds.map((id) => garmentMap().get(id)).filter(Boolean);
    const slots = currentItems.some((item) => item.slot === "dress") ? ["dress", "shoes"] : ["top", "bottom", "shoes"];
    ["outer", "bag"].forEach((slot) => { if (currentItems.some((item) => item.slot === slot)) slots.push(slot); });
    slots.forEach((slot) => {
      const field = make("label", "modify-field", slotLabels[slot] || slot);
      const select = make("select", "modify-select");
      select.dataset.slot = slot;
      const candidates = wardrobeItems().filter((item) => item.slot === slot && isAllowedGarment(item.garment_id, state.scene));
      candidates.forEach((item) => {
        const index = state.modifyOptions.push(item) - 1;
        const option = make("option", "", item.name);
        option.value = String(index);
        if (currentIds.includes(item.garment_id)) option.selected = true;
        select.append(option);
      });
      select.disabled = isFinalized() || !candidates.length;
      field.append(select);
      target.append(field);
    });
  }

  async function handleUserModify(event) {
    event.preventDefault();
    if (!state.activeLook || isFinalized()) return;
    const selectors = [...byId("user-modify-fields").querySelectorAll("select")];
    const replacedSlots = new Set(selectors.map((select) => select.dataset.slot));
    const selected = selectors.map((select) => state.modifyOptions[Number(select.value)]).filter(Boolean);
    const preserved = state.activeLook.item_ids.filter((id) => !replacedSlots.has(garmentMap().get(id)?.slot));
    const itemIds = [...new Set([...preserved, ...selected.map((item) => item.garment_id)])];
    if (!itemIds.length || itemIds.some((id) => !isAllowedGarment(id, state.scene)) || !isCompleteIds(itemIds)) {
      showToast("当前组合未通过白名单与必需槽位复核");
      return;
    }
    if (itemIds.length === state.activeLook.item_ids.length && itemIds.every((id) => state.activeLook.item_ids.includes(id))) {
      showToast("当前组合没有变化");
      return;
    }
    const button = byId("user-modify-submit");
    button.disabled = true;
    try {
      const response = await api("/adjust", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          look_version_id: lookVersionId(state.activeLook),
          decision: "user_modified",
          reason: "用户从当前可用衣橱按名称修改",
          item_ids: itemIds,
          asset_ids: availableLookAssetIds()
        })
      });
      mergeAdjustmentDecisionRecord(response.decision_record);
      const look = validateLook(response.look_version || response.look || response);
      if (!look) throw new Error("用户修改后的 Look 未通过白名单与槽位复核");
      state.activeLook = look;
      syncAssetsToLook(look);
      state.scorecard = response.scorecard && !prohibitedScorecard(response.scorecard) ? response.scorecard : null;
      state.adjustments = normalizeAdjustments(response.priority_adjustments || []);
      await refreshLooks();
      renderCocreation();
      if (!state.scorecard) await requestScorecard();
      await fetchTrace(response.trace_id || look.trace_id);
      appendMessage("Stylist", `已按你选择的衣橱单品建立 ${versionLabel(look)}，未要求变化的部分保持不变。`);
    } catch (error) {
      showToast(`用户修改未完成：${error.message}`);
    } finally {
      setCocreationControls();
    }
  }

  function renderFinalizeState() {
    const form = byId("finalize-form");
    form.querySelector(".finalized-note")?.remove();
    if (isFinalized()) {
      const note = make("p", "finalized-note", "Final 已保存。系统不会为了提高分数继续施压修改。" );
      form.prepend(note);
      byId("finalize-submit").textContent = "已定稿";
    } else {
      byId("finalize-submit").textContent = "满意定稿";
    }
    setCocreationControls();
  }

  async function handleFinalize(event) {
    event.preventDefault();
    if (!state.activeLook || isFinalized()) return;
    const button = byId("finalize-submit");
    button.disabled = true;
    try {
      const response = await api("/finalize", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId,
          look_version_id: lookVersionId(state.activeLook),
          satisfied: true,
          satisfaction: Number(byId("satisfaction").value),
          reason: byId("final-reason").value.trim() || undefined
        })
      });
      const record = response.final_look || response.finalization || response.record || response;
      const expectedIds = [...state.activeLook.item_ids].sort();
      const returnedIds = asList(record?.item_ids).sort();
      const finalRecordValid = record?.satisfied === true
        && record?.advice_stopped === true
        && record?.user_id === userId
        && record?.styling_session_id === state.stylingSessionId
        && record?.look_version_id === lookVersionId(state.activeLook)
        && returnedIds.length === expectedIds.length
        && returnedIds.every((id, index) => id === expectedIds[index])
        && returnedIds.every((id) => isAllowedGarment(id, state.scene))
        && isCompleteIds(returnedIds);
      if (!finalRecordValid) throw new Error("定稿响应未通过身份、版本与白名单验证");
      const finalLook = { ...state.activeLook, asset_ids: asList(record.asset_ids), status: "final" };
      state.activeLook = finalLook;
      syncAssetsToLook(finalLook);
      state.finalization = record;
      state.finalized = true;
      state.adjustments = [];
      await refreshLooks();
      renderActiveLook();
      renderCocreation();
      await fetchTrace(response.trace_id || finalLook.trace_id || record.trace_id);
      appendMessage("Stylist", "已按你的满意决定保存 Final Look。我们停在这里，不再继续挑问题。" );
    } catch (error) {
      showToast(`定稿未完成：${error.message}`);
      button.disabled = false;
    }
  }

  function memoryTemplatesFor(memoryType) {
    return approvedMemoryTemplates[memoryType] || [];
  }

  function isApprovedMemoryTemplate(memoryType, content) {
    return memoryTemplatesFor(memoryType).includes(content);
  }

  function renderMemoryTemplateOptions(selectedContent) {
    const type = byId("memory-type").value;
    const select = byId("memory-content");
    const templates = memoryTemplatesFor(type);
    select.replaceChildren(...templates.map((content) => {
      const option = make("option", "", content);
      option.value = content;
      if (content === selectedContent) option.selected = true;
      return option;
    }));
    select.disabled = !state.memoryAvailable || templates.length === 0;
  }

  function setMemoryAvailability(available, detail) {
    state.memoryAvailable = available;
    byId("memory-api-state").textContent = available ? "API 可用" : (detail || "暂不可用");
    ["memory-namespace", "memory-type", "memory-content", "memory-sensitive", "memory-propose-submit"].forEach((id) => { byId(id).disabled = !available; });
    renderMemoryTemplateOptions();
    if (!available) {
      state.memoryProposals = [];
      state.memoryRecords = [];
      renderMemory();
    }
  }

  function normalizeMemoryProposal(proposal) {
    if (!proposal?.proposal_id || proposal.user_id !== userId || !["shared", "stylist"].includes(proposal.namespace)) return null;
    const content = String(proposal.content || "");
    const blocked = proposal.sensitivity === "sensitive"
      || proposal.commit_blocked === true
      || !isApprovedMemoryTemplate(proposal.type, content);
    return { ...proposal, content: blocked ? null : content, sensitivity: blocked ? "sensitive" : proposal.sensitivity, commit_blocked: blocked };
  }

  function normalizeMemoryRecord(record) {
    if (!record?.memory_id || record.user_id !== userId || !["shared", "stylist"].includes(record.namespace)) return null;
    const sensitive = record.sensitivity === "sensitive";
    const content = String(record.content || "");
    if (sensitive || !isApprovedMemoryTemplate(record.type, content)) return null;
    return { ...record, content, sensitivity: record.sensitivity };
  }

  async function loadMemory() {
    try {
      const response = await api(`/memory?user_id=${encodeURIComponent(userId)}`);
      state.memoryProposals = (response.proposals || []).map(normalizeMemoryProposal).filter(Boolean);
      state.memoryRecords = (response.records || []).map(normalizeMemoryRecord).filter(Boolean);
      setMemoryAvailability(true);
      renderMemory();
    } catch (error) {
      setMemoryAvailability(false, "Memory API 暂不可用");
      byId("memory-write-status").hidden = false;
      byId("memory-write-status").textContent = `未写入：${error.message}`;
    }
  }

  function memoryMeta(item) {
    const meta = make("div", "memory-meta");
    meta.append(make("span", "", item.namespace === "shared" ? "团队共享" : "仅 Stylist"), make("span", "", item.type || "偏好"), make("span", "", item.status || "待确认"));
    return meta;
  }

  function renderMemory() {
    const pending = byId("memory-pending-list");
    const committed = byId("memory-committed-list");
    const proposals = state.memoryProposals.filter((item) => ["proposed", "pending"].includes(item.status));
    if (!proposals.length) pending.replaceChildren(make("p", "empty-state compact", "暂无待确认提议。"));
    else pending.replaceChildren(...proposals.map((proposal) => {
      const card = make("article", "memory-item");
      card.append(make("h4", "", proposal.sensitivity === "sensitive" ? "敏感提议 · 未写入" : "待确认记忆"), memoryMeta(proposal));
      if (proposal.sensitivity === "sensitive") {
        card.append(make("p", "", "内容已被敏感门控拦截，不在界面或 Debug 回显。"));
        const reject = make("button", "button button-danger button-small", "关闭这条提议"); reject.type = "button"; reject.addEventListener("click", () => decideMemory(proposal, "reject", null, reject)); card.append(reject);
      } else {
        const templates = memoryTemplatesFor(proposal.type);
        const editLabel = make("label", "memory-template-edit", "编辑为同类型受控模板");
        const edit = make("select", "memory-edit");
        templates.forEach((content) => {
          const option = make("option", "", content); option.value = content; option.selected = content === proposal.content; edit.append(option);
        });
        edit.disabled = templates.length === 0;
        editLabel.append(edit);
        card.append(editLabel);
        const actions = make("div", "memory-actions");
        const confirm = make("button", "button button-primary button-small", "确认提交"); confirm.type = "button"; confirm.addEventListener("click", () => decideMemory(proposal, "confirm", proposal.content, confirm));
        confirm.disabled = !isApprovedMemoryTemplate(proposal.type, proposal.content);
        const save = make("button", "button button-secondary button-small", "保存模板并确认"); save.type = "button"; save.disabled = templates.length === 0; save.addEventListener("click", () => decideMemory(proposal, "edit", edit.value, save));
        const reject = make("button", "button button-danger button-small", "拒绝"); reject.type = "button"; reject.addEventListener("click", () => decideMemory(proposal, "reject", null, reject));
        actions.append(confirm, save, reject); card.append(actions);
      }
      return card;
    }));
    if (!state.memoryRecords.length) committed.replaceChildren(make("p", "empty-state compact", "暂无已提交记忆。"));
    else committed.replaceChildren(...state.memoryRecords.map((record) => {
      const card = make("article", "memory-item");
      card.append(make("h4", "", "已提交记忆"), memoryMeta(record));
      card.append(make("p", "", record.sensitivity === "sensitive" ? "敏感内容不在此界面回显。" : record.content));
      const remove = make("button", "button button-danger button-small", "删除记忆"); remove.type = "button"; remove.addEventListener("click", () => deleteMemory(record, remove)); card.append(remove);
      return card;
    }));
  }

  async function handleMemoryPropose(event) {
    event.preventDefault();
    if (!state.memoryAvailable) return;
    const contentInput = byId("memory-content");
    const memoryType = byId("memory-type").value;
    const content = contentInput.value;
    if (!isApprovedMemoryTemplate(memoryType, content)) {
      showToast("请选择服务端批准的记忆模板");
      return;
    }
    const sensitive = byId("memory-sensitive").checked;
    const button = byId("memory-propose-submit");
    const status = byId("memory-write-status");
    button.disabled = true;
    status.hidden = true;
    if (sensitive) {
      renderMemoryTemplateOptions();
      byId("memory-sensitive").checked = false;
      status.hidden = false;
      status.textContent = "已按你的标记在浏览器端拦截；敏感原文未发送，也未写入。";
      button.disabled = false;
      return;
    }
    try {
      const response = await api("/memory/propose", {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          styling_session_id: state.stylingSessionId || undefined,
          namespace: byId("memory-namespace").value,
          type: memoryType,
          content
        })
      });
      const proposal = normalizeMemoryProposal(response.proposal || response);
      if (!proposal || response.record) throw new Error("提议阶段不得直接提交长期记忆");
      renderMemoryTemplateOptions();
      byId("memory-sensitive").checked = false;
      status.hidden = false;
      if (proposal.sensitivity === "sensitive" || proposal.commit_blocked) {
        state.memoryProposals = [...state.memoryProposals.filter((item) => item.proposal_id !== proposal.proposal_id), proposal];
        status.textContent = "敏感内容未写入；待处理列表仅保留脱敏状态，不回显原文。";
      } else {
        state.memoryProposals = [...state.memoryProposals.filter((item) => item.proposal_id !== proposal.proposal_id), proposal];
        status.textContent = "仅创建了待确认提议，尚未写入长期记忆。";
      }
      renderMemory();
      await fetchTrace(response.trace_id || proposal.trace_id);
    } catch (error) {
      renderMemoryTemplateOptions();
      status.hidden = false;
      status.textContent = sensitive ? "敏感内容未写入；原文已从当前表单清除。" : `未写入：${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function decideMemory(proposal, decision, editedContent, button) {
    if (!state.memoryAvailable || (proposal.sensitivity === "sensitive" && decision !== "reject")) return;
    if (decision === "edit" && !isApprovedMemoryTemplate(proposal.type, editedContent)) {
      showToast("编辑内容必须来自同类型受控模板");
      return;
    }
    if (decision === "confirm" && !isApprovedMemoryTemplate(proposal.type, proposal.content)) {
      showToast("当前提议未通过受控模板复核");
      return;
    }
    button.disabled = true;
    try {
      const response = await api(`/memory/${encodeURIComponent(proposal.proposal_id)}/confirm`, {
        method: "POST",
        body: JSON.stringify({
          user_id: userId,
          decision,
          edited_content: decision === "edit" ? editedContent : undefined
        })
      });
      const returnedProposal = normalizeMemoryProposal(response.proposal);
      if (!returnedProposal) throw new Error("记忆决定响应未通过用户与命名空间复核");
      const sensitiveBlocked = returnedProposal.status === "rejected"
        && returnedProposal.commit_blocked === true
        && returnedProposal.sensitivity === "sensitive"
        && response.record == null;
      if (decision !== "reject" && sensitiveBlocked) {
        state.memoryProposals = state.memoryProposals.filter((item) => item.proposal_id !== proposal.proposal_id);
        renderMemory();
        const status = byId("memory-write-status");
        status.hidden = false;
        status.textContent = "编辑后的内容触发敏感门控，已安全拒绝且未写入；原文不再显示。";
        showToast("敏感记忆已拦截，未写入");
        await fetchTrace(response.trace_id || returnedProposal.trace_id);
        return;
      }
      let record = null;
      if (decision !== "reject") {
        record = normalizeMemoryRecord(response.record);
        if (returnedProposal.status !== "committed" || !record) throw new Error("确认后未返回有效 committed 记录");
      }
      state.memoryProposals = state.memoryProposals.filter((item) => item.proposal_id !== proposal.proposal_id);
      if (record) state.memoryRecords = [...state.memoryRecords.filter((item) => item.memory_id !== record.memory_id), record];
      renderMemory();
      await fetchTrace(response.trace_id || response.proposal?.trace_id);
    } catch (error) {
      showToast(`记忆决定未完成：${error.message}`);
      button.disabled = false;
    }
  }

  async function deleteMemory(record, button) {
    button.disabled = true;
    try {
      await api(`/memory/${encodeURIComponent(record.memory_id)}?user_id=${encodeURIComponent(userId)}`, { method: "DELETE" });
      state.memoryRecords = state.memoryRecords.filter((item) => item.memory_id !== record.memory_id);
      renderMemory();
    } catch (error) {
      showToast(`记忆删除未完成：${error.message}`);
      button.disabled = false;
    }
  }

  function sanitizeDebugValue(value, key = "") {
    if (/(secret|token|api.?key|base64|raw|query_text|conversation|content|file|image)/i.test(key)) return "[已清洗]";
    if (Array.isArray(value)) return value.map((item) => sanitizeDebugValue(item));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([childKey, childValue]) => [childKey, sanitizeDebugValue(childValue, childKey)]));
    }
    return value;
  }

  async function fetchTrace(traceId, isStillCurrent = () => true, signal = null) {
    if (!isStillCurrent()) return;
    if (state.source !== "api" || !traceId) {
      state.trace = {
        mode: "trace_unavailable",
        trace_id: traceId || null,
        reason: state.source !== "api" ? "同源 API 未连接，未生成服务端 Trace。" : "服务端未提供 trace_id。"
      };
      if (!isStillCurrent()) return;
      state.trace = sanitizeDebugValue(state.trace);
      renderDebug(); return;
    }
    try {
      const query = new URLSearchParams({ user_id: userId });
      if (state.stylingSessionId) query.set("styling_session_id", state.stylingSessionId);
      const traceResponse = await api(`/trace/${encodeURIComponent(traceId)}?${query.toString()}`, { signal });
      if (!isStillCurrent()) return;
      state.trace = sanitizeDebugValue(traceResponse.trace);
      state.traceHistory = [...state.traceHistory.filter((item) => item.trace_id !== state.trace?.trace_id), state.trace].slice(-12);
    } catch (error) {
      if (!isStillCurrent()) return;
      state.trace = { trace_id: traceId, error: error.message };
    }
    if (!isStillCurrent()) return;
    renderDebug();
  }

  function renderDebug() {
    const trace = state.trace || {};
    const catalog = trace.catalog || {};
    const dialogueRequestFailed = trace.mode === "dialogue_retry_required";
    const provider = dialogueRequestFailed
      ? {}
      : state.dialogueProvider || trace.provider || (state.source === "api" ? state.health?.providers?.llm : null) || {};
    const replySource = dialogueRequestFailed
      ? "本轮回应未完成"
      : provider.generation_source ? dialogueReplyLabel(provider, state.conversationMode) : "尚无已验证对话回合";
    const metrics = [
      ["数据源", state.source],
      ["Urgency", state.scene?.urgency || "—"],
      ["Catalog 调用", String(catalog.call_count ?? "未报告")],
      ["回复来源", replySource],
      ["请求模型", provider.requested_model || "未报告"],
      ["实际模型", provider.resolved_model || "未验证"],
      ["对话历史", `${state.turnIndex} / v${state.historyVersion}`],
      ["UI Build", UI_BUILD_VERSION]
    ];
    byId("debug-metrics").replaceChildren(...metrics.map(([label, value]) => {
      const card = make("div", "debug-metric"); card.append(make("span", "", label), make("strong", "", value)); return card;
    }));
    const reasons = byId("filter-reasons");
    const filters = trace.filters || [];
    reasons.replaceChildren(...(filters.length ? filters.map((entry) => {
      const reason = entry.reason || entry.code || entry.filter || "hard_filter";
      const id = entry.garment_id || entry.item_id || "";
      return make("li", "", id ? `${id} · ${reason}` : String(reason));
    }) : [make("li", "", Array.isArray(trace.filters) ? "本次没有额外过滤记录。" : "未取得服务端过滤记录。")]));
    const related = byId("debug-related");
    const versionLines = state.lookVersions.map((look) => `${versionLabel(look)} · ${look.status || "历史"}${state.debug ? ` · ${lookVersionId(look)}` : ""}`);
    const decisionLines = state.debug ? state.lookDecisionLedger.map((entry) => {
      const source = state.lookVersions.find((look) => lookVersionId(look) === entry.sourceVersionId);
      return `Decision · ${versionLabel(source)} · ${entry.decision} · canonical=${entry.canonicalKey}`;
    }) : [];
    const traceLines = state.traceHistory.map((item) => `Trace · ${item.trace_id || "未提供"}`);
    related.replaceChildren(...((versionLines.length || decisionLines.length || traceLines.length) ? [...versionLines, ...decisionLines, ...traceLines].map((line) => make("li", "", line)) : [make("li", "", "建立 Look 后显示。") ]));
    byId("trace-json").textContent = JSON.stringify(trace, null, 2);
  }

  function showOfflineBrowseOnly(detail) {
    clearRecommendationPresentation();
    byId("scene-input").required = false;
    state.trace = {
      mode: "offline_browse_only",
      recommendation_generated: false,
      reason: detail,
      catalog: { attempted: false, call_count: 0, blocked_reason: "recommendation_api_unavailable" }
    };
    const summary = byId("scene-summary");
    summary.replaceChildren(...[
      ["状态", "离线只读"], ["推荐", "已安全停止"], ["方向", "0"], ["使用范围", "仅浏览 Team / 衣橱"]
    ].map(([term, value]) => {
      const row = make("div"); row.append(make("dt", "", term), make("dd", "", value)); return row;
    }));
    byId("scene-source").textContent = "仅浏览";
    byId("constraint-chips").replaceChildren(make("span", "chip", "未执行离线硬约束推断"));
    byId("local-guard").hidden = false;
    byId("session-state-label").textContent = "离线仅浏览 · 未生成推荐";
    byId("conversation-origin").textContent = "对话服务未连接";
    byId("current-task-title").textContent = "推荐已安全停止";
    byId("current-task-detail").textContent = "Team 与衣橱仍可浏览；恢复 API 后再生成方向。";
    byId("task-progress").style.width = "16%";
    renderRecommendations();
    renderDebug();
    appendMessage("系统", "Stylist 暂时无法回应；衣橱演示仍可浏览，恢复服务后可安全重试。");
  }

  function recommendationFailureCopy(error) {
    const timedOut = error?.code === "REQUEST_TIMEOUT";
    const aborted = error?.code === "REQUEST_ABORTED";
    const networkUnavailable = error?.code === "NETWORK_UNAVAILABLE";
    if (timedOut) return "Stylist 这次回应时间较长；会话仍保留，可以安全重试。";
    if (aborted) return "本次请求已中止；未采用任何未验证结果，可安全重试。";
    if (networkUnavailable) return "本次请求暂时未连通；保留上次成功连接状态，未生成方向，可安全重试。";
    return "Stylist 这次没有完成回应；没有显示未验证内容，可以安全重试。";
  }

  function clearDialogueWaitTimer() {
    if (dialogueWaitTimer !== null) window.clearInterval(dialogueWaitTimer);
    dialogueWaitTimer = null;
    dialogueWaitStartedAt = 0;
  }

  function renderDialogueWaitElapsed() {
    if (!dialogueWaitStartedAt) return;
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - dialogueWaitStartedAt) / 1000));
    const waitLabel = `CPA 正在生成 · 已等待 ${elapsedSeconds}s`;
    byId("session-state-label").textContent = waitLabel;
    byId("conversation-origin").textContent = waitLabel;
  }

  function startDialogueWaitTimer() {
    clearDialogueWaitTimer();
    dialogueWaitStartedAt = Date.now();
    renderDialogueWaitElapsed();
    dialogueWaitTimer = window.setInterval(renderDialogueWaitElapsed, 1000);
  }

  function releaseDialogueRequestUi(requestToken, sceneInput, submitLabel) {
    if (!dialogueGuard.finish(requestToken)) return false;
    clearDialogueWaitTimer();
    const closed = state.conversationMode === "task_closed";
    setDialogueInFlightControls(false);
    sceneInput.required = !closed && !state.pendingDialogueRetry;
    if (submitLabel) submitLabel.textContent = state.pendingDialogueRetry ? "安全重试" : "发送给 Stylist";
    return true;
  }

  function loadDialogueTraceBestEffort(response, requestToken) {
    dialogueTraceGuard.cancel();
    const traceController = new AbortController();
    const traceToken = dialogueTraceGuard.begin(response.trace_id, traceController);
    if (!traceToken) return;
    const isStillCurrent = () => dialogueTraceGuard.isCurrent(traceToken)
      && dialogueGuard.isGenerationCurrent(requestToken)
      && state.stylingSessionId === response.styling_session_id
      && state.turnIndex === response.turn_index;
    void fetchTrace(response.trace_id, isStillCurrent, traceController.signal)
      .catch(() => {})
      .finally(() => { dialogueTraceGuard.finish(traceToken); });
  }

  function createDialogueRequestId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `web-turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function validateCpaProvider(raw, requireGenerationSource = false) {
    if (!raw || typeof raw !== "object" || (raw.status !== "ok" && raw.status !== "fallback")) throw new Error("响应缺少可信 CPA 状态");
    if (typeof raw.attempted !== "boolean") throw new Error("CPA 尝试状态不完整");
    if (raw.requested_model !== "grok4.6" || raw.transport_model !== "grok-4.6-high") throw new Error("CPA 模型合同不匹配");
    if (raw.status === "ok" && (raw.attempted !== true || raw.model_verified !== true || raw.degraded !== false || !dialogueRuntime.hasExactCpaModel(raw))) {
      throw new Error("CPA 成功状态未经精确模型验证");
    }
    if (raw.status === "fallback" && raw.degraded !== true) throw new Error("CPA 降级状态不完整");
    if (requireGenerationSource && raw.generation_source !== "cpa" && raw.generation_source !== "local_fallback") throw new Error("CPA 生成来源不完整");
    if (raw.status === "ok" && raw.generation_source && raw.generation_source !== "cpa") throw new Error("CPA 成功状态来源不匹配");
    if (raw.status === "fallback" && raw.generation_source && raw.generation_source !== "local_fallback") throw new Error("CPA 降级状态来源不匹配");
    return {
      status: raw.status,
      attempted: raw.attempted,
      requested_model: raw.requested_model,
      transport_model: raw.transport_model,
      resolved_model: typeof raw.resolved_model === "string" ? raw.resolved_model : null,
      model_verified: raw.model_verified === true,
      degraded: raw.degraded === true,
      generation_source: typeof raw.generation_source === "string" ? raw.generation_source : null,
      reason_code: typeof raw.reason_code === "string" ? raw.reason_code : null
    };
  }

  function hasEnabledToolPayload(value) {
    if (value === null || value === undefined || value === false || value === 0 || value === "") return false;
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === "object") return Object.keys(value).length > 0;
    return true;
  }

  function validateDialogueScene(raw, stylingSessionId, requestId) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("统一对话端点缺少权威 scene");
    if (raw.user_id !== userId) throw new Error("权威 scene owner 不匹配");
    if (raw.styling_session_id !== stylingSessionId) throw new Error("权威 scene session 不匹配");
    if (!sceneHorizons.has(raw.event_horizon)) throw new Error("权威 scene 包含未知 event_horizon");
    if (!sceneUrgencies.has(raw.urgency)) throw new Error("权威 scene 包含未知 urgency");
    if (!sceneOccasions.has(raw.occasion)) throw new Error("权威 scene 包含未知 occasion");
    if (!raw.constraints || typeof raw.constraints !== "object" || Array.isArray(raw.constraints)) throw new Error("权威 scene 缺少 constraints");
    if (!raw.ui_capabilities || typeof raw.ui_capabilities !== "object" || Array.isArray(raw.ui_capabilities)) throw new Error("权威 scene 缺少 ui_capabilities");
    return hardenScene({ ...raw, request_id: raw.request_id || requestId, styling_session_id: stylingSessionId });
  }

  function validateDialogueTurn(raw, expected) {
    if (!raw || typeof raw !== "object") throw new Error("统一对话端点未返回对象");
    if (!dialogueModes.has(raw.conversation_mode)) throw new Error("统一对话端点返回了未知 conversation_mode");
    if (!dialogueActions.has(raw.action)) throw new Error("统一对话端点返回了未知 action");
    if (!pendingQuestionStates.has(raw.pending_question_status)) throw new Error("统一对话端点返回了未知 pending_question_status");
    if (typeof raw.recommendation_paused !== "boolean") throw new Error("统一对话端点未返回 recommendation_paused");
    if (!raw.turn_id || !raw.request_id || !raw.styling_session_id || !raw.trace_id) throw new Error("统一对话端点缺少回合标识");
    if (!Number.isInteger(raw.turn_index) || raw.turn_index < 1 || !Number.isInteger(raw.history_version) || raw.history_version < raw.turn_index) {
      throw new Error("统一对话端点返回了非法历史版本");
    }
    if (raw.request_id !== expected.requestId) throw new Error("统一对话端点返回了不匹配的 request_id");
    if (expected.stylingSessionId && raw.styling_session_id !== expected.stylingSessionId) throw new Error("统一对话端点返回了不匹配的 styling_session_id");
    const expectedTurnIndex = expected.stylingSessionId ? state.turnIndex + 1 : 1;
    if (raw.turn_index !== expectedTurnIndex) throw new Error("统一对话端点返回了乱序回合");
    if (typeof raw.assistant_message !== "string" || !raw.assistant_message.trim()) throw new Error("统一对话端点未返回角色回复");
    if (raw.action === "clarify" && (raw.assistant_message.match(/[?？]/g) || []).length > 1) throw new Error("clarify action 每轮最多显示一个问题");
    const combinationRule = dialogueCombinationRules[raw.conversation_mode]?.[raw.action];
    if (!combinationRule) throw new Error("统一对话端点返回了非法 mode/action 组合");
    if (raw.recommendation_paused !== combinationRule.paused) throw new Error("统一对话端点返回了非法 recommendation_paused 状态");
    if (!combinationRule.pending.includes(raw.pending_question_status)) throw new Error("统一对话端点返回了非法 pending_question_status 组合");
    if (forbiddenTopLevelToolFields.some((field) => hasEnabledToolPayload(raw[field]))) throw new Error("统一对话端点返回了未授权工具字段");
    const provider = validateCpaProvider(raw.provider, true);
    const scene = raw.scene === null ? null : validateDialogueScene(raw.scene, raw.styling_session_id, raw.request_id);
    if (raw.conversation_mode === "stylist_chat" && scene !== null) throw new Error("自然聊天不得创建场景");
    if (raw.conversation_mode === "styling_active" && scene === null) throw new Error("穿搭回合缺少权威场景");

    const recommendationIsNonempty = raw.recommendation
      && typeof raw.recommendation === "object"
      && Object.keys(raw.recommendation).length > 0;
    if (raw.action !== "recommend" && raw.recommendation !== null) throw new Error("非推荐 action 的 recommendation 必须为 null");
    if (raw.action !== "recommend" && scene && (scene.shopping_allowed !== false || scene.ui_capabilities.shopping_cta !== false)) {
      throw new Error("非推荐 action 不得启用购物能力");
    }
    if (raw.action === "recommend") {
      if (!recommendationIsNonempty) throw new Error("recommend action 缺少推荐");
      if (raw.recommendation.request_id !== raw.request_id) throw new Error("recommendation request_id 与权威回合不匹配");
    }

    const suggestedReplies = Array.isArray(raw.suggested_replies)
      ? [...new Set(raw.suggested_replies.map((reply) => suggestedReplyAliases[reply]).filter((reply) => controlledSuggestedReplies.has(reply)))]
      : [];
    const assistantMessage = dialogueRuntime.selectAssistantText(
      raw.assistant_message,
      provider,
      (message) => safeStylingText(message, "我会按安全边界继续陪你处理这次任务。")
    );
    return { ...raw, scene, provider, suggested_replies: suggestedReplies, assistant_message: assistantMessage };
  }

  function applyDialogueTurn(response) {
    clearDialogueWaitTimer();
    state.stylingSessionId = response.styling_session_id;
    state.pendingQuestionStatus = response.pending_question_status;
    state.suggestedReplies = response.suggested_replies;
    state.turnIndex = response.turn_index;
    state.historyVersion = response.history_version;
    state.dialogueProvider = response.provider;
    setDialogueMode(response.conversation_mode);
    appendMessage("Stylist", response.assistant_message, false, {
      provider: response.provider,
      conversationMode: response.conversation_mode,
      turnIndex: response.turn_index
    });

    if (response.scene) state.scene = response.scene;
    if (response.action === "recommend") {
      recommendationPreviewGuard.cancel();
      state.recommendationPreviews = new Map();
      state.activeOutfitId = null;
      state.recommendation = sanitizeRecommendation(state.scene, response.recommendation);
      renderScene();
      renderRecommendations();
      window.setTimeout(() => { void generateRecommendationPreviews(response); }, 0);
    } else {
      if (response.scene) renderScene();
      else {
        byId("scene-panel").hidden = true;
        byId("scene-panel").closest(".studio-layout").classList.add("is-chat-only");
      }
      clearRecommendationPresentation();
    }

    renderSuggestedReplies();
    byId("conversation-origin").textContent = `本轮：${dialogueReplyLabel(response.provider, response.conversation_mode)}`;
    if (response.conversation_mode === "stylist_chat") {
      byId("current-task-title").textContent = "正在和 Stylist 聊天";
      byId("current-task-detail").textContent = `同一会话已连续 ${response.turn_index} 轮；准备搭配时直接告诉我。`;
      byId("session-ownership").textContent = `Stylist 会话 · 历史版本 ${response.history_version}`;
      byId("task-progress").style.width = "18%";
    } else if (response.conversation_mode === "support_pause") {
      byId("current-task-detail").textContent = "已暂停穿搭建议；同一会话与已确认场景继续保留。";
    } else if (response.conversation_mode === "task_closed") {
      byId("current-task-detail").textContent = "本次任务已结束；如需继续，请明确开始新任务。";
    }
  }

  function showDialogueFailure(error, retryBinding) {
    clearDialogueWaitTimer();
    const detail = recommendationFailureCopy(error);
    state.pendingDialogueRetry = { ...retryBinding };
    byId("scene-input").required = false;
    clearRecommendationPresentation();
    state.trace = {
      mode: "dialogue_retry_required",
      recommendation_generated: false,
      error_code: ["REQUEST_TIMEOUT", "REQUEST_ABORTED", "NETWORK_UNAVAILABLE"].includes(error?.code)
        ? error.code
        : "REQUEST_FAILED",
      catalog: { attempted: false, call_count: 0, blocked_reason: "dialogue_turn_not_verified" }
    };
    byId("session-state-label").textContent = "本次回应未完成 · 可安全重试";
    byId("conversation-origin").textContent = "本轮回应未完成";
    byId("current-task-detail").textContent = detail;
    renderDebug();
    appendMessage("系统", detail);
  }

  async function handleSceneSubmit(event) {
    event.preventDefault();
    if (dialogueGuard.inFlight) return;
    dialogueTraceGuard.cancel();
    const sceneInput = byId("scene-input");
    const typedQuery = sceneInput.value.trim();
    const retry = state.pendingDialogueRetry;
    const message = typedQuery || retry?.message || "";
    if (!message) return;
    const submit = byId("scene-submit");
    const submitLabel = submit.querySelector("span");
    const isSafeRetry = !typedQuery
      && Boolean(retry?.message && retry?.requestId)
      && retry.message === message
      && retry.stylingSessionId === state.stylingSessionId;
    const stylingSessionId = isSafeRetry ? retry.stylingSessionId : state.stylingSessionId;
    const requestId = isSafeRetry ? retry.requestId : createDialogueRequestId();
    const retryBinding = { message, requestId, stylingSessionId };
    const requestController = new AbortController();
    const requestToken = dialogueGuard.begin(requestId, requestController);
    if (!requestToken) return;
    let requestReleased = false;
    setDialogueInFlightControls(true);
    if (submitLabel) submitLabel.textContent = "正在等待 CPA";
    if (!isSafeRetry) {
      appendMessage("你", message, true);
      sceneInput.value = "";
    }
    startDialogueWaitTimer();
    try {
      if (state.source !== "api") {
        state.pendingDialogueRetry = retryBinding;
        showOfflineBrowseOnly("当前同源 API 不可用；fixture 只用于浏览，不参与推荐。");
        return;
      }
      state.pendingDialogueRetry = null;
      const payload = { user_id: userId, message, request_id: requestId };
      if (stylingSessionId) payload.styling_session_id = stylingSessionId;
      const rawResponse = await api("/dialogue/turn", {
        method: "POST",
        body: JSON.stringify(payload),
        timeoutMs: DIALOGUE_TURN_TIMEOUT_MS,
        signal: requestController.signal
      });
      if (!dialogueGuard.isCurrent(requestToken)) return;
      const response = validateDialogueTurn(rawResponse, retryBinding);
      if (!dialogueGuard.isCurrent(requestToken)) return;
      applyDialogueTurn(response);
      requestReleased = releaseDialogueRequestUi(requestToken, sceneInput, submitLabel);
      if (!requestReleased) return;
      loadDialogueTraceBestEffort(response, requestToken);
    } catch (error) {
      if (!dialogueGuard.isCurrent(requestToken)) return;
      showDialogueFailure(error, retryBinding);
      showToast("本次回应未完成，可安全重试");
      requestReleased = releaseDialogueRequestUi(requestToken, sceneInput, submitLabel);
    } finally {
      if (!requestReleased && dialogueGuard.isCurrent(requestToken)) {
        requestReleased = releaseDialogueRequestUi(requestToken, sceneInput, submitLabel);
      }
    }
  }

  function startNewTask() {
    if (!window.confirm("开始新任务会结束当前上下文并清空本页会话状态，确定继续吗？")) return;
    resetSessionScopedState(null);
    state.scene = null;
    state.trace = null;
    byId("conversation").replaceChildren();
    appendMessage("Stylist", "新任务已开始。告诉我场合、时间和想呈现的感觉；是否开始搭配由你决定。");
    byId("scene-summary").replaceChildren();
    byId("scene-panel").hidden = true;
    byId("scene-panel").closest(".studio-layout").classList.add("is-chat-only");
    byId("constraint-chips").replaceChildren(make("span", "chip", "等待新任务输入"));
    byId("local-guard").hidden = true;
    byId("session-state-label").textContent = "新任务 · 等待输入";
    byId("current-task-title").textContent = "新任务待描述";
      byId("current-task-detail").textContent = "尚未开始连续会话。";
    byId("session-ownership").textContent = "尚未建立 · personal_team:stylist";
    byId("task-progress").style.width = "8%";
    byId("scene-input").disabled = false;
    byId("scene-input").required = true;
    byId("scene-input").value = "";
    byId("scene-submit").disabled = false;
    byId("scene-submit").querySelector("span").textContent = "发送给 Stylist";
    renderSuggestedReplies();
    renderRecommendations();
    renderPreview2d();
    renderDebug();
    byId("scene-input").focus();
  }

  function bindEvents() {
    byId("scene-form").addEventListener("submit", handleSceneSubmit);
    byId("new-task").addEventListener("click", startNewTask);
    byId("preview2d-generate").addEventListener("click", generateStatic2dPreview);
    byId("preview2d-delete").addEventListener("click", deleteStatic2dPreview);
    byId("asset-form").addEventListener("submit", handleAssetSubmit);
    byId("asset-file").addEventListener("change", () => {
      const file = byId("asset-file").files?.[0];
      if (file && ["image/png", "image/jpeg", "image/webp"].includes(file.type) && file.size <= 5 * 1024 * 1024) previewAssetFile(file);
    });
    byId("score-request").addEventListener("click", requestScorecard);
    byId("user-modify-form").addEventListener("submit", handleUserModify);
    byId("finalize-form").addEventListener("submit", handleFinalize);
    byId("memory-propose-form").addEventListener("submit", handleMemoryPropose);
    byId("memory-type").addEventListener("change", () => renderMemoryTemplateOptions());
    byId("compare-left").addEventListener("change", renderVersionComparison);
    byId("compare-right").addEventListener("change", renderVersionComparison);
    byId("scene-input").addEventListener("keydown", (event) => {
      if (!dialogueRuntime.shouldSubmitComposerKey(event)) return;
      event.preventDefault();
      if (!dialogueGuard.inFlight) byId("scene-form").requestSubmit();
    });
    document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => {
      if (dialogueGuard.inFlight) return;
      byId("scene-input").value = button.dataset.example || ""; byId("scene-input").focus();
    }));
    ["filter-slot", "filter-status", "filter-season"].forEach((id) => byId(id).addEventListener("change", renderWardrobe));
    byId("reset-filters").addEventListener("click", () => {
      byId("filter-slot").value = "all"; byId("filter-status").value = "all"; byId("filter-season").value = "all"; renderWardrobe();
    });
    byId("retry-api").addEventListener("click", connect);
    byId("debug-toggle").addEventListener("click", () => {
      state.debug = !state.debug;
      byId("debug-panel").hidden = !state.debug;
      byId("debug-toggle").setAttribute("aria-expanded", String(state.debug));
      renderWardrobe(); renderRecommendations(); renderActiveLook(); renderCocreation(); renderMemory(); renderDebug();
      if (state.debug) byId("debug-panel").scrollIntoView({ block: "start", behavior: "smooth" });
    });
    window.addEventListener("beforeunload", () => {
      dialogueTraceGuard.cancel();
      dialogueGuard.cancel();
      clearDialogueWaitTimer();
      recommendationPreviewGuard.cancel();
      if (state.assetPreviewUrl) URL.revokeObjectURL(state.assetPreviewUrl);
    });
  }

  bindEvents();
  connect();
}());
