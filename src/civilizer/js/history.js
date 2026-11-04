// Civilizer history capture: evaluated inside the Civ VII UI like snapshot.js, but
// collects what a whole-game timeline needs: victory history for the current and
// past ages, the end-game summary datasets, and per-player state that no log
// records (researched techs/civics, government, religion, settlements, wars).
//
// Omniscient by design (post-game review): covers every major player, met or not.
//
// SAFETY: same rule as snapshot.js. Every call copies a call site in Firaxis's
// shipped UI JS (path noted in comments) with the same argument shapes.
//
// __OPTIONS__ is replaced by the Python side with a JSON object.
(() => {
  const OPT = __OPTIONS__;
  const errors = [];
  const sec = (name, fn) => {
    try {
      return fn();
    } catch (e) {
      errors.push(`${name}: ${e && e.message ? e.message : e}`);
      return null;
    }
  };
  const loc = (k) => {
    try {
      return k ? Locale.compose(k) : k;
    } catch (e) {
      return k;
    }
  };
  const ageType = GameInfo.Ages.lookup(Game.age)?.AgeType;
  const majors = Players.getAlive().filter((p) => p.isMajor);

  const out = { schema: 1, kind: "history", captured_at: new Date().toISOString(), options: OPT };

  out.game = sec("game", () => ({
    turn: Game.turn,
    seed: Configuration.getGame().gameSeed,
    difficulty: Configuration.getGame().difficultyName,
    age: ageType,
    local_player_id: GameContext.localPlayerID,
    // victories-screen-model.js: GameInfo.Ages.forEach, ChronologyIndex
    ages: Array.from({ length: GameInfo.Ages.length }, (_, i) => GameInfo.Ages[i]).map((a) => ({
      type: a.AgeType,
      name: loc(a.Name),
      index: a.ChronologyIndex,
    })),
  }));

  // Researched nodes of this age's trees. advice-support.js: player.Techs.isNodeUnlocked(name),
  // player.Culture.isNodeUnlocked(name); tree-grid.js: Game.ProgressionTrees.getNode(player, nodeType).
  const NODES = sec("nodes", () => {
    const rows = [];
    GameInfo.ProgressionTreeNodes.forEach((n) => {
      const tree = GameInfo.ProgressionTrees.lookup(n.ProgressionTree);
      if (!tree || tree.AgeType != ageType) return;
      rows.push({ node: n.ProgressionTreeNodeType, hash: n.$hash, name: loc(n.Name), tree: tree.ProgressionTreeType, system: tree.SystemType });
    });
    return rows;
  }) || [];
  const researched = (p, system) => {
    const api = system == "SYSTEM_TECH" ? p.Techs : p.Culture;
    if (!api) return [];
    const res = [];
    for (const n of NODES) {
      if (n.system != system || !api.isNodeUnlocked(n.node)) continue;
      const nd = Game.ProgressionTrees.getNode(p.id, n.hash);
      res.push({ node: n.node, name: n.name, tree: n.tree, depth: nd ? nd.depthUnlocked : null });
    }
    return res;
  };

  out.players = majors.map((p) => {
    const o = { id: p.id };
    sec("p.basic", () =>
      Object.assign(o, {
        leader: loc(GameInfo.Leaders.lookup(p.leaderType)?.Name),
        leader_type: GameInfo.Leaders.lookup(p.leaderType)?.LeaderType,
        civilization: loc(p.civilizationFullName),
        is_human: p.isHuman,
      })
    );
    // policies/model-policies.js; snapshot.js me.religion / me.ideology
    o.government = sec("p.gov", () => GameInfo.Governments.lookup(p.Culture.getGovernmentType())?.GovernmentType ?? null);
    o.religion = sec("p.religion", () => ({ name: loc(p.Religion.getReligionName()), holy_city: loc(p.Religion.getHolyCityName()) }));
    o.ideology = sec("p.ideology", () => GameInfo.Ideologies.lookup(p.Culture.getChosenIdeology())?.IdeologyType ?? null);
    o.techs = sec("p.techs", () => researched(p, "SYSTEM_TECH"));
    o.civics = sec("p.civics", () => researched(p, "SYSTEM_CULTURE"));
    // tutorial-items-antiquity.js: player.Diplomacy.isAtWarWith(otherPlayerId)
    o.at_war_with = sec("p.war", () => Players.getAlive().filter((q) => q.id != p.id && p.Diplomacy.isAtWarWith(q.id)).map((q) => q.id));
    // city-banners / model-city-details: city fields; originalOwner (20 call sites)
    o.settlements = sec("p.cities", () =>
      p.Cities.getCities().map((c) => ({
        id: c.id.id,
        name: loc(c.name),
        type: c.isTown ? "town" : "city",
        capital: c.isCapital,
        population: c.population,
        x: c.location.x,
        y: c.location.y,
        original_owner: c.originalOwner,
      }))
    );
    // victory-manager.js: leader.AdvancedStart?.getHistoricalLegacyPoints()
    o.legacy_points = sec("p.legacy", () => p.AdvancedStart?.getHistoricalLegacyPoints() ?? null);
    return o;
  });

  out.minors = sec("minors", () => Players.getAlive().filter((p) => p.isMinor).map((p) => ({ id: p.id, name: loc(p.name) })));

  // Per-turn victory points. victories-screen-model.js: getHistoryForVictoryType(hash) for the current
  // age, getPastAgeHistoryForVictoryType(hash, age.$hash) for each age before it.
  out.victories = sec("victories", () => {
    const res = {};
    const thisAge = GameInfo.Ages.lookup(Game.age)?.ChronologyIndex;
    const past = [];
    GameInfo.Ages.forEach((age) => {
      if (age.ChronologyIndex <= thisAge && age.$hash != Game.age) past.push(age);
    });
    // Only the countdown victories: the ones snapshot.js already reads live without trouble.
    const types = ["VICTORY_ECONOMIC_MODERN", "VICTORY_SCIENCE_MODERN", "VICTORY_CULTURE_MODERN", "VICTORY_MILITARY_MODERN"];
    types.map((t) => GameInfo.Victories.lookup(t)).filter((v) => v).forEach((v) => {
      const cur = {}, prev = {};
      for (const p of majors) {
        if (!p.Victories) continue;
        cur[p.id] = sec(`v.cur.${v.VictoryType}`, () => p.Victories.getHistoryForVictoryType(v.$hash)) || [];
        for (const age of past) {
          const h = sec(`v.past.${v.VictoryType}`, () => p.Victories.getPastAgeHistoryForVictoryType(v.$hash, age.$hash)) || [];
          (prev[age.AgeType] = prev[age.AgeType] || {})[p.id] = h;
        }
      }
      const any = (o) => Object.values(o).some((h) => h && h.length);
      const pastAny = Object.values(prev).some(any);
      if (any(cur) || pastAny) res[v.VictoryType] = { current: cur, past: prev };
    });
    return res;
  });

  // End-game graphs. fxs-hof-chart.js: Game.Summary.getObjects(), getDataSets(id), getDataPoints();
  // dataset ids from end-result-graphs/panel-end-result-graphs.js.
  out.summary = sec("summary", () => ({
    objects: Game.Summary.getObjects(),
    datasets: Object.fromEntries(
      ["Gold", "Science", "CitiesFounded", "WondersConstructed"].map((id) => [id, sec(`summary.${id}`, () => Game.Summary.getDataSets(id))])
    ),
    datapoints: sec("summary.points", () => Game.Summary.getDataPoints()),
  }));

  out.errors = errors;
  return JSON.stringify(out);
})()
