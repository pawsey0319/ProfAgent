(function exposeFixtures() {
  "use strict";

  const garmentRows = [
    ["g001", "短款风衣", "outer", "navy", ["spring", "autumn"], "available", ["commute", "meeting"]],
    ["g002", "轻薄夹克", "outer", "white", ["spring", "summer"], "available", ["daily", "travel"]],
    ["g003", "针织外套", "outer", "beige", ["autumn", "winter"], "available", ["daily", "commute"]],
    ["g004", "简洁大衣", "outer", "black", ["winter"], "available", ["meeting", "interview"]],
    ["g005", "基础衬衫", "top", "gray", ["all"], "available", ["daily", "commute", "interview", "meeting"]],
    ["g006", "圆领针织衫", "top", "brown", ["autumn", "winter"], "available", ["daily", "commute"]],
    ["g007", "纯色短袖", "top", "khaki", ["spring", "summer"], "available", ["daily", "home"]],
    ["g008", "利落长袖", "top", "blue", ["spring", "autumn"], "available", ["commute", "meeting"]],
    ["g009", "简约上衣", "top", "green", ["all"], "available", ["daily", "date"]],
    ["g010", "轻便卫衣", "top", "red", ["autumn", "winter"], "available", ["daily", "sports"]],
    ["g011", "直筒西裤", "bottom", "pink", ["all"], "available", ["commute", "interview", "meeting"]],
    ["g012", "宽松牛仔裤", "bottom", "navy", ["all"], "available", ["daily", "travel"]],
    ["g013", "垂感长裤", "bottom", "white", ["spring", "summer"], "available", ["commute", "meeting"]],
    ["g014", "休闲半裙", "bottom", "beige", ["spring", "summer"], "available", ["daily", "date"]],
    ["g015", "保暖长裤", "bottom", "black", ["autumn", "winter"], "available", ["daily", "outdoor"]],
    ["g016", "通勤衬衫裙", "dress", "gray", ["all"], "available", ["daily", "commute", "meeting"]],
    ["g017", "简洁连衣裙", "dress", "brown", ["spring", "summer"], "available", ["date", "party"]],
    ["g018", "针织长裙", "dress", "khaki", ["autumn", "winter"], "available", ["meeting", "date"]],
    ["g019", "轻便运动鞋", "shoes", "blue", ["all"], "available", ["daily", "sports"]],
    ["g020", "简洁乐福鞋", "shoes", "green", ["all"], "available", ["commute", "interview", "meeting"]],
    ["g021", "低跟单鞋", "shoes", "red", ["spring", "summer"], "available", ["meeting", "date"]],
    ["g022", "保暖短靴", "shoes", "pink", ["autumn", "winter"], "available", ["daily", "travel"]],
    ["g023", "通勤托特包", "bag", "navy", ["all"], "available", ["commute", "meeting"]],
    ["g024", "轻便斜挎包", "bag", "white", ["all"], "laundry", ["daily", "travel"]],
    ["g025", "小号手提包", "bag", "beige", ["all"], "reserved", ["date", "party"]],
    ["g026", "素色围巾", "accessory", "black", ["autumn", "winter"], "available", ["daily", "travel"]],
    ["g027", "简约腰带", "accessory", "purple", ["all"], "available", ["commute", "meeting"]],
    ["g028", "轻便帽子", "accessory", "brown", ["spring", "summer"], "unavailable", ["outdoor", "travel"]]
  ];

  const garments = garmentRows.map(([garment_id, name, slot, color, seasons, status, occasions]) => ({
    api_version: "r1_demo_v1",
    data_version: "fixtures_v1.0",
    source_id: "fixtures",
    synthetic: true,
    user_id: "u01",
    garment_id,
    name,
    slot,
    color,
    seasons,
    status,
    occasions
  }));

  window.PROFAGENT_FIXTURES = {
    health: {
      api_version: "r1_demo_v1",
      status: "fixture",
      ready: true,
      data: {
        version: "fixtures_v1.0",
        counts: { users: 3, garments: 50, outfits: 20, catalog: 50, eval: 30 }
      },
      providers: {
        llm: { status: "fixture", requested_model: "grok4.6" },
        dense: { status: "fixture" },
        catalog: { status: "blocked_in_high_urgency" }
      },
      capabilities: { rule_fallback: true, static_2d: false }
    },
    teamHome: {
      api_version: "r1_demo_v1",
      team: {
        team_id: "personal_team",
        name: "你的私人团队",
        description: "围绕你的具体任务协作；R1 当前只上线一位专业成员。"
      },
      members: [{
        member_id: "stylist",
        persona_id: "stylist",
        name: "你的私人明星穿搭师",
        status: "available",
        boundary: "负责穿搭场景理解、衣橱组合和可解释建议；不是销售、治疗师或真人造型师。",
        capabilities: ["场景理解", "衣橱检索", "穿搭方向", "约束复核"]
      }],
      active_sessions: [],
      unavailable_capabilities: [
        { name: "第二位专业成员", status: "not_launched" },
        { name: "真人专家", status: "not_launched" },
        { name: "静态 2D 预览", status: "not_launched" }
      ]
    },
    wardrobe: {
      api_version: "r1_demo_v1",
      user_id: "u01",
      count: garments.length,
      items: garments,
      available_filters: ["slot", "status", "season", "color", "occasion"]
    }
  };
})();
